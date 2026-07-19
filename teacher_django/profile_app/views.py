import json
import logging
import threading
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from core.models import User
from .models import StudentProfile, ProfileConversationSession
from core.xunfei_spark import spark_client
from agent_system.services.profile_events import infer_profile_delta_from_text, record_profile_event
from agent_system.services.profile_auto_updater import build_review_queue
from .growth_report import _normalize_knowledge_score, build_growth_report

logger = logging.getLogger(__name__)


def _build_profile_radar_data(knowledge_items, learning_goals_list, misconceptions_list,
                               learning_preferences_items, confidence_scores, engagement):
    """汇总六维画像数据为雷达图所需的标签与数值（均为0-100分）。"""
    scores = []
    for k, v in knowledge_items:
        if k == 'overall' or str(k).startswith('__'):
            continue
        score = _normalize_knowledge_score(v)
        if score is not None:
            scores.append(score)
    knowledge_score = round(sum(scores) / len(scores), 1) if scores else 0

    engagement_score = engagement.get('score', 0) if isinstance(engagement, dict) else 0

    goals_score = min(len(learning_goals_list) * 25, 100)

    if learning_preferences_items:
        true_count = sum(1 for _, v in learning_preferences_items if v)
        preference_score = round(true_count / len(learning_preferences_items) * 100)
    else:
        preference_score = 0

    clarity_score = max(100 - len(misconceptions_list) * 20, 0)

    if confidence_scores:
        confidence_score = round(sum(confidence_scores.values()) / len(confidence_scores) * 100)
    else:
        confidence_score = 0

    return {
        'labels': ['知识掌握', '学习参与度', '学习目标', '学习偏好', '概念清晰度', '画像置信度'],
        'values': [knowledge_score, engagement_score, goals_score, preference_score, clarity_score, confidence_score],
    }


# 六维展示标签（OLM 可解释展示用）
_DIM_LABELS = [
    ('knowledge_profile', '知识基础'),
    ('cognitive_style', '认知风格'),
    ('learning_goals', '学习目标'),
    ('misconceptions', '易错点'),
    ('engagement', '学习参与'),
    ('learning_preferences', '学习偏好'),
]


def _dimension_evidence(profile_obj):
    """把 SPIRES 抽取的 _meta 整理成"每维：置信度 + 支撑它的学生原话 + FSLSM 四轴"的可展示列表。
    这是 Open Learner Model(OLM) 的可解释性落地：让学生看到画像每一维"凭什么这么判"。"""
    meta = (profile_obj or {}).get('_meta') if isinstance(profile_obj, dict) else None
    if not isinstance(meta, dict):
        return []
    out = []
    for key, label in _DIM_LABELS:
        m = meta.get(key)
        if not isinstance(m, dict):
            continue
        try:
            conf = max(0.0, min(1.0, float(m.get('confidence') or 0.0)))
        except Exception:
            conf = 0.0
        evidence = [str(e).strip() for e in (m.get('evidence') or []) if str(e).strip()][:4]
        item = {
            'label': label,
            'confidence_pct': round(conf * 100),
            'evidence': evidence,
            'weak': conf < 0.3,  # 证据不足 → 前端标"待补充"
        }
        if key == 'cognitive_style':
            fslsm = m.get('fslsm') if isinstance(m.get('fslsm'), dict) else {}
            axes = []
            for axis, (p1, p2, desc) in _FSLSM_AXES.items():
                v = str(fslsm.get(axis) or '').strip().lower()
                if v in (p1, p2):
                    left, right = desc.split('↔')
                    axes.append({'pole': (left if v == p1 else right), 'desc': desc})
            item['fslsm'] = axes
        out.append(item)
    return out


# 这些不是真实知识点：__ 前缀是内部元数据；'overall' 是整体水平摘要；
# 其余是免费大模型偶尔把 JSON schema 原样回显时混进来的 schema 关键字。
_KP_JUNK_KEYS = {'type', 'description', 'properties', 'items', 'required',
                 '$schema', 'enum', 'object', 'array', 'overall'}


def _is_meta_kp_key(t):
    return str(t).startswith('__') or str(t) in _KP_JUNK_KEYS


def _sanitize_parsed_profile(parsed):
    """清洗 LLM 解析出的画像：主要处理 knowledge_profile 被大模型回显成 JSON schema
    （如 {'type':'object','description':'...'}）的情况，剔除 schema 关键字/元数据键，
    避免把 'type'/'description' 当成知识点写进画像。"""
    if not isinstance(parsed, dict):
        return {}
    kp = parsed.get('knowledge_profile')
    if isinstance(kp, dict):
        cleaned = {k: v for k, v in kp.items() if not _is_meta_kp_key(k)}
        # 值本身若还是 schema 结构（形如 {'type':...} / {'description':...}）也一并丢弃
        cleaned = {
            k: v for k, v in cleaned.items()
            if not (isinstance(v, dict) and set(map(str, v.keys())) & {'type', 'properties', 'items', '$schema'})
        }
        parsed['knowledge_profile'] = cleaned
    return parsed


def _merged_knowledge_items(user, profile_obj):
    """合并 B(对话) + A(做题/冷启动播种) 的知识点，A 优先覆盖；过滤内部键与 overall 摘要键。
    返回 (raw_items, display_items)：raw 供雷达/成长快照用原始值，display 是百分比字符串供面板展示。
    仪表盘/详情页/构建页三处共用，保证知识展示口径一致。"""
    merged = {}
    b_kp = (profile_obj.get('knowledge_profile')
            if isinstance(profile_obj, dict) and isinstance(profile_obj.get('knowledge_profile'), dict) else {})
    ap = getattr(user, 'student_profile', None)
    a_kp = ap.knowledge_profile if (ap and isinstance(ap.knowledge_profile, dict)) else {}
    for src in (b_kp, a_kp):  # A 后写覆盖 B
        for t, v in src.items():
            if not _is_meta_kp_key(t):
                merged[t] = v
    raw = list(merged.items())
    disp = []
    for t, v in merged.items():
        s = _normalize_knowledge_score(v)
        if s is not None:
            disp.append((t, str(int(round(s))) + '%'))
        elif isinstance(v, dict):
            disp.append((t, '—'))  # 结构化但无 mastery_score，别渲染 dict repr
        else:
            disp.append((t, str(v)))
    return raw, disp


@login_required
def profile_dashboard(request):
    """用户画像仪表盘页面 - 新布局"""
    profile = StudentProfile.objects.filter(
        user=request.user,
        course_id='default'
    ).order_by('-last_updated').first()

    profile_obj = {}
    knowledge_items = []
    learning_goals_list = []
    misconceptions_list = []
    engagement = {}
    learning_preferences_items = []
    confidence_scores = {}

    if profile:
        try:
            profile_obj = json.loads(profile.profile_data)
        except Exception:
            profile_obj = {}
        try:
            confidence_scores = json.loads(profile.confidence_scores) if profile.confidence_scores else {}
        except Exception:
            confidence_scores = {}

        kp = profile_obj.get('knowledge_profile') or {}
        if isinstance(kp, dict):
            knowledge_items = list(kp.items())

        learning_goals_list = profile_obj.get('learning_goals') or []
        misconceptions_list = profile_obj.get('misconceptions') or []
        engagement = profile_obj.get('engagement') or {}
        lp = profile_obj.get('learning_preferences') or {}
        lp_key_map = {
            'online_learning': '在线学习',
            'practical_application': '注重实践应用',
            'self_reflection': '注重自我反思',
        }
        if isinstance(lp, dict):
            learning_preferences_items = [(lp_key_map.get(k, k.replace('_', ' ')), v) for k, v in lp.items()]

    # 合并做题画像(A)：让"知识掌握"面板与"今日复习推荐"同源、能反映做题进度，
    # 而不是只显示对话构建(B)那套常年为空的知识点。
    agent_profile = getattr(request.user, 'student_profile', None)
    knowledge_items_raw, knowledge_items = _merged_knowledge_items(request.user, profile_obj)
    # 参与度同理并入 A：A 的 engagement 由做题/对话行为累积(有真实 score/trend)，
    # B 的 engagement 常年是 0 或写死的 60，雷达"学习参与度"不该只反映对话画像。
    if agent_profile and isinstance(agent_profile.engagement, dict) and agent_profile.engagement.get('score'):
        engagement = {**(engagement if isinstance(engagement, dict) else {}), **agent_profile.engagement}

    radar_chart_data = _build_profile_radar_data(
        knowledge_items_raw, learning_goals_list, misconceptions_list,
        learning_preferences_items, confidence_scores, engagement,
    )

    growth_report = {
        'has_growth_history': False,
        'growth_deltas': [],
        'growth_trend': None,
        'growth_narrative': '',
        'latest_snapshot_at': None,
        'previous_snapshot_at': None,
        'radar_previous_values': None,
    }
    if profile:
        try:
            growth_report = build_growth_report(
                request.user, 'default', profile, radar_chart_data,
                knowledge_items_raw, misconceptions_list, learning_goals_list,
            )
        except Exception:
            logger.exception('构建学习成长报告失败')

    if growth_report.get('radar_previous_values'):
        radar_chart_data = dict(radar_chart_data, previous_values=growth_report['radar_previous_values'])

    agent_profile = getattr(request.user, 'student_profile', None)
    review_queue = []
    if agent_profile:
        review_queue = build_review_queue(agent_profile.knowledge_profile, agent_profile.knowledge_timestamps)

    # 置信度维度用中文标签展示（此前模板直接渲染 knowledge_profile 等英文键名）
    _conf_label_map = {
        'knowledge_profile': '知识掌握', 'cognitive_style': '认知风格',
        'learning_goals': '学习目标', 'misconceptions': '概念清晰度',
        'engagement': '学习参与度', 'learning_preferences': '学习偏好',
    }
    confidence_items = [
        (_conf_label_map.get(_k, _k), _v)
        for _k, _v in (confidence_scores.items() if isinstance(confidence_scores, dict) else [])
    ]

    # —— 演示样例：admin 用户一律用一套均衡好看的示例雷达数据，便于 PPT 截图/演示
    #    （admin 真实画像稀疏、雷达歪，不适合展示）。
    #    TODO(明天完善)：接入 AKT/COGENT 的真实评分，替换样例。
    is_demo_sample = False
    if request.user.username == 'admin':
        radar_chart_data = {
            'labels': ['知识掌握', '学习参与度', '学习目标', '学习偏好', '概念清晰度', '画像置信度'],
            'values': [82, 76, 90, 68, 88, 74],
        }
        is_demo_sample = True

    context = {
        'profile': profile,
        'current_profile': profile.profile_data if profile else '{}',
        'current_profile_obj': profile_obj,
        'knowledge_items': knowledge_items,
        'learning_goals_list': learning_goals_list,
        'misconceptions_list': misconceptions_list,
        'engagement': engagement,
        'learning_preferences_items': learning_preferences_items,
        'confidence_scores': confidence_scores,
        'confidence_items': confidence_items,
        # OLM 可解释：每维证据(学生原话) + 证据驱动置信度 + FSLSM 四轴
        'dimension_evidence': _dimension_evidence(profile_obj),
        'has_profile': bool(profile) or is_demo_sample,
        'is_demo_sample': is_demo_sample,
        'radar_chart_data': radar_chart_data,
        'has_growth_history': growth_report['has_growth_history'],
        'growth_deltas': growth_report['growth_deltas'],
        'growth_trend': growth_report['growth_trend'],
        'growth_narrative': growth_report['growth_narrative'],
        'latest_snapshot_at': growth_report['latest_snapshot_at'],
        'previous_snapshot_at': growth_report['previous_snapshot_at'],
        'review_queue': review_queue,
    }

    return render(request, 'profile/profile_new.html', context)


@login_required
def profile_building_view(request):
    """画像构建页面"""
    # 检查是否已有活跃会话，且未完成
    session = ProfileConversationSession.objects.filter(
        user=request.user,
        status='active'
    ).first()
    
    # 如果会话已完成（6 个维度已覆盖，或达到轮次上限），标记为完成——与 step/stream 的判定口径一致
    if session:
        try:
            _done = len(json.loads(session.answered_dimensions or '[]')) + len(json.loads(session.skipped_dimensions or '[]'))
        except Exception:
            _done = 0
        if _done >= 6 or session.current_round >= 14:
            session.status = 'completed'
            session.save()
            session = None
    
    if not session:
        # 创建新会话
        session = ProfileConversationSession.objects.create(
            user=request.user,
            asked_dimensions='[]',
            answered_dimensions='[]',
            skipped_dimensions='[]',
            conversation_history='[]',
            current_round=0,
            status='active'
        )
        # 新会话创建后，主动让 AI 提出第一个问题（让 AI 主动发起对话）
        try:
            asked_dims = []
            answered_dims = []
            skipped_dims = []
            conv = []
            ai_q, _stay, _dim, _kind = generate_next_question(conv, answered_dims, skipped_dims, asked_dims, user=request.user)
            conv.append({'role': 'assistant', 'content': ai_q, 'dim': _dim, 'kind': _kind})
            if _dim and _dim not in asked_dims:
                asked_dims.append(_dim)
            session.asked_dimensions = json.dumps(asked_dims)
            session.conversation_history = json.dumps(conv)
            session.save()
        except Exception:
            # 失败时忽略，页面仍可手动触发
            pass
    else:
        # 如果存在会话但对话历史为空，也主动发起第一个问题
        try:
            conv_existing = json.loads(session.conversation_history or '[]')
            if not conv_existing:
                asked_dims = json.loads(session.asked_dimensions or '[]')
                answered_dims = json.loads(session.answered_dimensions or '[]')
                skipped_dims = json.loads(session.skipped_dimensions or '[]')
                ai_q, _stay, _dim, _kind = generate_next_question([], answered_dims, skipped_dims, asked_dims, user=request.user)
                conv_existing = [{'role': 'assistant', 'content': ai_q, 'dim': _dim, 'kind': _kind}]
                if _dim and _dim not in asked_dims:
                    asked_dims.append(_dim)
                session.asked_dimensions = json.dumps(asked_dims)
                session.conversation_history = json.dumps(conv_existing)
                session.save()
        except Exception:
            pass
    
    # 获取当前画像数据（如果有），并解析为结构化对象以便模板显示
    profile = StudentProfile.objects.filter(
        user=request.user,
        course_id='default'
    ).order_by('-last_updated').first()

    profile_obj = {}
    knowledge_items = []
    learning_goals_list = []
    misconceptions_list = []
    engagement = {}
    learning_preferences_items = []
    if profile:
        try:
            profile_obj = json.loads(profile.profile_data)
        except Exception:
            profile_obj = {}
        # 合并 A(做题/冷启动播种)，与仪表盘/详情页同源，并格式化成百分比，别显示原始浮点/字典
        _, knowledge_items = _merged_knowledge_items(request.user, profile_obj)
        learning_goals_list = profile_obj.get('learning_goals') or []
        misconceptions_list = profile_obj.get('misconceptions') or []
        engagement = profile_obj.get('engagement') or {}
        lp = profile_obj.get('learning_preferences') or {}
        # 将可能的英文键名映射为中文用于前端展示，避免直接显示英文标识
        lp_key_map = {
            'online_learning': '在线学习',
            'practical_application': '注重实践应用',
            'self_reflection': '注重自我反思',
        }
        if isinstance(lp, dict):
            learning_preferences_items = [(lp_key_map.get(k, k.replace('_', ' ')), v) for k, v in lp.items()]

    context = {
        'session_id': session.id,
        'current_profile': profile.profile_data if profile else '{}',
        'current_profile_obj': profile_obj,
        'knowledge_items': knowledge_items,
        'learning_goals_list': learning_goals_list,
        'misconceptions_list': misconceptions_list,
        'engagement': engagement,
        'learning_preferences_items': learning_preferences_items,
        'asked_dimensions': json.loads(session.asked_dimensions),
        'answered_dimensions': json.loads(session.answered_dimensions),
        'skipped_dimensions': json.loads(session.skipped_dimensions),
        'conversation_history': json.loads(session.conversation_history),
    }
    
    return render(request, 'profile/profile_building.html', context)


@login_required
def detailed_profile_view(request):
    """详细个人资料页面"""
    from curriculum_app.models import LearningProgress
    
    profile = StudentProfile.objects.filter(
        user=request.user,
        course_id='default'
    ).order_by('-last_updated').first()

    profile_obj = {}
    learning_goals_list = []
    misconceptions_list = []
    engagement = {}
    learning_preferences_items = []

    if profile:
        try:
            profile_obj = json.loads(profile.profile_data)
        except Exception:
            profile_obj = {}

        learning_goals_list = profile_obj.get('learning_goals') or []
        misconceptions_list = profile_obj.get('misconceptions') or []
        engagement = profile_obj.get('engagement') or {}
        lp = profile_obj.get('learning_preferences') or {}
        lp_key_map = {
            'online_learning': '在线学习',
            'practical_application': '注重实践应用',
            'self_reflection': '注重自我反思',
        }
        if isinstance(lp, dict):
            learning_preferences_items = [(lp_key_map.get(k, k.replace('_', ' ')), v) for k, v in lp.items()]

    # 合并权威画像 A 的知识掌握与参与度，否则详情页"知识掌握情况"永远空壳、参与度恒为 B 的 0/60，
    # 与仪表盘不一致。用与仪表盘/构建页同一个合并器，展示成百分比。
    _agent_profile = getattr(request.user, 'student_profile', None)
    if _agent_profile:
        _, _kp_disp = _merged_knowledge_items(request.user, profile_obj)
        if _kp_disp:
            profile_obj = dict(profile_obj)
            profile_obj['knowledge_profile'] = dict(_kp_disp)
        if isinstance(_agent_profile.engagement, dict) and _agent_profile.engagement.get('score'):
            engagement = {**(engagement if isinstance(engagement, dict) else {}), **_agent_profile.engagement}

    # 获取真实的学习统计数据 - 使用LearningProgress模型
    enrolled_courses = LearningProgress.objects.filter(user=request.user).values('course_outline').distinct().count()
    completed_courses = LearningProgress.objects.filter(user=request.user, status='completed').values('course_outline').distinct().count()

    # 真实的学习活动 / 统计（替换此前写死的假数据）
    try:
        from agent_system.models import ProfileEvent
    except Exception:
        ProfileEvent = None
    _EVENT_LABELS = {
        'material_quiz_submitted': ('fas fa-file-alt', '完成了一次随堂测验'),
        'material_quiz_generated': ('fas fa-list-check', '生成了练习题'),
        'course_ai_opened': ('fas fa-robot', '打开了课程 AI 助教'),
        'chat': ('fas fa-comments', '进行了一次学习对话'),
        'learning': ('fas fa-comments', '进行了一次学习对话'),
        'peer_teaching': ('fas fa-people-arrows', '给"小艾"讲解了知识点'),
        'profile_building_answer': ('fas fa-user-pen', '完善了个人画像'),
    }
    recent_activities = []
    quiz_attempts = 0
    if ProfileEvent is not None:
        try:
            events = list(ProfileEvent.objects.filter(user=request.user).order_by('-created_at')[:8])
            for ev in events:
                icon, label = _EVENT_LABELS.get(ev.event_type, ('fas fa-circle-dot', '学习活动'))
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                title_bits = payload.get('material_title') or payload.get('course_title') or ''
                title = f'{label}：{title_bits}' if title_bits else label
                recent_activities.append({'icon': icon, 'title': title, 'created_at': ev.created_at})
            quiz_attempts = ProfileEvent.objects.filter(user=request.user, event_type='material_quiz_submitted').count()
        except Exception:
            logger.exception('加载最近学习活动失败')

    # 成就积分与学习时长基于真实行为估算，不再写死为 0
    achievement_points = completed_courses * 100 + quiz_attempts * 10
    learning_time = round(quiz_attempts * 0.2 + completed_courses * 1.0, 1)

    context = {
        'current_profile_obj': profile_obj,
        'learning_goals_list': learning_goals_list,
        'misconceptions_list': misconceptions_list,
        'engagement': engagement,
        'learning_preferences_items': learning_preferences_items,
        'enrolled_courses': enrolled_courses,
        'completed_courses': completed_courses,
        'learning_time': learning_time,
        'achievement_points': achievement_points,
        'recent_activities': recent_activities,
    }

    if request.method == 'POST':
        user = request.user
        user.full_name = request.POST.get('full_name', '')
        user.major = request.POST.get('major', '')
        user.grade = request.POST.get('grade', '')
        
        # 处理头像上传
        if 'avatar' in request.FILES:
            user.avatar = request.FILES['avatar']
        
        user.save()
        # 可以添加一个消息通知用户资料已更新
        from django.contrib import messages
        messages.success(request, '资料更新成功！')
        return redirect('detailed_profile')

    return render(request, 'profile/detailed_profile.html', context)


@login_required
def profile_view(request):
    """旧的画像详情页已并入 profile_dashboard（唯一的学习画像页），此处重定向过去，
    保证全站只有一个学习画像页面（含六维雷达）。"""
    return redirect('profile_dashboard')


@login_required
def profile_building_step(request):
    """处理画像构建的每一步：仅保存用户回答或跳过，并返回简短状态。AI 回复通过 SSE 在 `profile_building_stream` 中生成并推送。"""
    if request.method != 'POST':
        return JsonResponse({'error': '仅支持 POST 请求'}, status=405)

    try:
        data = json.loads(request.body)
        session_id = data.get('sessionId')
        user_message = data.get('message', '')
        action = data.get('action', 'answer')  # 'answer' or 'skip'

        # 获取会话
        session = get_object_or_404(ProfileConversationSession, id=session_id, user=request.user)

        if session.status != 'active':
            return JsonResponse({'error': '会话已结束'}, status=400)

        # 更新会话状态（仅记录用户行为）
        asked_dims = json.loads(session.asked_dimensions or '[]')
        answered_dims = json.loads(session.answered_dimensions or '[]')
        skipped_dims = json.loads(session.skipped_dimensions or '[]')
        conversation_history = json.loads(session.conversation_history or '[]')

        # 注意：不要在这里立即标记维度为已回答
        # 维度是否已回答由 generate_next_question 的 is_followup 标志决定
        # 追问时，用户还需要回答同一维度的问题
        if action == 'answer' and user_message:
            conversation_history.append({'role': 'user', 'content': user_message})
            try:
                record_profile_event(
                    request.user,
                    'profile_building_answer',
                    {
                        'session_id': session.id,
                        'dimension': asked_dims[-1] if asked_dims else '',
                        'text': user_message[:800],
                        'profile_delta': infer_profile_delta_from_text(user_message),
                    },
                    source_app='profile_app',
                    confidence=0.8,
                )
            except Exception:
                logger.exception('Failed to record profile event for profile building answer')
        elif action == 'skip':
            if asked_dims:
                current_dimension = asked_dims[-1]
                skipped_dims.append(current_dimension)
                # 跳过时也要从 asked_dims 移除
                asked_dims.remove(current_dimension)
                # 注意：不在这里 +1 轮次。跳过后前端仍会打开 stream，由 stream 统一 +1，
                # 否则一次跳过会被计成 2 轮，让 current_round>=14 硬上限对跳过多的会话提前触发。

        session.asked_dimensions = json.dumps(asked_dims)
        session.answered_dimensions = json.dumps(answered_dims)
        session.skipped_dimensions = json.dumps(skipped_dims)
        session.conversation_history = json.dumps(conversation_history)
        # 注意：这里不增加 current_round，由 profile_building_stream 根据是否追问来决定
        session.save()

        completed = False
        _step_dims_done = len(json.loads(session.answered_dimensions or '[]')) + len(json.loads(session.skipped_dimensions or '[]'))
        if _step_dims_done >= 6 or session.current_round >= 14:
            session.status = 'completed'
            session.save()
            completed = True
            # 关键：step 路径(尤其"以跳过收尾")完成时前端不会再开 stream，
            # 必须在这里解析+落库+桥接，否则整场画像构建丢失。同步执行保证一定落库。
            _persist_profile_session(session.id, request.user.id)

        return JsonResponse({'ok': True, 'session_id': session.id, 'completed': completed, 'currentRound': session.current_round})

    except Exception as e:
        logger.exception('profile_building_step error')
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def profile_building_regenerate(request):
    """重新生成画像 - 创建新会话并开始新的对话"""
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            session_id = data.get('sessionId')
            
            # 找到当前会话并标记为已废弃
            try:
                session = ProfileConversationSession.objects.get(id=session_id, user=request.user)
                session.status = 'abandoned'
                session.save()
            except ProfileConversationSession.DoesNotExist:
                pass
            
            # 创建新会话
            new_session = ProfileConversationSession.objects.create(
                user=request.user,
                asked_dimensions='[]',
                answered_dimensions='[]',
                skipped_dimensions='[]',
                conversation_history='[]',
                current_round=0,  # 显式设置为0
                status='active'
            )
            
            # 生成第一个问题
            ai_q, _stay, _dim, _kind = generate_next_question([], [], [], [], user=request.user)
            conv = [{'role': 'assistant', 'content': ai_q, 'dim': _dim, 'kind': _kind}]
            asked_dims = [_dim] if _dim else []

            new_session.asked_dimensions = json.dumps(asked_dims)
            new_session.conversation_history = json.dumps(conv)
            new_session.save()
            
            return JsonResponse({'ok': True, 'newSessionId': new_session.id})
            
        except Exception as e:
            logger.exception('profile_building_regenerate error')
            return JsonResponse({'error': str(e)}, status=500)
    
    return JsonResponse({'error': 'Invalid method'}, status=400)


def _count_current_dim_followups(conversation_history):
    """Count how many follow-up exchanges have happened for the current dimension.

    Counts backwards in history: each (assistant, user) pair = 1 exchange.
    The first exchange is the original Q+A; subsequent ones are follow-ups.
    Returns 0 if no follow-up has occurred yet.
    """
    if not conversation_history or len(conversation_history) < 2:
        return 0
    i = len(conversation_history) - 1
    pairs = 0
    while i >= 1:
        if (conversation_history[i].get('role') == 'user' and
                conversation_history[i - 1].get('role') == 'assistant'):
            pairs += 1
            i -= 2
        else:
            break
    return max(0, pairs - 1)  # subtract 1 for the original Q+A


def analyze_answer_quality(conversation_history, spark_client):
    """Check if the last answer needs a follow-up (max 1 per dimension).

    Returns (needs_followup: bool, followup_question: str or None).
    Rules:
      - Never follow up more than once per dimension.
      - Only follow up when the answer is very short (<10 chars) or clearly
        vague (matches keyword list). No LLM quality-judge call.
    """
    if not conversation_history or len(conversation_history) < 2:
        return False, None

    # 每维度的追问上限由调用方 generate_next_question 用 _count_dim_stays 控制（按维度计数），
    # 这里不再用 _count_current_dim_followups（它会把整段对话都算进来，导致后面维度永不追问）。

    last_user_msg = None
    last_ai_question = None
    for i, msg in enumerate(reversed(conversation_history)):
        if i == 0 and msg.get('role') == 'user':
            last_user_msg = msg.get('content', '')
        elif i == 1 and msg.get('role') == 'assistant':
            last_ai_question = msg.get('content', '')

    if not last_user_msg:
        return False, None

    is_too_short = len(last_user_msg.strip()) < 10
    vague_keywords = ['还行', '一般', '差不多', '不太清楚', '不知道', '随便',
                      '都可以', '没想过', '不确定', '无所谓', '说不好']
    has_vague = any(kw in last_user_msg for kw in vague_keywords)

    if not (is_too_short or has_vague):
        return False, None

    # Generate a targeted follow-up via LLM
    if spark_client:
        try:
            messages = [
                {
                    'role': 'system',
                    'content': (
                        "你是一个友好的学习助手，正在了解学生的学习情况。\n"
                        "任务：学生刚才的回答太简短或太模糊，请生成一个自然的追问，引导学生多说一点。\n"
                        "要求：\n"
                        "1. 问题要简短自然，像朋友聊天\n"
                        "2. 直接引导学生说出具体内容，不要用\"你能再详细说说吗\"这类空话\n"
                        "3. 不超过20字\n"
                        "4. 不要重复原来的问题"
                    )
                },
                {
                    'role': 'user',
                    'content': f"之前问：{last_ai_question or '一个问题'}\n学生回答：{last_user_msg}\n请生成一个简短的追问。"
                }
            ]
            ai_followup = spark_client.get_response(messages)
            if ai_followup and ai_followup.strip():
                return True, ai_followup.strip()
        except Exception:
            logger.exception('AI生成追问问题失败')

    # Fallback follow-up texts
    if is_too_short:
        return True, "能多说一点吗？比如具体是哪方面？"
    return True, "可以举个例子或再展开说说吗？"

_ALL_DIMENSIONS = [
    {'key': 'knowledge_base', 'name': '知识基础', 'description': '已掌握的知识和技能水平'},
    {'key': 'cognitive_style', 'name': '认知风格', 'description': '学习方式偏好（视觉/听觉/动手等）'},
    {'key': 'learning_pace', 'name': '学习节奏', 'description': '学习时间安排和节奏习惯'},
    {'key': 'metacognition', 'name': '元认知', 'description': '自我反思和学习策略'},
    {'key': 'motivation', 'name': '学习动机', 'description': '学习的目的和驱动力'},
    {'key': 'error_patterns', 'name': '易错模式', 'description': '常见错误和薄弱环节'},
]

# 用户想要例子/表示没听懂时，各维度给的贴近生活的提示
_DIM_HELP = {
    'knowledge_base': '比如：学过高中数学、大学线性代数，或者写过一点 Python；哪怕是“编程零基础、数学还行”也算。',
    'cognitive_style': '比如：喜欢看视频跟着做、喜欢先看书弄懂原理，还是直接上手写代码去试。',
    'learning_pace': '比如：每天能学一小时、喜欢周末集中学，或者遇到难点喜欢慢慢啃透再往下。',
    'metacognition': '比如：学完会自己做几道题检验、不懂就查资料反复看，或者讲给别人听来确认自己真懂了。',
    'motivation': '比如：为了考研、为了找工作面试，还是纯粹兴趣想搞懂某个方向。',
    'error_patterns': '比如：常在公式推导、边界情况，或者代码调试上卡壳、出错。',
}

# 用户在反问 / 求助 / 想要例子（而不是在回答）的信号词
_CLARIFY_KW = ['举个例子', '例子', '什么意思', '啥意思', '不懂', '不明白', '没听懂', '没太懂',
               '不太懂', '你说呢', '怎么说', '咋说', '怎么讲', '解释', '是指', '指的是',
               '有哪些', '比如什么', '比如说啥', '怎么理解', '再说一遍', '什么呀', '啥呀',
               '听不懂', '不知道说什么', '不知道该说', '你先说', '你举']


def _last_user_message(conversation_history):
    for m in reversed(conversation_history or []):
        if m.get('role') == 'user':
            return str(m.get('content') or '')
    return ''


def _is_clarification_request(text):
    """判断用户这句是在「反问/求助/要例子」而不是在回答本维度的问题。"""
    t = str(text or '').strip()
    if not t:
        return False
    if any(k in t for k in _CLARIFY_KW):
        return True
    # 很短又带问号，多半是在反问求助
    if ('？' in t or '?' in t) and len(t) <= 15:
        return True
    return False


def _count_dim_stays(conversation_history, dim_key):
    """当前维度已额外停留（追问/举例）几次——只数带标记的助手消息。
    旧的 _count_current_dim_followups 会把整段对话都算进来，导致第一个维度之后再也不追问。"""
    n = 0
    for m in (conversation_history or []):
        if (m.get('role') == 'assistant' and m.get('dim') == dim_key
                and m.get('kind') in ('followup', 'clarification')):
            n += 1
    return n


def _generate_clarification(dim_key, conversation_history):
    """用户反问/要例子时，给贴心的例子帮他理解，并温和地重新邀请他回答。留在当前维度。"""
    example = _DIM_HELP.get(dim_key, '你就随便说说自己的真实情况，没有标准答案。')
    if spark_client:
        try:
            messages = [{'role': 'system', 'content': (
                "你是一个耐心、亲切的学习助手。学生对你刚才的问题有疑问，或者想让你先举个例子。\n"
                "任务：先自然地回应他，用一两个贴近生活的具体例子帮他明白你想了解什么，"
                "然后用轻松的语气再邀请他说说自己的情况。\n"
                "要求：口语化、简短（50字以内）、不说教、不要罗列 1234、不要机械重复原来的问题。")}]
            for m in (conversation_history or [])[-6:]:
                role = m.get('role', 'user')
                role = role if role in ('user', 'assistant') else 'user'
                messages.append({'role': role, 'content': m.get('content', '')})
            messages.append({'role': 'user', 'content': f'请给出帮助性的回应和例子，参考方向：{example}'})
            resp = spark_client.get_response(messages)
            if resp and resp.strip():
                return resp.strip()
        except Exception:
            logger.exception('生成澄清/举例回应失败')
    return f'当然可以～{example}你怎么样呢？'


def _gen_dim_question(dim, conversation_history, user=None):
    """为某个维度生成问题。

    设计（针对弱模型做的取舍）：先用一条【该维度的种子问题】把"这次要问哪个维度"钉死，
    再让 LLM 只做"贴专业 + 承接上一句 + 口语化改写"，核心意图不许改。
    这样既保证每个维度问的是不同的东西（种子决定），又能贴合学生专业/上文（LLM 润色），
    避免了之前"放开让模型自由生成 → 弱模型对着专业反复问同一句"的问题。LLM 不可用时直接用种子。"""
    major = (getattr(user, 'major', '') or '').strip()
    grade = (getattr(user, 'grade', '') or '').strip()
    # 种子问题：该维度的基础问法（带专业时优先专业化模板），本身就是维度专属、且会轮换
    seed = generate_dynamic_question(dim, conversation_history, major=major)

    if spark_client:
        try:
            stu_bits = []
            if major:
                stu_bits.append(f'专业：{major}')
            if grade:
                stu_bits.append(f'年级：{grade}')
            student_ctx = '；'.join(stu_bits) if stu_bits else '专业、年级暂时未知'
            last_user = _last_user_message(conversation_history)
            system_prompt = (
                "你是一个亲切的学习助手，正在像朋友聊天一样了解一名学生，为他建立学习画像。\n"
                f"【学生背景】{student_ctx}。\n"
                "我会给你【这次要问的核心问题】。请把它改写成一句更自然的话，规则：\n"
                "1. 若学生上一句说了内容，先用半句自然承接一下再问，让对话连贯；\n"
                "2. 有专业时，把问题的例子/说法贴到该专业上（如计算机→编程/数据结构，电子信息→电路/信号），"
                "但【问的核心意图必须和给定问题一致，不能问成别的维度】；\n"
                "3. 口语化、不超过35字、不堆术语、不要罗列1234；\n"
                "4. 只输出改写后的这一句问话本身，不要任何解释或前缀。\n"
            )
            messages = [{'role': 'system', 'content': system_prompt}]
            if last_user:
                messages.append({'role': 'user', 'content': f'（学生上一句说：{last_user[:120]}）'})
            messages.append({'role': 'user', 'content': f'这次要问的核心问题：{seed}\n请按上面规则改写成一句自然的话。'})
            ai_resp = spark_client.get_response(messages)
            if ai_resp and ai_resp.strip():
                return ai_resp.strip()
        except Exception:
            logger.exception('调用 spark_client 生成问题失败')
    return seed


def generate_next_question(conversation_history, answered_dims, skipped_dims, asked_dims=None, user=None):
    """生成下一句 AI 消息，能真正对话：识别用户的反问/求助并举例帮助，而不是机械换题。
    user：传入当前学生，用于把专业/年级喂给问题生成，让问题贴合其领域、不再千篇一律。

    返回: (message, is_stay, target_dim_key, kind)
    - is_stay=True 表示停留在当前维度（追问或举例），调用方不要推进维度
    - target_dim_key: 这条消息对应/推进到的维度（用于给对话历史打标记）
    - kind: 'question' | 'followup' | 'clarification' | 'done'
    """
    available_dims = [d for d in _ALL_DIMENSIONS
                      if d['key'] not in answered_dims and d['key'] not in skipped_dims]
    if not available_dims:
        return "感谢你的耐心分享！学习画像已经构建完成，可以开始个性化学习啦。", False, None, 'done'

    asked_dims = asked_dims or []

    # 首个问题：还没问过任何维度，直接问第一个
    if not asked_dims:
        target = available_dims[0]
        return _gen_dim_question(target, conversation_history, user), False, target['key'], 'question'

    current_key = asked_dims[-1]
    current_dim = next((d for d in _ALL_DIMENSIONS if d['key'] == current_key), available_dims[0])
    last_user = _last_user_message(conversation_history)
    stays = _count_dim_stays(conversation_history, current_key)

    # 1) 用户在反问/求助/要例子 → 给例子帮他理解，留在当前维度（最多2次，避免绕不出来）
    if last_user and _is_clarification_request(last_user) and stays < 2:
        return _generate_clarification(current_key, conversation_history), True, current_key, 'clarification'

    # 2) 回答太短/太模糊 → 追问一次（每个维度至多1次，按维度计数而非整段对话）
    if stays < 1:
        needs_followup, followup_question = analyze_answer_quality(conversation_history, spark_client)
        if needs_followup and followup_question:
            return followup_question, True, current_key, 'followup'

    # 3) 推进到「当前维度之后」的下一个维度（修掉旧逻辑误取当前维度的问题）
    advance_candidates = [d for d in available_dims if d['key'] != current_key]
    if not advance_candidates:
        return "感谢你的耐心分享！学习画像已经构建完成，可以开始个性化学习啦。", False, None, 'done'
    target = advance_candidates[0]
    return _gen_dim_question(target, conversation_history, user), False, target['key'], 'question'


def generate_dynamic_question(dim_info, conversation_history, major=''):
    """根据维度动态生成不同的问题（LLM 不可用时的兜底）。带上专业时，尽量把问题贴到该专业上。"""
    dimension_key = dim_info['key']
    major = (major or '').strip()
    # 兜底也尽量贴专业：有专业信息时，知识基础/易错点这类维度直接围绕专业问，避免"学过数学物理编程"的通用腔
    if major:
        major_templates = {
            'knowledge_base': [
                f"你{major}专业里，哪些核心课程或知识点你已经比较熟悉了？",
                f"在{major}这个方向上，你目前掌握得比较扎实的是哪部分？",
                f"学{major}到现在，哪些基础你觉得自己打得还不错？",
            ],
            'error_patterns': [
                f"学{major}的过程中，哪些内容你经常出错或觉得特别难懂？",
                f"{major}里有没有哪些概念/方法你总是记不住或容易混淆？",
                f"在{major}的题目或实验里，你最容易在什么地方卡壳？",
            ],
            'motivation': [
                f"你学{major}，是为了考研、就业，还是有具体想做的方向？",
                f"在{major}这条路上，你最想达成什么目标？",
            ],
        }
        if dimension_key in major_templates:
            import random
            return random.choice(major_templates[dimension_key])

    # 每个维度准备多个开放式问题模板（更具体明确）
    question_templates = {
        'knowledge_base': [
            "你之前有没有学过数学、物理或者编程这类课程？",
            "在学校里，你对哪些科目比较熟悉？",
            "你有没有接触过数据分析、统计或者逻辑推理方面的内容？",
            "之前学过的课程里，哪些让你印象比较深？",
            "你觉得自己在哪些学科上有一定基础？"
        ],
        'cognitive_style': [
            "学习新知识时，你喜欢看视频讲解还是自己看书摸索？",
            "你觉得哪种方式学起来最轻松高效？",
            "做练习题和看理论讲解，你更倾向哪个？",
            "你是喜欢有人带着学，还是自己研究？",
            "动手实践和看别人演示，你觉得哪个效果更好？"
        ],
        'learning_pace': [
            "你平时每天大概能抽多少时间学习？",
            "学习新内容，你是喜欢一次性学很多，还是每天学一点？",
            "你习惯提前做计划，还是临时抱佛脚？",
            "遇到难点时，你是会一直死磕还是先放一放？",
            "你更追求学习速度，还是注重理解透彻？"
        ],
        'metacognition': [
            "遇到完全看不懂的内容，你会怎么做？",
            "学完一个知识点后，你会怎么检验自己学会了？",
            "如果一个内容学了三遍还是不懂，你会怎么办？",
            "你会定期回顾学过的内容吗？用什么方式？",
            "你有什么独特的学习方法或者技巧吗？"
        ],
        'motivation': [
            "你学这些是为了考试、找工作，还是单纯感兴趣？",
            "是什么让你想提升自己？有什么目标吗？",
            "你有没有特别想掌握的技能或者想做的事？",
            "学习对你来说意味着什么？",
            "是什么一直在推动你持续学习？"
        ],
        'error_patterns': [
            "之前做练习时，哪些类型的题目你经常出错？",
            "有没有哪些公式或者概念你总觉得记不住？",
            "在学习过程中，你最容易在什么地方卡壳？",
            "哪些知识点你觉得特别容易混淆？",
            "有没有什么低级错误你经常犯？"
        ]
    }
    
    templates = question_templates.get(dimension_key, ["能说说你的情况吗？"])
    
    # 根据对话历史稍微调整问题
    if len(conversation_history) >= 2:
        return templates[(len(conversation_history) // 2) % len(templates)]
    
    # 随机选择一个问题
    import random
    return random.choice(templates)


def _confidence_from_profile(parsed) -> dict:
    """给每维一个置信度。优先用 SPIRES 抽取时 LLM 基于证据强度给的 _meta.confidence（证据驱动，
    非固定值）；没有 _meta 时退回"该维有没有内容"的粗略存在性判断，避免置信度维恒为 0。"""
    if not isinstance(parsed, dict):
        return {}
    meta = parsed.get('_meta') if isinstance(parsed.get('_meta'), dict) else {}
    conf = {}
    for key in ('knowledge_profile', 'cognitive_style', 'learning_goals', 'misconceptions', 'engagement', 'learning_preferences'):
        m = meta.get(key) if isinstance(meta.get(key), dict) else None
        if m is not None and 'confidence' in m:
            try:
                conf[key] = max(0.0, min(1.0, float(m.get('confidence') or 0.0)))
            except Exception:
                conf[key] = 0.0
            continue
        v = parsed.get(key)
        has = (len(v) > 0) if isinstance(v, (dict, list)) else bool(str(v or '').strip())
        if has:
            conf[key] = 0.75
    return conf


def _dialogue_mastery_prior(value):
    """把对话里自我报告的知识水平换算成一个保守的 0-1 先验掌握度。
    自我报告偏乐观且不可靠，压到 [0.1, 0.6]，之后由做题(BKT)向上/向下修正。"""
    if isinstance(value, dict):
        value = value.get('mastery_score', value.get('level'))
    if isinstance(value, bool):
        return 0.35
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1:
            v = v / 100.0
        return max(0.1, min(0.6, v))
    if isinstance(value, str):
        s = value.strip()
        table = {
            '零基础': 0.12, '完全不会': 0.12, '没学过': 0.12,
            '入门': 0.3, '初级': 0.3, '基础一般': 0.3, '刚开始': 0.3,
            '中级': 0.5, '有基础': 0.5, '学过': 0.45, '了解': 0.4,
            '熟悉': 0.6, '高级': 0.6, '精通': 0.6, '比较熟练': 0.6, '做过项目': 0.6,
        }
        for k, val in table.items():
            if k in s:
                return val
        return 0.35  # 提到了某知识点但没说清水平，给个保守默认
    return 0.35


def _seed_dialogue_knowledge(ap, profile_data):
    """把对话抽取到的知识点作为「冷启动初始值」播种进权威画像 A 的 knowledge_profile：
    只给「还没有任何掌握度记录」的知识点写入保守先验，绝不覆盖做题(BKT)已有的估计；
    做题后 BKT 会以这个先验为起点继续修正。同时给 knowledge_summary 的自由文本兜底抽标签。"""
    kp = (profile_data or {}).get('knowledge_profile')
    tags = {}
    if isinstance(kp, dict) and kp:
        for tag, val in kp.items():
            t = str(tag).strip()
            # 过滤内部键/overall/schema 关键字(大模型回显 schema 时的 type/description 等)
            if t and not _is_meta_kp_key(t):
                tags[t] = _dialogue_mastery_prior(val)
    else:
        # 大模型不可用时，从自由文本「知识基础」回答里抽领域关键词兜底
        summary = str((profile_data or {}).get('knowledge_summary') or '').strip()
        if summary:
            try:
                from agent_system.services.dialog_profile_builder import KnowledgeExtractor
                for t in KnowledgeExtractor.extract_knowledge_tags(summary):
                    tags[str(t).strip()] = _dialogue_mastery_prior(summary)
            except Exception:
                logger.exception('从知识基础文本抽取标签失败')

    if not tags:
        return
    cur = ap.knowledge_profile if isinstance(ap.knowledge_profile, dict) else {}
    ts = ap.knowledge_timestamps if isinstance(ap.knowledge_timestamps, dict) else {}
    now_iso = timezone.now().isoformat()
    seeded = False
    for t, prior in tags.items():
        if t and t not in cur:  # 只播种缺失项，不动 BKT 已有估计
            cur[t] = round(float(prior), 3)
            ts.setdefault(t, now_iso)
            seeded = True
    if seeded:
        ap.knowledge_profile = cur
        ap.knowledge_timestamps = ts


def _bridge_dialogue_profile_to_agent(user, profile_data, confidence=None):
    """把对话构建出的画像合并进权威画像 A（agent_system.StudentProfile），
    让对话结果真正被辅导/学习路径/测评消费；并推进冷启动。
    知识点作为冷启动初始值播种(只补缺失、不覆盖做题数据)，见 _seed_dialogue_knowledge。"""
    try:
        from agent_system.models import StudentProfile as AgentStudentProfile
        ap, _ = AgentStudentProfile.objects.get_or_create(user=user)

        cs = str((profile_data or {}).get('cognitive_style') or '').strip()
        if cs:
            ap.cognitive_style = cs[:100]

        # 知识点作为冷启动初始值播种（只补缺失、不覆盖做题数据）
        _seed_dialogue_knowledge(ap, profile_data)

        goals = [g for g in ((profile_data or {}).get('learning_goals') or []) if g]
        if goals:
            # 合并而非覆盖：保留做题/对话事件经 _merge_unique 攒进 A 的已有目标
            merged_goals = list(ap.learning_goals or [])
            for g in goals:
                if str(g).strip() and g not in merged_goals:
                    merged_goals.append(g)
            ap.learning_goals = merged_goals

        new_misc = [m for m in ((profile_data or {}).get('misconceptions') or []) if m]
        if new_misc:
            existing = list(ap.misconceptions or [])
            existing_texts = {
                (e if isinstance(e, str) else str((e or {}).get('concept') or (e or {}).get('text') or '')).strip()
                for e in existing
            }
            for m in new_misc:
                if str(m).strip() and str(m).strip() not in existing_texts:
                    existing.append(m)
            ap.misconceptions = existing

        prefs = (profile_data or {}).get('learning_preferences')
        if isinstance(prefs, dict) and prefs:
            merged = dict(ap.learning_preferences or {})
            merged.update(prefs)
            ap.learning_preferences = merged

        eng = (profile_data or {}).get('engagement')
        if isinstance(eng, dict) and str(eng.get('notes') or '').strip():
            merged_eng = dict(ap.engagement or {})
            merged_eng.setdefault('self_report', eng.get('notes'))  # 不覆盖行为算出的 score
            ap.engagement = merged_eng

        # 对话构建完成 → 推进冷启动（对话跑完即视为脱离冷启动）
        try:
            ap.cold_start_progress = min(1.0, max(float(ap.cold_start_progress or 0.0), 0.8))
            ap.is_cold_start = False
        except Exception:
            pass

        ap.save()
    except Exception:
        logger.exception('对话画像桥接到 agent_system 失败')


def _persist_profile_session(sid, uid):
    """会话完成后：解析对话 → 写 B（对话画像记录）→ 桥接进 A（权威画像）。
    step 与 stream 两条完成路径共用，避免"以跳过收尾"的会话因只有 stream 落库而整场丢失。"""
    try:
        s_obj = ProfileConversationSession.objects.get(pk=sid)
        profile_data, confidence = parse_profile_from_conversation(s_obj)
        user_obj = User.objects.get(pk=uid)
        StudentProfile.objects.update_or_create(
            user=user_obj,
            course_id='default',
            defaults={
                'profile_data': json.dumps(profile_data, ensure_ascii=False),
                'confidence_scores': json.dumps(confidence, ensure_ascii=False),
            }
        )
        _bridge_dialogue_profile_to_agent(user_obj, profile_data, confidence)
    except Exception:
        logger.exception('后台保存 StudentProfile 失败')


# Felder-Silverman 学习风格模型（FSLSM）四轴：认知风格结构化到这四个可测量维度上，
# 而不是一段自由文本。参见 Felder & Silverman；自适应学习里最常用的可测量学习风格模型。
_FSLSM_AXES = {
    'active_reflective': ('active', 'reflective', '主动实践↔反思观察'),
    'sensing_intuitive': ('sensing', 'intuitive', '具体感知↔抽象直觉'),
    'visual_verbal': ('visual', 'verbal', '视觉↔言语'),
    'sequential_global': ('sequential', 'global', '线性递进↔整体把握'),
}


def _robust_json(resp):
    """从 LLM 回复里稳健地取出 JSON 对象。"""
    if not resp:
        return None
    try:
        return json.loads(resp)
    except Exception:
        pass
    import re as _re
    m = _re.search(r'\{[\s\S]*\}', str(resp))
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _spires_extract_profile(conv, major=''):
    """SPIRES 式画像抽取（Structured Prompt Interrogation & Recursive Extraction of Semantics）：
    用 schema 约束 + 证据锚定，让 LLM 把整段对话抽成 6 维画像，每一维都带
    {值, evidence(学生原话引用), confidence(证据强度，非固定值)}；认知风格额外给 Felder-Silverman 四轴。
    没有对应话语的维度 → 低置信 + 标"待补充"，绝不编造（与防幻觉同一原则）。

    返回 (profile_data, confidence_scores)；spark 不可用或解析失败返回 None（交给上层回退）。
    参考：SPIRES(schema 约束零样本抽取)、USER-LLM(交互历史建模)、Felder-Silverman(学习风格)。
    """
    if not spark_client:
        return None
    # 只取"学生说过的话"作为证据来源，助手提问不作为画像依据
    student_turns = [str(m.get('content') or '').strip()
                     for m in (conv or []) if m.get('role') == 'user' and str(m.get('content') or '').strip()]
    if not student_turns:
        return None
    transcript = '\n'.join(f'- {t}' for t in student_turns[:20])
    major_line = f'（该生专业：{major}）' if major else ''
    system_prompt = (
        "你是学习者建模专家。请【只根据学生实际说过的话】，把下面这段对话抽取成结构化学生画像，"
        "严格输出符合给定 schema 的合法 JSON，不要任何解释。\n"
        "硬性规则：\n"
        "1. 每个维度都要给 evidence：从学生原话里引用能支撑该判断的片段（可截断）；没有能支撑的原话就留空数组。\n"
        "2. confidence 反映证据强度：说得明确充分→0.7~0.9；含糊/间接→0.4~0.6；学生根本没提到这一维→≤0.2。\n"
        "3. 【绝不编造】：学生没说的不要臆测；该维没证据就置信度给低分、值留空或写\"待补充\"。\n"
        "4. cognitive_style.fslsm 按 Felder-Silverman 四轴判断，每轴只能是给定两极之一或 \"unknown\"。\n\n"
        "schema（只输出这个对象）：\n"
        "{\n"
        '  "knowledge_profile": {"known_topics": {"知识点":"掌握程度(如 较熟练/入门/薄弱)"}, "evidence": ["原话"], "confidence": 0.0},\n'
        '  "cognitive_style": {"summary": "一句话概括学习偏好", "fslsm": {"active_reflective":"active|reflective|unknown", "sensing_intuitive":"sensing|intuitive|unknown", "visual_verbal":"visual|verbal|unknown", "sequential_global":"sequential|global|unknown"}, "evidence": ["原话"], "confidence": 0.0},\n'
        '  "learning_goals": {"goals": ["目标"], "evidence": ["原话"], "confidence": 0.0},\n'
        '  "misconceptions": {"items": ["易错点/薄弱点"], "evidence": ["原话"], "confidence": 0.0},\n'
        '  "engagement": {"score": 0, "notes": "节奏/时长/规律性描述", "evidence": ["原话"], "confidence": 0.0},\n'
        '  "learning_preferences": {"prefs": {"偏好项":"取值"}, "evidence": ["原话"], "confidence": 0.0}\n'
        "}"
    )
    try:
        resp = spark_client.get_response([
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': f'{major_line}\n学生在对话里说过：\n{transcript}'},
        ])
    except Exception:
        logger.exception('SPIRES 画像抽取调用失败')
        return None
    data = _robust_json(resp)
    if not isinstance(data, dict):
        return None

    def _conf(block):
        try:
            c = float((block or {}).get('confidence'))
        except Exception:
            c = 0.0
        return max(0.0, min(1.0, c))

    def _ev(block):
        return [str(e).strip() for e in ((block or {}).get('evidence') or []) if str(e).strip()][:4]

    kp_b = data.get('knowledge_profile') if isinstance(data.get('knowledge_profile'), dict) else {}
    cs_b = data.get('cognitive_style') if isinstance(data.get('cognitive_style'), dict) else {}
    lg_b = data.get('learning_goals') if isinstance(data.get('learning_goals'), dict) else {}
    mc_b = data.get('misconceptions') if isinstance(data.get('misconceptions'), dict) else {}
    en_b = data.get('engagement') if isinstance(data.get('engagement'), dict) else {}
    lp_b = data.get('learning_preferences') if isinstance(data.get('learning_preferences'), dict) else {}

    known = kp_b.get('known_topics') if isinstance(kp_b.get('known_topics'), dict) else {}
    goals = [str(g).strip() for g in (lg_b.get('goals') or []) if str(g).strip()]
    miscs = [str(m).strip() for m in (mc_b.get('items') or []) if str(m).strip()]
    try:
        eng_score = int(float(en_b.get('score') or 0))
    except Exception:
        eng_score = 0
    prefs = lp_b.get('prefs') if isinstance(lp_b.get('prefs'), dict) else {}
    fslsm = cs_b.get('fslsm') if isinstance(cs_b.get('fslsm'), dict) else {}
    # 只保留合法的四轴取值
    fslsm_clean = {}
    for axis, (p1, p2, _label) in _FSLSM_AXES.items():
        v = str(fslsm.get(axis) or '').strip().lower()
        fslsm_clean[axis] = v if v in (p1, p2) else 'unknown'

    # 扁平结构：保持既有 6 键与形状不变，桥接/雷达/播种无需改动
    profile_data = {
        'knowledge_profile': {str(k).strip(): v for k, v in known.items() if str(k).strip()},
        'cognitive_style': str(cs_b.get('summary') or '').strip(),
        'learning_goals': goals,
        'misconceptions': miscs,
        'engagement': {'score': max(0, min(100, eng_score)), 'notes': str(en_b.get('notes') or '').strip()},
        'learning_preferences': {str(k): v for k, v in prefs.items()},
        # 新增：SPIRES 证据 + 每维置信度 + FSLSM 四轴（OLM 可解释展示 & 证据驱动置信度用）
        '_meta': {
            'source': 'spires_llm',
            'knowledge_profile': {'evidence': _ev(kp_b), 'confidence': _conf(kp_b)},
            'cognitive_style': {'evidence': _ev(cs_b), 'confidence': _conf(cs_b), 'fslsm': fslsm_clean},
            'learning_goals': {'evidence': _ev(lg_b), 'confidence': _conf(lg_b)},
            'misconceptions': {'evidence': _ev(mc_b), 'confidence': _conf(mc_b)},
            'engagement': {'evidence': _ev(en_b), 'confidence': _conf(en_b)},
            'learning_preferences': {'evidence': _ev(lp_b), 'confidence': _conf(lp_b)},
        },
    }
    confidence_scores = {dim: profile_data['_meta'][dim]['confidence'] for dim in
                         ('knowledge_profile', 'cognitive_style', 'learning_goals',
                          'misconceptions', 'engagement', 'learning_preferences')}
    return profile_data, confidence_scores


def parse_profile_from_conversation(session):
    """根据会话历史解析画像：优先 SPIRES 式证据锚定抽取，其次旧版 LLM/启发式映射。

    返回 (profile_data_dict, confidence_scores_dict)
    """
    conv = json.loads(session.conversation_history or '[]')
    asked_dims = json.loads(session.asked_dimensions or '[]')
    answered_dims = json.loads(session.answered_dimensions or '[]')
    skipped_dims = json.loads(session.skipped_dimensions or '[]')

    # 首选：SPIRES 式证据锚定抽取（schema 约束 + 每维 evidence/confidence + FSLSM）。
    # 至少要抽到一维有实际证据才采纳，否则落到旧版逻辑，避免"空壳画像"。
    major = ''
    try:
        major = (getattr(getattr(session, 'user', None), 'major', '') or '').strip()
    except Exception:
        major = ''
    try:
        spires = _spires_extract_profile(conv, major=major)
        if spires:
            pd, conf = spires
            if any((v or 0) >= 0.3 for v in (conf or {}).values()):
                return pd, conf
    except Exception:
        logger.exception('SPIRES 画像抽取失败，回退旧逻辑')

    # 回退：旧版 spark_client 结构化 JSON
    if spark_client:
        try:
            # 严格 JSON schema 提示（第一轮）
            system_prompt = (
                "你是一个教学助手。任务：根据下面的对话生成学生画像的 JSON。必须严格遵循下面的 schema 并且只输出合法的 JSON，对话之外不要输出任何解释或评论。\n"
                "schema: {\n"
                "  \"knowledge_profile\": {\"type\": \"object\", \"description\": \"{知识点: 掌握程度（0-100 或 文本）}\"},\n"
                "  \"cognitive_style\": {\"type\": \"string\"},\n"
                "  \"learning_goals\": {\"type\": \"array\", \"items\": {\"type\": \"string\"}},\n"
                "  \"misconceptions\": {\"type\": \"array\", \"items\": {\"type\": \"string\"}},\n"
                "  \"engagement\": {\"type\": \"object\", \"description\": \"{score:0-100, notes:string}\"},\n"
                "  \"learning_preferences\": {\"type\": \"object\"}\n"
                "}\n"
            )
            user_prompt = '对话: ' + json.dumps(conv, ensure_ascii=False)
            messages = [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}]
            resp = spark_client.get_response(messages)
            if resp:
                # 尝试直接解析 JSON
                try:
                    parsed = json.loads(resp)
                    parsed = _sanitize_parsed_profile(parsed)
                    return parsed, _confidence_from_profile(parsed)
                except Exception:
                    # 提取文本中的 JSON 子串再尝试解析
                    import re
                    m = re.search(r'\{[\s\S]*\}', resp)
                    if m:
                        try:
                            parsed = json.loads(m.group(0))
                            parsed = _sanitize_parsed_profile(parsed)
                            return parsed, _confidence_from_profile(parsed)
                        except Exception:
                            pass

            # 第二轮回退：请求模型按键值对返回每个维度的简短文本（便于解析）
            try:
                follow_prompt = (
                    "如果上面的输出无法解析，请仅以一行一个键值对的格式回复（例如：knowledge_profile: {...}），"
                    "键按 order: knowledge_profile, cognitive_style, learning_goals, misconceptions, engagement, learning_preferences。"
                )
                messages2 = [{'role': 'system', 'content': follow_prompt}, {'role': 'user', 'content': user_prompt}]
                resp2 = spark_client.get_response(messages2)
                if resp2:
                    # 尝试解析为 JSON 或键值对
                    try:
                        parsed = json.loads(resp2)
                        parsed = _sanitize_parsed_profile(parsed)
                        return parsed, _confidence_from_profile(parsed)
                    except Exception:
                        # 解析键值对形式
                        parsed = {}
                        for line in resp2.splitlines():
                            if ':' in line:
                                k, v = line.split(':', 1)
                                k = k.strip()
                                v = v.strip()
                                # 尝试解析 JSON 值
                                try:
                                    parsed[k] = json.loads(v)
                                except Exception:
                                    parsed[k] = v
                        if parsed:
                            parsed = _sanitize_parsed_profile(parsed)
                            return parsed, _confidence_from_profile(parsed)
            except Exception:
                logger.exception('spark_client follow-up parse failed')
        except Exception:
            logger.exception('spark_client parse failed')

    # 回退：启发式映射
    # 首选：从 profile_building_answer 事件按维度取答案。事件记录了每条回答对应的 dimension，
    # 追问的答复也归到同一维度(last-wins=追问澄清后的答案)，不会像"按顺序 zip conv"那样一旦
    # 出现追问就整体错位。
    mapped = {}
    try:
        from agent_system.models import ProfileEvent as _AgentProfileEvent
        _events = _AgentProfileEvent.objects.filter(
            user=session.user, event_type='profile_building_answer',
        ).order_by('created_at')
        for _ev in _events:
            _p = _ev.payload if isinstance(_ev.payload, dict) else {}
            if _p.get('session_id') == session.id and _p.get('dimension'):
                _txt = str(_p.get('text') or '').strip()
                if _txt:
                    mapped[_p['dimension']] = _txt
    except Exception:
        logger.exception('从事件重建维度答案失败，改用按序映射')

    # 次选：事件缺失时，按 asked_dims 与答复顺序对齐（无追问时正确）
    if not mapped:
        answers = []
        for i, m in enumerate(conv):
            if m.get('role') == 'assistant':
                if i + 1 < len(conv) and conv[i + 1].get('role') == 'user':
                    answers.append(conv[i + 1].get('content'))
        for idx, dim in enumerate(asked_dims):
            if idx < len(answers):
                mapped[dim] = str(answers[idx] or '').strip()

    # 正确地把提问维度(A套: knowledge_base/cognitive_style/learning_pace/metacognition/motivation/error_patterns)
    # 映射到画像字段(B套)，避免此前 learning_goals 取了根本不存在的键、其余维度答案被整体丢弃。
    profile_data = {
        'knowledge_profile': {},  # 自由文本无法可靠抽标签，另存到 knowledge_summary
        'knowledge_summary': mapped.get('knowledge_base', ''),
        'cognitive_style': mapped.get('cognitive_style', ''),
        'learning_goals': [mapped['motivation']] if mapped.get('motivation') else [],
        'misconceptions': [mapped['error_patterns']] if mapped.get('error_patterns') else [],
        'engagement': {
            'score': 60 if mapped.get('metacognition') else 0,
            'notes': mapped.get('metacognition', ''),
        },
        'learning_preferences': {'pace': mapped['learning_pace']} if mapped.get('learning_pace') else {},
    }

    # 每个"有答案"的维度给出置信度（键用 B 套字段名，供雷达"画像置信度"维度使用）
    confidence = {}
    if mapped.get('knowledge_base'):
        confidence['knowledge_profile'] = 0.5
    if mapped.get('cognitive_style'):
        confidence['cognitive_style'] = 0.7
    if mapped.get('motivation'):
        confidence['learning_goals'] = 0.7
    if mapped.get('error_patterns'):
        confidence['misconceptions'] = 0.7
    if mapped.get('metacognition'):
        confidence['engagement'] = 0.5
    if mapped.get('learning_pace'):
        confidence['learning_preferences'] = 0.6
    return profile_data, confidence


@login_required
def profile_building_stream(request):
    """流式 SSE 接口：前端通过 EventSource 订阅该接口以实时接收 AI 的问题（或回复）。

    GET 参数:
    - sessionId: 会话 ID
    - message: 用户提交的回答（可选）
    - action: 'skip' 表示跳过当前问题
    """
    if request.method != 'GET':
        return JsonResponse({'error': '仅支持 GET'}, status=405)

    session_id = request.GET.get('sessionId') or request.GET.get('session_id')
    action = request.GET.get('action', 'answer')
    user_message = request.GET.get('message', '')

    session = get_object_or_404(ProfileConversationSession, id=session_id, user=request.user)
    def chunk_text(s, size=80):
        for i in range(0, len(s), size):
            yield s[i:i+size]

    def event_stream():
        try:
            # 重新从数据库加载会话，确保获取刚刚通过 POST 保存的用户消息
            session.refresh_from_db()
            asked_dims = json.loads(session.asked_dimensions or '[]')
            answered_dims = json.loads(session.answered_dimensions or '[]')
            skipped_dims = json.loads(session.skipped_dimensions or '[]')
            conv = json.loads(session.conversation_history or '[]')

            # 生成 AI 问题/回答（基于最新的会话状态）：可能是新问题，也可能是对当前维度的追问/举例
            ai_q, is_stay, target_key, kind = generate_next_question(conv, answered_dims, skipped_dims, asked_dims, user=request.user)

            # 把 AI 回答追加到会话，并标记它对应的维度与类型（供按维度计数追问/举例次数）
            conv.append({'role': 'assistant', 'content': ai_q, 'dim': target_key, 'kind': kind})

            # 更新维度状态：停留(追问/举例)时保持当前维度；推进时标记当前已回答，并前进到 target_key
            if is_stay:
                next_dim = asked_dims[-1] if asked_dims else None
            else:
                if asked_dims:
                    current_dim = asked_dims[-1]
                    if current_dim not in answered_dims:
                        answered_dims.append(current_dim)
                next_dim = target_key  # generate_next_question 已算好「当前维度之后」的下一个维度
                if next_dim and next_dim not in asked_dims:
                    asked_dims.append(next_dim)
            # 始终增加轮次（包括追问/举例），防止绕不出去
            session.current_round += 1

            session.asked_dimensions = json.dumps(asked_dims)
            session.conversation_history = json.dumps(conv)
            session.answered_dimensions = json.dumps(answered_dims)
            session.skipped_dimensions = json.dumps(skipped_dims)
            session.save()

            # 将回复分块为 SSE data 事件发送
            for part in chunk_text(ai_q, 80):
                yield f"data: {part}\n\n"

            # 发送 meta 事件，包含维度进度与轮次信息
            _all_dims = ['knowledge_base', 'cognitive_style', 'learning_pace', 'metacognition', 'motivation', 'error_patterns']
            completed_dims = len(answered_dims) + len(skipped_dims)
            meta = {
                'nextDimension': next_dim,
                'currentRound': session.current_round,
                'completedDimensions': completed_dims,
                'totalDimensions': len(_all_dims),
                'isFollowup': is_stay,
                'kind': kind,
            }
            yield f"event: meta\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"

            # 会话完成：所有维度已覆盖，或达到硬上限（防御性）
            _all_dims_count = 6
            _dims_done = len(answered_dims) + len(skipped_dims)
            if _dims_done >= _all_dims_count or session.current_round >= 14:
                session.status = 'completed'
                session.save()

                # 后台线程解析并保存 StudentProfile，避免阻塞 SSE 响应
                try:
                    t = threading.Thread(
                        target=_persist_profile_session,
                        args=(session.id, request.user.id),
                        daemon=True,
                    )
                    t.start()
                except Exception:
                    logger.exception('启动后台解析线程失败')

                yield f"event: completed\ndata: {json.dumps({'message': '会话完成'})}\n\n"

        except Exception as e:
            logger.exception('profile_building_stream error')
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingHttpResponse(event_stream(), content_type='text/event-stream')