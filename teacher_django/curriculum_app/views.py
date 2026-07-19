from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .forms import TeacherCourseForm, CourseMaterialForm
from .models import CourseOutline, LearningProgress, LearningPlan, OutlineExport, Course, CourseMaterial, MaterialChunk, MaterialQuizAttempt, MaterialQuestionStat, MaterialWeakAreaArchive, MaterialQuizAdaptivePolicy
from .tasks import enqueue_course_material_processing
from django.http import JsonResponse, StreamingHttpResponse, FileResponse, Http404, HttpResponseRedirect
from django.conf import settings
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db import models, transaction
from django.urls import reverse
import json
import time
import logging
import re
import hashlib
import unicodedata
from datetime import timedelta
from agent_system.services.profile_events import record_profile_event

logger = logging.getLogger(__name__)
import os

try:
    from agent_system.models import AgentTask, LearningResource
except Exception:
    AgentTask = None
    LearningResource = None

try:
    from agent_system.models import Conversation, Message
except Exception:
    Conversation = None
    Message = None


def _safe_json_loads(value, default):
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _normalize_task_mode_label(output_data):
    output_data = output_data if isinstance(output_data, dict) else {}
    launch_mode = str(output_data.get('launch_mode') or '').strip().lower()
    if launch_mode == 'subprocess':
        return '子进程执行'
    if launch_mode == 'thread_fallback':
        return '线程回退'
    return '同步直出'


def _format_adjustment_trigger_source(trigger_source):
    source = str(trigger_source or '').strip().lower()
    label_map = {
        'material-quiz': '资料小测触发',
        'manual-refresh': '手动重排触发',
        'weak-area-refresh': '薄弱点重排触发',
    }
    return label_map.get(source) or ('评估信号触发' if source else '')


def _load_unified_profile_snapshot(user):
    snapshot = {
        'source': 'none',
        'knowledge_profile': {},
        'cognitive_style': '',
        'learning_goals': [],
        'misconceptions': [],
        'engagement': {},
        'learning_preferences': {},
    }
    if not user or not getattr(user, 'is_authenticated', False):
        return snapshot

    # 合并两套画像：agent_system(A，做题/BKT 驱动) 是知识掌握的权威来源，
    # profile_app(B，对话构建) 补充认知风格/目标/偏好等对话维度。
    # 之前只要 B 存在就直接返回，导致空白对话画像遮蔽了 A 里真实的做题掌握度。
    a = getattr(user, 'student_profile', None)
    b_data = {}
    try:
        from profile_app.models import StudentProfile as ProfileAppStudentProfile
        stored_b = ProfileAppStudentProfile.objects.filter(user=user, course_id='default').order_by('-last_updated').first()
        if stored_b:
            parsed = _safe_json_loads(getattr(stored_b, 'profile_data', ''), {})
            b_data = parsed if isinstance(parsed, dict) else {}
    except Exception:
        b_data = {}

    if not a and not b_data:
        return snapshot

    def _pick(a_val, b_val):
        return a_val if a_val else b_val

    a_knowledge = a.knowledge_profile if (a and isinstance(a.knowledge_profile, dict)) else {}
    b_knowledge = b_data.get('knowledge_profile') if isinstance(b_data.get('knowledge_profile'), dict) else {}
    a_eng = a.engagement if (a and isinstance(a.engagement, dict)) else {}
    b_eng = b_data.get('engagement') if isinstance(b_data.get('engagement'), dict) else {}
    a_pref = a.learning_preferences if (a and isinstance(a.learning_preferences, dict)) else {}
    b_pref = b_data.get('learning_preferences') if isinstance(b_data.get('learning_preferences'), dict) else {}

    snapshot.update({
        'source': 'merged',
        'knowledge_profile': _pick(a_knowledge, b_knowledge),  # 做题数据优先，A 空才退回 B
        'cognitive_style': _pick(str(getattr(a, 'cognitive_style', '') or '').strip(), str(b_data.get('cognitive_style') or '').strip()),
        'learning_goals': _pick(list(getattr(a, 'learning_goals', []) or []), list(b_data.get('learning_goals') or [])),
        'misconceptions': _pick(list(getattr(a, 'misconceptions', []) or []), list(b_data.get('misconceptions') or [])),
        'engagement': _pick(a_eng, b_eng),
        'learning_preferences': _pick(a_pref, b_pref),
    })
    return snapshot


def _match_query_course(user, query):
    tokens = {
        token for token in re.split(r'[^\w\u4e00-\u9fff]+', str(query or '').lower())
        if len(token) >= 2
    }
    if not tokens:
        return _course_queryset_for_user(user).order_by('-updated_at').first()

    best_course = None
    best_score = 0
    for course in _course_queryset_for_user(user).order_by('-updated_at')[:12]:
        haystack = ' '.join([
            str(course.title or ''),
            str(course.summary or ''),
            str(course.description or ''),
            str(course.tags or ''),
        ]).lower()
        score = sum(1 for token in tokens if token in haystack)
        if score > best_score:
            best_course = course
            best_score = score
    return best_course or _course_queryset_for_user(user).order_by('-updated_at').first()


def _build_course_knowledge_snapshot(course):
    empty = {
        'material_count': 0,
        'chunk_count': 0,
        'topics': [],
        'headings': [],
        'summary': '',
    }
    if not course:
        return empty

    materials = list(CourseMaterial.objects.filter(course=course).order_by('display_order', 'created_at')[:20])
    chunks = list(MaterialChunk.objects.filter(material__course=course).select_related('material').order_by('material_id', 'chunk_index')[:120])

    topics = []
    headings = []
    seen_topics = set()
    seen_headings = set()
    for chunk in chunks:
        heading = (chunk.heading or '').strip()
        if heading and heading not in seen_headings:
            seen_headings.add(heading)
            headings.append(heading)
        for raw in re.split(r'[/,，;；]+', chunk.keyword_summary or ''):
            topic = raw.strip()
            if len(topic) < 2:
                continue
            lowered = topic.lower()
            if lowered in seen_topics:
                continue
            seen_topics.add(lowered)
            topics.append(topic)
            if len(topics) >= 8:
                break
        if len(topics) >= 8 and len(headings) >= 6:
            break

    if not topics:
        for material in materials:
            title = (material.title or '').strip()
            if title and title.lower() not in seen_topics:
                seen_topics.add(title.lower())
                topics.append(title)
                if len(topics) >= 6:
                    break

    summary_parts = []
    if materials:
        summary_parts.append(f'本课程共 {len(materials)} 份资料')
    if chunks:
        summary_parts.append(f'已解析 {len(chunks)} 个知识片段')
    if topics:
        summary_parts.append('重点主题：' + '、'.join(topics[:6]))
    elif headings:
        summary_parts.append('主要结构：' + '、'.join(headings[:5]))

    return {
        'material_count': len(materials),
        'chunk_count': len(chunks),
        'topics': topics[:8],
        'headings': headings[:6],
        'summary': '；'.join(summary_parts),
    }


def _sanitize_plan_modules(modules):
    """校验/清洗 LLM 生成的学习路径 modules，结构不对就返回 None（回退模板）。"""
    if not isinstance(modules, list):
        return None
    valid_res = {'doc', 'ppt', 'quiz', 'animation', 'code', 'mindmap', 'reading'}
    out = []
    for mod in modules[:5]:
        if not isinstance(mod, dict):
            continue
        name = str(mod.get('name') or '').strip()
        if not name:
            continue
        lessons = []
        for les in (mod.get('lessons') or [])[:4]:
            if not isinstance(les, dict):
                continue
            lt = str(les.get('title') or '').strip()
            if not lt:
                continue
            res = [r for r in (les.get('resources') or []) if r in valid_res][:4] or ['doc']
            lessons.append({'title': lt, 'objectives': str(les.get('objectives') or '').strip(), 'resources': res})
        if not lessons:
            continue
        try:
            hrs = float(mod.get('estimated_hours') or 0) or 2.0
        except (TypeError, ValueError):
            hrs = 2.0
        out.append({'name': name, 'estimated_hours': round(max(0.5, min(hrs, 10.0)), 1),
                    'focus': str(mod.get('focus') or '').strip(), 'lessons': lessons})
    return out if len(out) >= 2 else None


def _llm_plan_modules(title, learning_goals, weak_areas, preferred_mode, misconceptions, engagement_score, course_topics):
    """用大模型按学生画像真正编排学习路径的阶段/单元；失败返回 None（回退固定模板）。"""
    try:
        from core.xunfei_spark import spark_client
        if not spark_client:
            return None
        prompt = (
            f'你是一名学习规划专家。请根据下面的学生画像，为主题"{title}"编排一条**个性化学习路径**：\n'
            '要求：\n'
            '1) 3-4 个循序渐进的阶段，每阶段 1-3 个学习单元；阶段顺序与单元内容必须真正结合画像来定制'
            '（薄弱点优先补、贴合学习目标与偏好），不要套"搭框架/案例练习/复盘"这种通用三段模板；\n'
            '2) 每个单元指明推荐资源类型（只能从 doc/ppt/quiz/animation/code/mindmap/reading 里选）；\n'
            '3) objectives 写"学完这个单元能做到什么"，要具体到该主题；\n'
            '4) 只输出 JSON：{"modules":[{"name":"阶段名","estimated_hours":3.5,"focus":"本阶段目标",'
            '"lessons":[{"title":"单元标题","objectives":"学完能做到什么","resources":["doc","quiz"]}]}]}\n\n'
            f'【学生画像】\n学习目标：{("、".join(learning_goals)) or "未明确"}\n'
            f'当前薄弱点(优先补)：{("、".join(weak_areas)) or "暂无"}\n'
            f'学习偏好：{preferred_mode}\n易错点：{("、".join(misconceptions)) or "暂无"}\n'
            f'参与度评分：{engagement_score if engagement_score is not None else "未知"}\n'
            f'课程重点主题：{("、".join(course_topics)) or "无"}'
        )
        if hasattr(spark_client, 'get_response'):
            resp = spark_client.get_response([{'role': 'user', 'content': prompt}])
        else:
            resp = spark_client.generate_text(prompt)
        if not resp or str(resp).lstrip().startswith('[占位'):
            return None
        import re as _re
        m = _re.search(r'\{[\s\S]*\}', resp)
        data = json.loads(m.group(0)) if m else json.loads(resp)
        modules = data.get('modules') if isinstance(data, dict) else data
        return _sanitize_plan_modules(modules)
    except Exception:
        logger.exception('LLM 学习路径编排失败，回退模板')
        return None


def _build_personalized_learning_plan(query, user, use_llm=True):
    title = _normalize_course_topic(query) or str(query or '').strip() or '个性化学习路线'
    profile = _load_unified_profile_snapshot(user)
    matched_course = _match_query_course(user, title)
    course_map = _build_course_knowledge_snapshot(matched_course)

    progress_list = list(
        LearningProgress.objects.filter(user=user)
        .select_related('course_outline')
        .order_by('-last_accessed_at')[:6]
    )
    weak_areas = list(
        MaterialQuestionStat.objects.filter(user=user)
        .order_by('-wrong_count', '-last_seen_at')
        .values_list('knowledge_tag', flat=True)[:4]
    )
    weak_areas = [item for item in weak_areas if item]

    learning_goals = [str(item).strip() for item in profile.get('learning_goals') or [] if str(item).strip()]
    preferences = profile.get('learning_preferences') or {}
    preferred_mode = str(preferences.get('preferred_mode') or profile.get('cognitive_style') or '讲练结合').strip() or '讲练结合'
    misconceptions = [str(item).strip() for item in profile.get('misconceptions') or [] if str(item).strip()]
    engagement = profile.get('engagement') if isinstance(profile.get('engagement'), dict) else {}
    engagement_score = engagement.get('score') if isinstance(engagement.get('score'), (int, float)) else None

    # 优先让大模型按画像真正编排阶段/单元；失败或关闭时回退固定三段模板
    plan_source = 'template'
    modules = _llm_plan_modules(
        title, learning_goals, weak_areas, preferred_mode, misconceptions, engagement_score,
        course_map.get('topics') or [],
    ) if use_llm else None
    if modules:
        plan_source = 'llm'

    stage_one_lessons = [
        {
            'title': f'先建立“{title}”的整体框架',
            'objectives': '先回答这门内容解决什么问题、核心概念有哪些、后续每一步怎么学。',
            'resources': ['doc', 'ppt'],
        },
        {
            'title': f'围绕“{title}”抓住关键术语和基础定义',
            'objectives': '把高频概念、判断口径和常见混淆点先理顺，避免一开始就碎片化。',
            'resources': ['doc', 'quiz'],
        },
    ]
    stage_two_lessons = [
        {
            'title': '通过例题或案例把抽象内容落地',
            'objectives': '把概念和步骤放进具体场景里，形成可复用的判断方法。',
            'resources': ['ppt', 'animation', 'quiz'],
        },
        {
            'title': '针对当前薄弱点做专项巩固',
            'objectives': '优先补最近错得最多或最容易混淆的知识点，而不是平均用力。',
            'resources': ['quiz', 'doc', 'code'],
        },
    ]
    stage_three_lessons = [
        {
            'title': '做一次综合练习并复盘错因',
            'objectives': '检验是否已经会解释、会判断、会应用，并把错因回写到后续计划。',
            'resources': ['quiz', 'doc'],
        },
    ]

    if not modules:
        modules = [
            {
                'name': '阶段一：先搭框架再进入细节',
                'estimated_hours': 3.5,
                'focus': '建立全局认知，避免一开始只记零散知识点。',
                'lessons': stage_one_lessons,
            },
            {
                'name': '阶段二：结合案例和练习完成理解迁移',
                'estimated_hours': 4.0,
                'focus': '把定义、步骤、例题和误区连起来，形成会用的知识。',
                'lessons': stage_two_lessons,
            },
            {
                'name': '阶段三：通过复盘形成稳定掌握',
                'estimated_hours': 2.0,
                'focus': '用综合练习和错因分析，确认这部分内容是否真正学会。',
                'lessons': stage_three_lessons,
            },
        ]

        # 固定模板才需要把画像信号"注入"到对应位置；LLM 版本已按画像编排，无需注入
        if matched_course and course_map.get('topics'):
            modules[0]['lessons'][0]['objectives'] += ' 当前课程资料里的重点主题包括：' + '、'.join(course_map['topics'][:4]) + '。'
        if weak_areas:
            modules[1]['lessons'][1]['objectives'] += ' 当前优先薄弱点：' + '、'.join(weak_areas[:3]) + '。'
        if learning_goals:
            modules[2]['focus'] += ' 当前学生目标：' + '；'.join(learning_goals[:2]) + '。'

    progress_summary = []
    for item in progress_list[:3]:
        progress_summary.append({
            'outline_id': item.course_outline_id,
            'outline_title': item.course_outline.title,
            'chapter_id': item.chapter_id,
            'status': item.status,
            'quiz_score': item.quiz_score,
        })

    recommendation_reason = [f'讲解方式优先按“{preferred_mode}”组织。']
    if misconceptions:
        recommendation_reason.append('需要额外关注的易错点：' + '、'.join(misconceptions[:3]) + '。')
    if engagement_score is not None:
        if engagement_score < 40:
            recommendation_reason.append('参与度偏低，前两阶段应缩短单次学习时长并增加即时反馈。')
        elif engagement_score >= 75:
            recommendation_reason.append('参与度较高，可在第二阶段加入更完整的综合练习或拓展案例。')
    if matched_course:
        recommendation_reason.append(f'本次优先结合课程“{matched_course.title}”已有资料来安排学习顺序。')

    return {
        'title': title,
        'description': f'围绕“{title}”生成的个性化学习路径，优先结合画像、课程资料和当前学习进度安排学习顺序。',
        'plan_source': plan_source,
        'profile_summary': {
            'source': profile.get('source'),
            'cognitive_style': profile.get('cognitive_style') or '未明确',
            'preferred_mode': preferred_mode,
            'learning_goals': learning_goals[:3],
            'misconceptions': misconceptions[:3],
            'engagement_score': engagement_score,
        },
        'matched_course': {
            'id': matched_course.id,
            'title': matched_course.title,
            'summary': course_map.get('summary') or (matched_course.summary or ''),
            'topics': course_map.get('topics') or [],
        } if matched_course else None,
        'recent_progress': progress_summary,
        'weak_areas': weak_areas[:4],
        'recommendation_reason': recommendation_reason,
        'modules': modules,
    }


def _build_refreshed_learning_plan(plan, user):
    source_data = _safe_json_loads(getattr(plan, 'plan_data', ''), {})
    _ADJUST_SUFFIX = '（已按当前状态调整）'
    raw_title = (
        source_data.get('title')
        if isinstance(source_data, dict) and source_data.get('title')
        else getattr(plan, 'title', '')
    )
    # 剥掉可能已有的"（已按当前状态调整）"后缀，避免反复刷新时后缀层层叠加
    query = str(raw_title or '').replace(_ADJUST_SUFFIX, '').strip()
    # 刷新走 use_llm=False：这是做题后触发的重排，要快；不重新调大模型编排。
    refreshed = _build_personalized_learning_plan(query, user, use_llm=False)
    # 保留原计划（可能是大模型按画像编排的）阶段作为主线，避免刷新时退化成通用模板；
    # 但要先剔除上一次刷新已前插的"阶段0：先补当前薄弱点"，否则每次刷新会层层累积补弱阶段。
    if isinstance(source_data, dict) and isinstance(source_data.get('modules'), list) and source_data.get('modules'):
        refreshed['modules'] = [
            m for m in source_data['modules']
            if not str((m or {}).get('name', '')).startswith('阶段0')
        ]
        if source_data.get('plan_source'):
            refreshed['plan_source'] = source_data['plan_source']

    weak_areas = [str(item).strip() for item in refreshed.get('weak_areas') or [] if str(item).strip()]
    recent_progress = refreshed.get('recent_progress') if isinstance(refreshed.get('recent_progress'), list) else []
    low_score_progress = [item for item in recent_progress if isinstance(item, dict) and (item.get('quiz_score') or 0) < 80]

    if weak_areas:
        remediation_module = {
            'name': '阶段0：先补当前薄弱点',
            'estimated_hours': 1.5,
            'focus': '先把最近错误最多、最容易反复出错的知识点补齐，再进入主线学习。当前优先薄弱点：' + '、'.join(weak_areas[:3]) + '。',
            'lessons': [
                {
                    'title': '回看对应资料并重新建立概念锚点',
                    'objectives': '先回到和薄弱点最相关的资料页，把核心定义、判断口径和易错边界重新理顺。',
                    'resources': ['doc', 'ppt', 'animation'],
                },
                {
                    'title': '围绕当前薄弱点做一轮针对性练习',
                    'objectives': '优先重做最容易错的知识点，而不是平均分配练习时间。',
                    'resources': ['quiz', 'doc'],
                },
            ],
        }
        refreshed['modules'] = [remediation_module] + list(refreshed.get('modules') or [])
        refreshed['recommendation_reason'] = [
            '检测到当前薄弱点：' + '、'.join(weak_areas[:3]) + '，已将补弱阶段提前。'
        ] + list(refreshed.get('recommendation_reason') or [])

    if low_score_progress:
        latest = low_score_progress[0]
        refreshed['recommendation_reason'].append(
            f"最近一次相关练习得分为 {latest.get('quiz_score')}，建议先修正错因再继续推进后续阶段。"
        )

    _base_title = str(refreshed.get('title') or getattr(plan, 'title', '个性化学习路线')).replace(_ADJUST_SUFFIX, '').strip()
    refreshed['title'] = _base_title + _ADJUST_SUFFIX
    refreshed['description'] = (
        str(refreshed.get('description') or '').strip()
        + ' 系统已结合最近小测表现、薄弱点和当前学习进度重新排列优先级。'
    ).strip()
    refreshed['adjustment_meta'] = {
        'source_plan_id': getattr(plan, 'id', None),
        'adjustment_type': 'weak-area-prioritized',
        'adjusted_at': timezone.now().isoformat(),
        'weak_area_count': len(weak_areas),
        'recent_low_score_count': len(low_score_progress),
    }
    return refreshed


def _resolve_auto_adjust_base_plan(user, course=None):
    if not user or not getattr(user, 'is_authenticated', False):
        return None

    snapshots = []
    if course is not None:
        snapshots.append(_get_latest_course_plan_snapshot(user, course=course))
    snapshots.append(_get_latest_course_plan_snapshot(user))

    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        plan_id = snapshot.get('plan_id')
        if not plan_id:
            continue
        plan = LearningPlan.objects.filter(id=plan_id, user=user, status='generated').first()
        if plan:
            return plan
    return None


def _maybe_auto_refresh_learning_plan_from_quiz(user, course, material, attempt_record, practice_insights):
    if not attempt_record:
        return None

    review_recommendations = []
    if isinstance(practice_insights, dict):
        review_recommendations = list(practice_insights.get('review_recommendations') or [])
    score = float(getattr(attempt_record, 'score', 0) or 0)
    low_score = score < 80
    has_review_signal = bool(review_recommendations)
    if not low_score and not has_review_signal:
        return None

    base_plan = _resolve_auto_adjust_base_plan(user, course=course)
    if not base_plan:
        return None

    base_payload = _safe_json_loads(base_plan.plan_data, {})
    if isinstance(base_payload, dict):
        adjustment_meta = base_payload.get('adjustment_meta') if isinstance(base_payload.get('adjustment_meta'), dict) else {}
        if str(adjustment_meta.get('trigger_attempt_id') or '').strip() == str(getattr(attempt_record, 'id', '')):
            return None
        # 已经是自动重排结果时，短时间内不重复重排，避免一次练习会话连续刷出多个新路径。
        if str(adjustment_meta.get('trigger_source') or '').strip() == 'material-quiz':
            same_material = str(adjustment_meta.get('trigger_material_id') or '') == str(getattr(material, 'id', ''))
            adjusted_at_raw = str(adjustment_meta.get('adjusted_at') or '').strip()
            adjusted_at = parse_datetime(adjusted_at_raw) if adjusted_at_raw else None
            if adjusted_at and timezone.is_naive(adjusted_at):
                adjusted_at = timezone.make_aware(adjusted_at, timezone.get_current_timezone())
            if same_material and adjusted_at:
                if timezone.now() - adjusted_at < timedelta(minutes=30):
                    return None

    refreshed_plan_data = _build_refreshed_learning_plan(base_plan, user)
    refreshed_reasons = [
        str(item).strip() for item in (refreshed_plan_data.get('recommendation_reason') or []) if str(item).strip()
    ]

    if has_review_signal:
        lead_reason = f'检测到《{material.title}》小测得分 {score:.1f}，并出现 {len(review_recommendations)} 条回看建议，已自动触发路径重排。'
    else:
        lead_reason = f'检测到《{material.title}》小测得分 {score:.1f}，低于 80 分阈值，已自动触发路径重排。'
    refreshed_plan_data['recommendation_reason'] = [lead_reason] + refreshed_reasons

    meta = refreshed_plan_data.get('adjustment_meta') if isinstance(refreshed_plan_data.get('adjustment_meta'), dict) else {}
    meta.update({
        'trigger_source': 'material-quiz',
        'trigger_attempt_id': attempt_record.id,
        'trigger_course_id': getattr(course, 'id', None),
        'trigger_material_id': getattr(material, 'id', None),
        'trigger_material_title': getattr(material, 'title', ''),
        'trigger_score': round(score, 1),
        'trigger_threshold': 80,
        'review_recommendation_count': len(review_recommendations),
        'progress_signal_count': max(
            int(meta.get('progress_signal_count') or 0),
            len((refreshed_plan_data.get('recent_progress') or [])),
            1,
        ),
    })
    refreshed_plan_data['adjustment_meta'] = meta

    new_plan = LearningPlan.objects.create(
        user=user,
        title=str(refreshed_plan_data.get('title') or base_plan.title)[:200],
        plan_data=json.dumps(refreshed_plan_data, ensure_ascii=False),
        status='generated',
    )

    return {
        'triggered': True,
        'trigger_source': 'material-quiz',
        'trigger_source_label': _format_adjustment_trigger_source('material-quiz'),
        'trigger_attempt_id': attempt_record.id,
        'score': round(score, 1),
        'threshold': 80,
        'review_recommendation_count': len(review_recommendations),
        'new_plan_id': new_plan.id,
        'new_plan_title': new_plan.title,
        'detail_url': reverse('learning_plan_detail', args=[new_plan.id]),
    }


def _get_latest_course_plan_snapshot(user, course=None):
    if not user or not getattr(user, 'is_authenticated', False):
        return None

    plan_items = LearningPlan.objects.filter(user=user, status='generated').order_by('-updated_at', '-id')[:12]
    target_course_id = getattr(course, 'id', None)
    for item in plan_items:
        payload = _safe_json_loads(item.plan_data, {})
        if not isinstance(payload, dict):
            continue
        matched_course = payload.get('matched_course') if isinstance(payload.get('matched_course'), dict) else {}
        matched_course_id = matched_course.get('id')
        if target_course_id and str(matched_course_id) != str(target_course_id):
            continue

        weak_areas = [str(entry).strip() for entry in payload.get('weak_areas') or [] if str(entry).strip()]
        recommendation_reason = [str(entry).strip() for entry in payload.get('recommendation_reason') or [] if str(entry).strip()]
        modules = payload.get('modules') if isinstance(payload.get('modules'), list) else []
        top_module = modules[0] if modules and isinstance(modules[0], dict) else {}
        top_lessons = top_module.get('lessons') if isinstance(top_module.get('lessons'), list) else []

        return {
            'plan_id': item.id,
            'title': payload.get('title') or item.title,
            'description': str(payload.get('description') or '').strip(),
            'matched_course': matched_course,
            'plan_data': payload,
            'weak_areas': weak_areas[:3],
            'recommendation_reason': recommendation_reason[:3],
            'top_module_name': str(top_module.get('name') or '').strip(),
            'top_module_focus': str(top_module.get('focus') or '').strip(),
            'top_lessons': [
                {
                    'title': str(lesson.get('title') or '').strip(),
                    'objectives': str(lesson.get('objectives') or '').strip(),
                }
                for lesson in top_lessons[:2]
                if isinstance(lesson, dict)
            ],
            'adjustment_meta': payload.get('adjustment_meta') if isinstance(payload.get('adjustment_meta'), dict) else {},
        }
    return None






def _collect_relevant_agent_tasks(user, latest_plan=None, matched_course=None, limit=12):
    if AgentTask is None:
        return {
            'tasks': [],
            'task_by_outline_id': {},
            'task_by_resource_key': {},
        }

    task_by_outline_id = {}
    task_by_resource_key = {}
    relevant_tasks = []
    plan_data = latest_plan.get('plan_data') if isinstance(latest_plan, dict) else {}
    outline_ids = {
        int(item.get('outline_id'))
        for item in (plan_data.get('recent_progress') or [])
        if isinstance(item, dict) and item.get('outline_id')
    }
    filter_text = ' '.join([
        str((latest_plan or {}).get('title') or ''),
        str((latest_plan or {}).get('top_module_name') or ''),
        str(((latest_plan or {}).get('matched_course') or {}).get('title') or ''),
        str(getattr(matched_course, 'title', '') or ''),
    ])
    filter_tokens = _tokenize_learning_plan_text(filter_text)

    for task in AgentTask.objects.filter(user=user).order_by('-updated_at')[:40]:
        input_data = _safe_json_loads(getattr(task, 'input_data', {}), {})
        output_data = _safe_json_loads(getattr(task, 'output_data', {}), {})
        outline_id = input_data.get('outline_id') if isinstance(input_data, dict) else None
        resources = output_data.get('resources') if isinstance(output_data, dict) else {}
        matched = False
        if outline_id:
            outline_id = int(outline_id)
            if outline_id not in task_by_outline_id:
                task_by_outline_id[outline_id] = task
            if outline_id in outline_ids:
                matched = True
        if isinstance(resources, dict):
            for task_rtype, resource_payload in resources.items():
                if not isinstance(resource_payload, dict) or not resource_payload.get('id'):
                    continue
                resource_key = (str(task_rtype).strip(), int(resource_payload['id']))
                if resource_key not in task_by_resource_key:
                    task_by_resource_key[resource_key] = task
        if not matched and filter_tokens:
            haystack = ' '.join([
                str(task.name or ''),
                json.dumps(input_data, ensure_ascii=False),
                json.dumps(output_data, ensure_ascii=False),
            ]).lower()
            matched = any(token.lower() in haystack for token in filter_tokens[:8])
        if matched and len(relevant_tasks) < limit:
            relevant_tasks.append(task)

    return {
        'tasks': relevant_tasks,
        'task_by_outline_id': task_by_outline_id,
        'task_by_resource_key': task_by_resource_key,
    }


def _course_queryset_for_user(user):
    if getattr(user, 'is_authenticated', False):
        return Course.objects.filter(
            models.Q(owner=user)
            | (
                models.Q(status='published')
                & (models.Q(visibility='public') | models.Q(visibility='login'))
            )
        )
    return Course.objects.filter(status='published', visibility='public')


def _get_material_preview_kind(material):
    material_type = (material.material_type or '').lower()
    file_name = (getattr(material.file, 'name', '') or '').lower()
    if material_type == 'pdf' or file_name.endswith('.pdf'):
        return 'pdf'
    if material_type == 'image' or file_name.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
        return 'image'
    return 'download'


def _build_material_quiz_context(material, max_chunks: int = 30, max_total_chars: int = 25000):
    all_chunks = list(material.chunks.order_by('chunk_index'))
    if not all_chunks and material.extracted_text:
        return (material.extracted_text or '')[:max_total_chars]

    if not all_chunks:
        return ''

    total_chunks = len(all_chunks)
    if total_chunks <= max_chunks:
        selected_chunks = all_chunks
    else:
        selected_indexes = set()
        last_index = total_chunks - 1
        
        selected_indexes.add(0)
        selected_indexes.add(last_index)
        
        middle_index = last_index // 2
        selected_indexes.add(middle_index)
        
        for position in range(2, max_chunks):
            ratio = position / max(max_chunks - 1, 1)
            adjusted_ratio = ratio ** 0.7
            selected_index = round(adjusted_ratio * last_index)
            selected_indexes.add(selected_index)
        
        selected_chunks = [all_chunks[index] for index in sorted(selected_indexes)]

    lines = []
    remaining_budget = max_total_chars
    
    header_lines = []
    if len(selected_chunks) < len(all_chunks):
        header_lines.append('⚠️ 注意：以下内容覆盖资料的开头、中段和结尾，请确保出题时覆盖所有部分。')
        header_lines.append(f'📄 资料共{total_chunks}个片段，本次抽取{len(selected_chunks)}个。')
    for h in header_lines:
        remaining_budget -= len(h)
    
    per_chunk_budget = remaining_budget // max(len(selected_chunks), 1)
    excerpt_budget = min(1200, per_chunk_budget)
    
    chunk_count = 0
    for chunk in selected_chunks:
        if remaining_budget <= 100:
            break
        heading = (chunk.keyword_summary or chunk.heading or '资料片段').strip()
        page = f"第{chunk.source_page}页/张" if chunk.source_page else ''
        chunk_num = chunk.chunk_index if hasattr(chunk, 'chunk_index') else '?'
        progress = f"[{chunk_num}/{total_chunks}]"
        title_line = f"{progress}{heading}"
        if page:
            title_line += f"（{page}）"
        content_text = (chunk.content or '').strip()
        if not content_text:
            continue
        
        actual_budget = min(excerpt_budget, remaining_budget - len(title_line) - 2)
        if actual_budget < 50:
            continue
            
        content_excerpt = content_text[:actual_budget]
        lines.append(title_line)
        lines.append(content_excerpt)
        remaining_budget -= len(title_line) + len(content_excerpt) + 1
        chunk_count += 1
    
    result = '\n'.join(lines).strip()
    logger.debug(f"[CONTEXT DEBUG] Generated context: {len(result)} chars from {chunk_count} chunks")
    return result


def _sample_material_chunks(material, max_chunks=6):
    all_chunks = list(material.chunks.order_by('chunk_index'))
    if not all_chunks:
        return []
    if len(all_chunks) <= max_chunks:
        return all_chunks

    selected_indexes = set()
    last_index = len(all_chunks) - 1
    for position in range(max_chunks):
        selected_indexes.add(round((position * last_index) / max(max_chunks - 1, 1)))
    return [all_chunks[index] for index in sorted(selected_indexes)]


def _interleave_sampled_chunks(chunks):
    ordered = []
    left = 0
    right = len(chunks) - 1
    while left <= right:
        ordered.append(chunks[left])
        if left < right:
            ordered.append(chunks[right])
        left += 1
        right -= 1
    return ordered


def _normalize_quiz_variant_seed(seed):
    try:
        return int(seed)
    except (TypeError, ValueError):
        return 0


def _rotate_chunks_by_seed(chunks, seed):
    if not chunks:
        return []
    normalized_seed = abs(_normalize_quiz_variant_seed(seed))
    if normalized_seed == 0:
        return list(chunks)
    offset = normalized_seed % len(chunks)
    if offset == 0:
        return list(chunks)
    return list(chunks[offset:]) + list(chunks[:offset])


def _is_page_like_heading(value):
    text = str(value or '').strip()
    if not text:
        return True
    return bool(re.fullmatch(r'第\s*\d+\s*页(?:/张)?', text) or re.fullmatch(r'page\s*\d+', text.lower()))


def _split_topic_candidates(value):
    text = str(value or '').strip()
    if not text:
        return []
    parts = [item.strip('"\'“”‘’()（）[]【】<>《》 ') for item in re.split(r'[\\/|,，;；、]+', text) if item.strip()]
    return [item for item in parts if item]


def _is_noisy_topic_candidate(value):
    text = str(value or '').strip()
    if not text:
        return True
    if _is_page_like_heading(text):
        return True
    if re.fullmatch(r'\d+(?:\.\d+)?', text):
        return True

    noise_terms = {
        '海纳百川',
        '厚德笃学',
        '自强不息',
        '知行合一',
        '大连理工大学',
        '校训',
        '校歌',
        '校史',
        '学校简介',
        '学院简介',
    }
    lowered = text.lower()
    if text in noise_terms:
        return True
    if any(term in lowered for term in {'copyright', '版权所有', '联系电话', '官网', 'www.'}):
        return True
    if len(text) < 2 or len(text) > 28:
        return True
    return False


def _domain_signal_count(text):
    content = str(text or '').strip().lower()
    if not content:
        return 0

    domain_markers = [
        '总线', '协议', '寄存器', '中断', '内存', '指令', '电路',
        '系统', '算法', '模型', '损失函数', '梯度', '学习率', '导数', '积分',
        '矩阵', '向量', '概率', '数据库', '事务', '索引', '编译', '进程', '线程',
        'dma', 'cpu', 'io', 'i/o',
    ]
    return sum(1 for marker in domain_markers if marker in content)


def _passes_domain_density_threshold(candidate, chunk):
    topic = str(candidate or '').strip()
    if not topic:
        return False

    heading = str(getattr(chunk, 'heading', '') or '').strip()
    keyword_summary = str(getattr(chunk, 'keyword_summary', '') or '').strip()
    content_text = str(getattr(chunk, 'content', '') or '').strip()
    scope_text = ' '.join([heading, keyword_summary, content_text])

    # 候选主题至少要在片段语境内出现，避免脱离资料的抽象词直接入题。
    if topic not in scope_text:
        return False

    topic_signal = _domain_signal_count(topic)
    scope_signal = _domain_signal_count(scope_text)
    return topic_signal >= 1 or scope_signal >= 2


def _derive_chunk_topic(chunk):
    heading = str(getattr(chunk, 'heading', '') or '').strip()
    if heading and heading not in {'资料片段', '该部分内容'}:
        for candidate in _split_topic_candidates(heading):
            if not _is_noisy_topic_candidate(candidate) and _passes_domain_density_threshold(candidate, chunk):
                return candidate

    keyword_summary = str(getattr(chunk, 'keyword_summary', '') or '').strip()
    if keyword_summary:
        for candidate in _split_topic_candidates(keyword_summary):
            if not _is_noisy_topic_candidate(candidate) and _passes_domain_density_threshold(candidate, chunk):
                return candidate

    content_text = str(getattr(chunk, 'content', '') or '').strip()
    fragments = [fragment.strip('：:;；，。, ') for fragment in re.split(r'[。！？\n]', content_text) if fragment.strip()]
    for fragment in fragments:
        for candidate in _split_topic_candidates(fragment):
            if 4 <= len(candidate) <= 24 and not _is_noisy_topic_candidate(candidate) and _passes_domain_density_threshold(candidate, chunk):
                return candidate
    return ''


def _is_noisy_fact_fragment(fragment):
    text = str(fragment or '').strip()
    if len(text) < 8:
        return True

    if re.fullmatch(r'[\d\s/\-_.]+', text):
        return True

    noise_markers = [
        '海纳百川', '厚德笃学', '自强不息', '知行合一', '大连理工大学',
        '校训', '校歌', '校史', '学校简介', '学院简介', '版权', '联系电话', '官网',
    ]
    noise_hits = sum(1 for marker in noise_markers if marker in text)
    if noise_hits >= 2:
        return True

    # 没有任何学科信号且出现强噪声标记时，直接过滤。
    if _domain_signal_count(text) == 0 and noise_hits >= 1:
        return True

    # 过滤 OCR/提取残片：如“I/O接口0 I/O接口1 I/O接口n…”这类枚举碎片不适合作为题干事实。
    has_ellipsis = '…' in text or '...' in text
    interface_enum_hits = len(re.findall(r'(?:i\s*/\s*o\s*接口\s*[0-9a-zA-ZnN]+)', text, flags=re.IGNORECASE))
    has_predicate = any(marker in text for marker in ['用于', '负责', '包括', '需要', '实现', '传输', '分配', '连接', '提供'])
    if (has_ellipsis and interface_enum_hits >= 2) or (interface_enum_hits >= 3 and not has_predicate):
        return True

    return False


def _score_fact_fragment(fragment, topic_hint=''):
    text = str(fragment or '').strip()
    if not text or _is_noisy_fact_fragment(text):
        return -1000

    score = _domain_signal_count(text) * 8
    if any(marker in text for marker in ['用于', '负责', '包括', '需要', '实现', '传输', '分配']):
        score += 2

    hint = str(topic_hint or '').strip()
    if hint and hint in text:
        score += 3

    score += min(len(text), 80) / 40.0
    return score


def _chunk_domain_signal(chunk):
    heading = str(getattr(chunk, 'heading', '') or '').strip()
    keyword_summary = str(getattr(chunk, 'keyword_summary', '') or '').strip()
    content_text = str(getattr(chunk, 'content', '') or '').strip()
    scope_text = ' '.join([heading, keyword_summary, content_text])
    return _domain_signal_count(scope_text)


def _is_knowledge_point_text(text, minimum_signal=1):
    content = str(text or '').strip()
    if not content:
        return False
    if _is_noisy_fact_fragment(content):
        return False
    return _domain_signal_count(content) >= max(1, int(minimum_signal or 1))


def _extract_chunk_core_fact(chunk, max_len=120):
    content_text = str(getattr(chunk, 'content', '') or '').strip()
    if not content_text:
        return ''

    topic_hint = _derive_chunk_topic(chunk)
    candidates = [
        fragment.strip('：:;；，。, ')
        for fragment in re.split(r'[。！？\n]', content_text)
        if fragment.strip()
    ]

    best_fragment = ''
    best_score = -1000
    for fragment in candidates:
        score = _score_fact_fragment(fragment, topic_hint)
        if score > best_score:
            best_score = score
            best_fragment = fragment

    if best_fragment and best_score > -100:
        return best_fragment[:max_len]

    for fragment in candidates:
        if len(fragment) >= 8 and not _is_noisy_fact_fragment(fragment):
            return fragment[:max_len]
    fallback_text = content_text[:max_len]
    if _is_noisy_fact_fragment(fallback_text):
        return ''
    return fallback_text


def _extract_fact_subject(topic, core_fact):
    topic_text = str(topic or '').strip()
    fact_text = str(core_fact or '').strip()
    if topic_text:
        return topic_text

    match = re.match(r'^([^，。,；;\s]{2,24}?)(用于|负责|是|包括|需要|可以|能够)', fact_text)
    if match:
        return match.group(1)
    return '该机制'


def _build_domain_distractors(topic, core_fact):
    topic_text = str(topic or '').strip()
    fact_text = str(core_fact or '').strip()
    subject = _extract_fact_subject(topic_text, fact_text)

    # 三类错误模式：范围偷换、条件缺失、术语混淆。
    if '总线' in topic_text or '总线' in fact_text:
        return [
            f'{subject}只在 CPU 内部单向工作，内存与外设不经过它交换信息。',
            f'{subject}在设备接入时不需要中断、DMA 或 I/O 资源分配也能稳定运行。',
            f'{subject}只承载控制流，不传输数据和地址信息。',
        ]

    if '中断' in topic_text or '中断' in fact_text:
        return [
            f'{subject}只在关机阶段触发，正常运行时不会进入中断处理。',
            f'{subject}发生后无需保存现场，执行流可以无缝继续。',
            f'{subject}与优先级和中断向量无关，所有请求按同一规则处理。',
        ]

    if '数据库' in topic_text or '事务' in topic_text or '事务' in fact_text:
        return [
            f'{subject}只优化查询速度，与一致性和隔离性无关。',
            f'{subject}提交前不需要任何并发控制也不会出现冲突。',
            f'{subject}可以用索引直接替代事务语义。',
        ]

    if '算法' in topic_text or '模型' in topic_text or '梯度' in fact_text:
        return [
            f'{subject}只决定展示方式，不会影响训练或推理结果。',
            f'{subject}不需要前置条件和迭代过程即可一次完成收敛。',
            f'{subject}与参数更新方向无关，梯度和损失可以互换使用。',
        ]

    return [
        f'{subject}只覆盖边缘场景，主流程通常不会使用它。',
        f'{subject}在缺少前置条件时仍可直接生效且不影响系统状态。',
        f'{subject}主要用于统一命名，不参与数据流与控制流处理。',
    ]


def _infer_fact_focus(core_fact):
    fact_text = str(core_fact or '').strip()
    if not fact_text:
        return 'general'
    if '负责' in fact_text:
        return 'responsibility'
    if '用于' in fact_text or '用来' in fact_text:
        return 'purpose'
    if '包括' in fact_text or '由' in fact_text and '组成' in fact_text:
        return 'composition'
    if '需要' in fact_text or '必须' in fact_text:
        return 'requirement'
    return 'general'


def _build_single_choice_question_stem(topic, core_fact):
    topic_text = str(topic or '').strip()
    fact_text = str(core_fact or '').strip()
    subject = _extract_fact_subject(topic_text, fact_text)
    focus = _infer_fact_focus(fact_text)

    if topic_text and focus == 'responsibility':
        return f'关于“{topic_text}”的主要职责，下列说法正确的是？'
    if topic_text and focus == 'purpose':
        return f'关于“{topic_text}”的用途，下列说法正确的是？'
    if topic_text and focus == 'composition':
        return f'关于“{topic_text}”的组成，下列说法正确的是？'
    if topic_text:
        return f'关于“{topic_text}”的描述，下列说法正确的是？'
    if subject and subject != '该机制':
        return f'关于{subject}的描述，下列说法正确的是？'
    return '下列关于本节核心机制的说法，正确的是？'


def _build_true_false_statement(topic, core_fact, question_index):
    fact_text = str(core_fact or '').strip()
    if not fact_text:
        return '', '正确'

    topic_text = str(topic or '').strip()
    distractors = _build_domain_distractors(topic_text, fact_text)
    # 固定采用交替策略，避免判断题始终是“正确”。
    if question_index % 2 == 1 and distractors:
        return distractors[0], '错误'
    return fact_text, '正确'


def _build_true_false_explanation(core_fact, statement, answer):
    fact_text = str(core_fact or '').strip()
    statement_text = str(statement or '').strip()
    if answer == '正确':
        return '资料中的关键事实：' + fact_text

    reason = '错误点：该表述与资料中的关键机制不一致。'
    if '只在' in statement_text:
        reason = '错误点：把适用范围过度缩小，遗漏了资料中的完整作用范围。'
    elif '不需要' in statement_text or '无需' in statement_text:
        reason = '错误点：忽略了资料明确要求的前置条件或必要资源。'
    elif '不传输' in statement_text or '无关' in statement_text:
        reason = '错误点：把核心功能错误地剔除或弱化了。'

    return '资料中的关键事实：' + fact_text + '。' + reason


def _build_single_choice_options_with_answer(core_fact, distractors, seed_text):
    answer_text = str(core_fact or '').strip()
    options = [answer_text] + [str(item or '').strip() for item in (distractors or []) if str(item or '').strip()]
    unique_options = []
    for option in options:
        if option and option not in unique_options:
            unique_options.append(option)
    if len(unique_options) <= 1:
        return unique_options, answer_text

    # 用稳定哈希旋转选项位置，让正确项不总是第一个。
    seed_value = int(hashlib.md5(str(seed_text or answer_text).encode('utf-8')).hexdigest(), 16)
    rotate = seed_value % len(unique_options)
    rotated = unique_options[rotate:] + unique_options[:rotate]
    return rotated, answer_text


def _build_structured_fallback_question(chunk, topic_label, question_index):
    core_fact = _extract_chunk_core_fact(chunk)
    if not core_fact:
        return None

    topic = str(topic_label or '').strip()
    source_page = str(getattr(chunk, 'source_page', '') or '').strip()
    source_heading = str(getattr(chunk, 'heading', '') or '').strip()
    knowledge_tag = topic or source_heading

    # 只对具备明确学科信号的片段出题，避免“非知识点内容”混入题库。
    scope_signal = _chunk_domain_signal(chunk)
    if scope_signal < 1:
        return None
    if not _is_knowledge_point_text(core_fact, minimum_signal=1):
        return None

    mode = question_index % 3

    if mode == 0:
        question_text = _build_single_choice_question_stem(topic, core_fact)
        distractors = _build_domain_distractors(topic, core_fact)
        options, answer_text = _build_single_choice_options_with_answer(
            core_fact,
            distractors,
            seed_text=' '.join([topic, source_heading, core_fact]),
        )
        return {
            'type': 'single_choice',
            'question': question_text,
            'options': options,
            'answer': answer_text,
            'explanation': '资料明确指出：' + core_fact,
            'source_page': source_page,
            'source_heading': source_heading,
            'knowledge_tag': knowledge_tag,
        }

    if mode == 1:
        tf_statement, tf_answer = _build_true_false_statement(topic, core_fact, question_index)
        if not tf_statement:
            return None
        return {
            'type': 'true_false',
            'question': '判断正误：' + tf_statement,
            'options': ['正确', '错误'],
            'answer': tf_answer,
            'explanation': _build_true_false_explanation(core_fact, tf_statement, tf_answer),
            'source_page': source_page,
            'source_heading': source_heading,
            'knowledge_tag': knowledge_tag,
        }

    question_text = f'什么是“{topic}”？它在当前主题中的核心作用是什么？' if topic else '请写出本节一个关键机制，并说明它的核心作用。'
    return {
        'type': 'short_answer',
        'question': question_text,
        'answer': core_fact,
        'explanation': '答案依据资料关键片段整理。',
        'source_page': source_page,
        'source_heading': source_heading,
        'knowledge_tag': knowledge_tag,
    }


def _build_fallback_quiz_questions(material, raw_questions=None, count=5, variant_seed=0):
    chunk_list = list(material.chunks.order_by('chunk_index'))
    if not chunk_list and not getattr(material, 'extracted_text', ''):
        return []

    questions = []
    seen_prompts = set()
    raw_question_list = [item for item in (raw_questions or []) if isinstance(item, dict)]
    if raw_question_list:
        annotated_questions = _annotate_quiz_questions_with_source(material, [dict(item) for item in raw_question_list])
        chunk_by_key = {
            (str(chunk.source_page or '').strip(), str(chunk.heading or '').strip()): chunk
            for chunk in chunk_list
        }
        for index, question in enumerate(annotated_questions):
            prompt_text = _get_question_text(question)
            if not prompt_text or prompt_text in seen_prompts:
                continue
            matched_chunk = chunk_by_key.get((str(question.get('source_page') or '').strip(), str(question.get('source_heading') or '').strip()))
            answer_text = str(question.get('answer') or question.get('correct_answer') or '').strip()
            if not answer_text and matched_chunk:
                answer_text = (matched_chunk.content or '').strip()[:240]
            if matched_chunk and _chunk_domain_signal(matched_chunk) < 1:
                continue
            if not _is_knowledge_point_text(' '.join([prompt_text, answer_text]), minimum_signal=1):
                continue
            explanation_text = str(question.get('explanation') or '').strip() or answer_text[:160]
            if not answer_text:
                continue
            seen_prompts.add(prompt_text)
            questions.append({
                'id': len(questions) + 1,
                'type': 'short_answer',
                'question': prompt_text,
                'answer': answer_text,
                'explanation': explanation_text,
                'source_page': question.get('source_page') or '',
                'source_heading': question.get('source_heading') or '',
                'knowledge_tag': question.get('knowledge_tag') or question.get('source_heading') or '',
            })
            if len(questions) >= count:
                return questions

    sampled_chunks = _sample_material_chunks(material, max_chunks=count * 2)
    ordered_chunks = _rotate_chunks_by_seed(_interleave_sampled_chunks(sampled_chunks), variant_seed)
    rear_chunk_count = max(1, (len(sampled_chunks) * 3 + 9) // 10)
    rear_chunk_ids = {chunk.id for chunk in sampled_chunks[-rear_chunk_count:]}
    fallback_candidates = []

    for chunk in ordered_chunks:
        topic_label = _derive_chunk_topic(chunk)
        built = _build_structured_fallback_question(chunk, topic_label, len(questions) + len(fallback_candidates))
        if not built:
            continue
        prompt_text = str(built.get('question') or '').strip()
        if not prompt_text or prompt_text in seen_prompts:
            continue
        seen_prompts.add(prompt_text)
        fallback_candidates.append({
            'question': built,
            'is_rear': chunk.id in rear_chunk_ids,
        })

    if fallback_candidates:
        selected_candidates = fallback_candidates[:count]
        if count >= 2 and selected_candidates and not any(item['is_rear'] for item in selected_candidates):
            rear_replacement = None
            for candidate in fallback_candidates[count:]:
                if candidate['is_rear']:
                    rear_replacement = candidate
                    break
            if rear_replacement:
                selected_candidates[-1] = rear_replacement

        for candidate in selected_candidates:
            built = candidate['question']
            built['id'] = len(questions) + 1
            questions.append(built)

    if not questions and getattr(material, 'extracted_text', ''):
        excerpt = str(material.extracted_text or '').strip()[:240]
        if excerpt and _is_knowledge_point_text(excerpt, minimum_signal=1):
            questions.append({
                'id': 1,
                'type': 'single_choice',
                'question': '下列关于本节核心机制的说法，正确的是？',
                'options': [
                    excerpt,
                    '该内容仅用于术语命名，不参与核心机制。',
                    '该内容只在启动阶段短暂生效，运行期不起作用。',
                    '该内容的目标是替代所有其他核心模块。',
                ],
                'answer': excerpt,
                'explanation': '答案依据当前资料文本整理。',
                'source_page': '',
                'source_heading': '',
                'knowledge_tag': '',
            })

    return questions[:count]


def _tokenize_quiz_match_text(value):
    return {
        token for token in re.split(r'[^\w\u4e00-\u9fff]+', (value or '').lower())
        if len(token) >= 2
    }


def _annotate_quiz_questions_with_source(material, questions):
    chunk_list = list(material.chunks.order_by('chunk_index')[:40])
    if not chunk_list:
        return questions

    for question in questions:
        question_text = ' '.join([
            str(question.get('question') or question.get('text') or ''),
            str(question.get('explanation') or ''),
            str(question.get('answer') or question.get('correct_answer') or ''),
        ])
        question_tokens = _tokenize_quiz_match_text(question_text)
        if not question_tokens:
            continue

        best_chunk = None
        best_score = 0.0
        for chunk in chunk_list:
            chunk_text = ' '.join([
                str(chunk.heading or ''),
                str(chunk.keyword_summary or ''),
                str(chunk.content or '')[:800],
            ])
            chunk_tokens = _tokenize_quiz_match_text(chunk_text)
            if not chunk_tokens:
                continue
            overlap = len(question_tokens & chunk_tokens) / max(len(question_tokens), 1)
            heading_bonus = 0.15 if chunk.heading and any(token in (chunk.heading or '').lower() for token in question_tokens) else 0.0
            score = overlap + heading_bonus
            if score > best_score:
                best_score = score
                best_chunk = chunk

        if best_chunk and best_score > 0.05:
            question['source_page'] = best_chunk.source_page or ''
            question['source_heading'] = best_chunk.heading or ''
            if _is_page_like_heading(best_chunk.heading):
                question['knowledge_tag'] = _derive_chunk_topic(best_chunk) or question.get('knowledge_tag') or ''
            else:
                question['knowledge_tag'] = best_chunk.heading or ''
        elif len(chunk_list) == 1:
            question['source_page'] = chunk_list[0].source_page or ''
            question['source_heading'] = chunk_list[0].heading or ''
            if _is_page_like_heading(chunk_list[0].heading):
                question['knowledge_tag'] = _derive_chunk_topic(chunk_list[0]) or question.get('knowledge_tag') or ''
            else:
                question['knowledge_tag'] = chunk_list[0].heading or ''
    return questions


def _parse_quiz_payload(raw_payload):
    def _flatten_option(option):
        if isinstance(option, dict):
            return str(
                option.get('text')
                or option.get('content')
                or option.get('value')
                or option.get('label')
                or ''
            ).strip()
        return str(option or '').strip()

    def _normalize_true_false_answer(answer_value):
        normalized = _normalize_practice_text(answer_value)
        if normalized in {'true', 't', 'yes', 'y', '对', '正确', '是'}:
            return '正确'
        if normalized in {'false', 'f', 'no', 'n', '错', '错误', '否'}:
            return '错误'
        return str(answer_value or '').strip()

    def _normalize_question(question, index):
        if not isinstance(question, dict):
            return None

        normalized = dict(question)
        normalized['id'] = normalized.get('id') or index + 1
        normalized['question'] = str(normalized.get('question') or normalized.get('text') or '').strip()
        if not normalized['question']:
            return None
        explanation_text = str(normalized.get('explanation') or '').strip()

        normalized_type = str(normalized.get('type') or '').strip().lower()
        raw_options = normalized.get('options')
        if not isinstance(raw_options, list) or not raw_options:
            raw_options = normalized.get('choices') if isinstance(normalized.get('choices'), list) else []

        options = []
        for option in raw_options:
            flattened = _flatten_option(option)
            if flattened and flattened not in options:
                options.append(flattened)

        answer_value = _get_question_correct_answer(normalized)
        answer_text = str(answer_value or '').strip()
        normalized_answer = _normalize_practice_text(answer_text)
        is_true_false_answer = normalized_answer in {'true', 't', 'yes', 'y', '对', '正确', '是', 'false', 'f', 'no', 'n', '错', '错误', '否'}

        if normalized_type not in {'single_choice', 'true_false', 'short_answer'}:
            if options:
                normalized_type = 'single_choice'
            elif is_true_false_answer:
                normalized_type = 'true_false'
            else:
                normalized_type = 'short_answer'

        if normalized_type == 'true_false':
            options = ['正确', '错误']
            answer_text = _normalize_true_false_answer(answer_text)
        elif normalized_type == 'single_choice':
            if not options and is_true_false_answer:
                normalized_type = 'true_false'
                options = ['正确', '错误']
                answer_text = _normalize_true_false_answer(answer_text)
            elif not options:
                normalized_type = 'short_answer'
            elif normalized_answer and normalized_answer in {'a', 'b', 'c', 'd', 'e', 'f'}:
                option_index = ord(normalized_answer) - ord('a')
                if 0 <= option_index < len(options):
                    answer_text = options[option_index]
            elif answer_text and answer_text not in options:
                options = [answer_text] + [item for item in options if item != answer_text]

            if normalized_type == 'single_choice' and len(options) < 2:
                normalized_type = 'short_answer'

        if normalized_type == 'short_answer' and not answer_text:
            answer_text = explanation_text

        if normalized_type == 'short_answer' and not answer_text:
            # 简答题既没有答案也没有解析，无法评分，直接丢弃
            return None

        if not answer_text:
            answer_text = '暂无答案'

        normalized['type'] = normalized_type
        normalized['options'] = options
        normalized['answer'] = answer_text
        normalized.pop('choices', None)
        return normalized

    def _normalize_questions(questions):
        normalized_questions = []
        for index, question in enumerate(questions):
            normalized = _normalize_question(question, index)
            if normalized:
                normalized_questions.append(normalized)
        return normalized_questions

    if isinstance(raw_payload, dict):
        questions = raw_payload.get('questions') or []
        normalized_questions = _normalize_questions(questions if isinstance(questions, list) else [])
        normalized_payload = dict(raw_payload)
        normalized_payload['questions'] = normalized_questions
        return normalized_payload, normalized_questions
    if isinstance(raw_payload, list):
        normalized_questions = _normalize_questions(raw_payload)
        return {'questions': normalized_questions}, normalized_questions
    if isinstance(raw_payload, str):
        try:
            parsed = json.loads(raw_payload)
            return _parse_quiz_payload(parsed)
        except Exception:
            return {}, []
    return {}, []


def _safe_positive_int(value, default=5, minimum=1, maximum=8):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _normalize_practice_text(value):
    text = unicodedata.normalize('NFKC', str(value or '').strip().lower())
    text = text.replace('\u3000', ' ')
    text = re.sub(r'[，。！？；：、“”‘’（）【】《》,\.!\?;:\'\"\(\)\[\]\{\}<>·、]', '', text)
    text = re.sub(r'\s+', '', text)
    return text


def _normalize_true_false_token(value):
    normalized = _normalize_practice_text(value)
    # Exact match first
    if normalized in {'true', 't', 'yes', 'y', '对', '正确', '是'}:
        return '正确'
    if normalized in {'false', 'f', 'no', 'n', '错', '错误', '否'}:
        return '错误'
    # Strip trailing punctuation/particles that the LLM may append (e.g. "正确的", "错误。")
    cleaned = normalized.rstrip('的。！!.')
    if cleaned in {'true', 't', 'yes', 'y', '对', '正确', '是'}:
        return '正确'
    if cleaned in {'false', 'f', 'no', 'n', '错', '错误', '否'}:
        return '错误'
    # Substring match as last resort: check unambiguous positive/negative tokens
    # "不正确"/"不是" should NOT match the positive branch — check negative first
    if any(kw in normalized for kw in ['不正确', '不对', '错误', '错的', '假的']):
        return '错误'
    if any(kw in normalized for kw in ['正确', '对的', '真的']):
        return '正确'
    return normalized


def _get_question_text(question):
    if not isinstance(question, dict):
        return ''
    return str(question.get('question') or question.get('text') or '').strip()


def _get_question_correct_answer(question):
    if not isinstance(question, dict):
        return ''
    for key in ('answer', 'correct_answer', 'answer_text', 'correct'):
        if key in question:
            return question.get(key)
    if 'choices' in question and isinstance(question.get('choices'), list):
        correct_options = []
        for option in question.get('choices'):
            if isinstance(option, dict) and option.get('correct'):
                correct_options.append(option.get('value') or option.get('label') or option.get('text') or option)
        if correct_options:
            return correct_options if len(correct_options) > 1 else correct_options[0]
    return ''


def _infer_question_knowledge_tag(question):
    if not isinstance(question, dict):
        return '未归类'

    direct_tag = str(question.get('knowledge_tag') or question.get('source_heading') or '').strip()
    if direct_tag:
        return direct_tag[:255]

    text = _get_question_text(question)
    if not text:
        return '未归类'

    fragments = re.split(r'[，。；：,.;:（）()\s]+', text)
    for fragment in fragments:
        cleaned = fragment.strip()
        if len(cleaned) >= 4:
            return cleaned[:255]
    return text[:255]


def _build_question_fingerprint(question):
    if not isinstance(question, dict):
        return ''
    payload = '|'.join([
        _normalize_practice_text(_get_question_text(question)),
        _normalize_practice_text(question.get('source_heading') or question.get('knowledge_tag') or ''),
        _normalize_practice_text(question.get('source_page') or ''),
    ])
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()


def _build_practice_profile(user, material, focus_question=None):
    recent_stats = list(
        MaterialQuestionStat.objects.filter(user=user, material=material)
        .order_by('-last_seen_at')[:8]
    )
    recent_questions = [item.question_text for item in recent_stats if item.question_text]
    recent_fingerprints = {item.question_fingerprint for item in recent_stats if item.question_fingerprint}

    focus_fingerprint = _build_question_fingerprint(focus_question) if isinstance(focus_question, dict) else ''
    focus_stat = None
    difficulty_stage = 'standard'
    difficulty_label = '标准巩固'
    stage_instruction = '保持基础到中等难度，便于学生快速校验刚读过的资料。'

    if focus_fingerprint:
        focus_stat = MaterialQuestionStat.objects.filter(
            user=user,
            material=material,
            question_fingerprint=focus_fingerprint,
        ).first()
        similar_count = focus_stat.similar_generation_count if focus_stat else 0
        if similar_count <= 0:
            difficulty_stage = 'reinforce'
            difficulty_label = '同层巩固'
            stage_instruction = '生成同知识点、同难度层级的巩固题，帮助学生先纠正当前错误。'
        elif similar_count == 1:
            difficulty_stage = 'progressive'
            difficulty_label = '进阶变式'
            stage_instruction = '生成同知识点但问法变化更明显、难度略高一点的变式题。'
        else:
            difficulty_stage = 'challenge'
            difficulty_label = '挑战提升'
            stage_instruction = '生成同知识点的迁移题或综合题，难度高于前两轮。'

    return {
        'recent_questions': recent_questions,
        'recent_fingerprints': recent_fingerprints,
        'difficulty_stage': difficulty_stage,
        'difficulty_label': difficulty_label,
        'stage_instruction': stage_instruction,
        'focus_fingerprint': focus_fingerprint,
        'focus_stat_id': getattr(focus_stat, 'id', None),
    }


def _default_feedback_counts():
    return {
        'useful': 0,
        'not_useful': 0,
        'too_easy': 0,
        'too_hard': 0,
        'off_topic': 0,
    }


def _default_adaptive_strategy():
    return {
        'question_count_delta': 0,
        'difficulty_bias': 'balanced',
        'preferred_question_type': 'mixed',
        'domain_signal_threshold': 1,
        'off_topic_guard': 'standard',
        'priority_knowledge_tags': [],
        'knowledge_feedback': {},
    }


def _normalize_feedback_counts(raw_counts):
    merged = _default_feedback_counts()
    raw = raw_counts if isinstance(raw_counts, dict) else {}
    for key in merged.keys():
        try:
            merged[key] = max(0, int(raw.get(key, merged[key])))
        except (TypeError, ValueError):
            merged[key] = 0
    return merged


def _normalize_adaptive_strategy(raw_strategy):
    merged = _default_adaptive_strategy()
    raw = raw_strategy if isinstance(raw_strategy, dict) else {}
    for key in merged.keys():
        if key not in raw:
            continue
        value = raw.get(key)
        if key in {'question_count_delta', 'domain_signal_threshold'}:
            try:
                merged[key] = int(value)
            except (TypeError, ValueError):
                continue
        elif key == 'priority_knowledge_tags':
            if isinstance(value, list):
                merged[key] = [str(item).strip() for item in value if str(item).strip()][:5]
        elif key == 'knowledge_feedback':
            if isinstance(value, dict):
                normalized_feedback = {}
                for tag, counts in value.items():
                    tag_text = str(tag or '').strip()
                    if not tag_text:
                        continue
                    bucket = _default_feedback_counts()
                    if isinstance(counts, dict):
                        for feedback_key in bucket.keys():
                            try:
                                bucket[feedback_key] = max(0, int(counts.get(feedback_key, 0)))
                            except (TypeError, ValueError):
                                bucket[feedback_key] = 0
                    normalized_feedback[tag_text] = bucket
                merged[key] = normalized_feedback
        else:
            merged[key] = str(value or '').strip() or merged[key]
    merged['question_count_delta'] = max(-2, min(2, merged['question_count_delta']))
    merged['domain_signal_threshold'] = max(1, min(3, merged['domain_signal_threshold']))
    if merged['difficulty_bias'] not in {'balanced', 'reinforce', 'progressive', 'challenge'}:
        merged['difficulty_bias'] = 'balanced'
    if merged['preferred_question_type'] not in {'mixed', 'objective', 'short_answer'}:
        merged['preferred_question_type'] = 'mixed'
    if merged['off_topic_guard'] not in {'standard', 'strict'}:
        merged['off_topic_guard'] = 'standard'
    return merged


ELO_DEFAULT_RATING = 1200.0
ELO_K_STUDENT = 24.0
ELO_K_ITEM = 16.0
ELO_GAP_PROGRESSIVE_THRESHOLD = 60.0
ELO_GAP_REINFORCE_THRESHOLD = -60.0


def _elo_expected_score(rating_a, rating_b):
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def _apply_elo_update(stat, policy, is_correct):
    """对一道题应用一次 Elo 更新：学生能力 vs 题目（个性化）难度。"""
    student_rating = policy.ability_rating or ELO_DEFAULT_RATING
    item_rating = stat.elo_rating or ELO_DEFAULT_RATING

    expected_student = _elo_expected_score(student_rating, item_rating)
    expected_item = 1.0 - expected_student

    student_score = 1.0 if is_correct else 0.0
    item_score = 1.0 - student_score

    policy.ability_rating = round(student_rating + ELO_K_STUDENT * (student_score - expected_student), 2)
    stat.elo_rating = round(item_rating + ELO_K_ITEM * (item_score - expected_item), 2)


def _compute_elo_gap(policy, user, material):
    """学生能力评分与近期题目（个性化）难度评分均值的差值；正值=能力超出难度。"""
    student_rating = policy.ability_rating or ELO_DEFAULT_RATING
    recent_ratings = list(
        MaterialQuestionStat.objects.filter(user=user, material=material)
        .order_by('-last_seen_at')
        .values_list('elo_rating', flat=True)[:10]
    )
    if not recent_ratings:
        return 0.0
    avg_item_rating = sum(recent_ratings) / len(recent_ratings)
    return student_rating - avg_item_rating


def _get_or_create_adaptive_policy(user, course, material):
    policy, _ = MaterialQuizAdaptivePolicy.objects.get_or_create(
        user=user,
        course=course,
        material=material,
        defaults={
            'feedback_counts': _default_feedback_counts(),
            'strategy': _default_adaptive_strategy(),
        },
    )
    return policy


def _recompute_adaptive_strategy(policy, user, material):
    feedback_counts = _normalize_feedback_counts(policy.feedback_counts)
    strategy = _normalize_adaptive_strategy(policy.strategy)

    recent_attempts = list(MaterialQuizAttempt.objects.filter(user=user, material=material).order_by('-created_at')[:8])
    avg_score = 0.0
    if recent_attempts:
        avg_score = sum(float(item.score or 0) for item in recent_attempts) / len(recent_attempts)

    if feedback_counts['too_easy'] >= feedback_counts['too_hard'] + 2 or avg_score >= 88:
        strategy['difficulty_bias'] = 'progressive'
    elif feedback_counts['too_hard'] >= feedback_counts['too_easy'] + 2 or avg_score <= 55:
        strategy['difficulty_bias'] = 'reinforce'
    else:
        strategy['difficulty_bias'] = 'balanced'

    if strategy['difficulty_bias'] == 'balanced':
        elo_gap = _compute_elo_gap(policy, user, material)
        if elo_gap >= ELO_GAP_PROGRESSIVE_THRESHOLD:
            strategy['difficulty_bias'] = 'progressive'
        elif elo_gap <= ELO_GAP_REINFORCE_THRESHOLD:
            strategy['difficulty_bias'] = 'reinforce'

    if feedback_counts['too_hard'] >= feedback_counts['too_easy'] + 1:
        strategy['preferred_question_type'] = 'objective'
    elif feedback_counts['too_easy'] >= feedback_counts['too_hard'] + 1:
        strategy['preferred_question_type'] = 'short_answer'
    else:
        strategy['preferred_question_type'] = 'mixed'

    if feedback_counts['off_topic'] >= 2 or feedback_counts['not_useful'] >= 3:
        strategy['domain_signal_threshold'] = 2
        strategy['off_topic_guard'] = 'strict'
    else:
        strategy['domain_signal_threshold'] = 1
        strategy['off_topic_guard'] = 'standard'

    if strategy['difficulty_bias'] == 'progressive':
        strategy['question_count_delta'] = 1
    elif strategy['difficulty_bias'] == 'reinforce':
        strategy['question_count_delta'] = -1
    else:
        strategy['question_count_delta'] = 0

    tag_feedback_map = strategy.get('knowledge_feedback') if isinstance(strategy.get('knowledge_feedback'), dict) else {}
    stats = list(
        MaterialQuestionStat.objects.filter(user=user, material=material, wrong_count__gt=0)
        .exclude(knowledge_tag='')
        .order_by('-consecutive_wrong_count', '-wrong_count', '-last_seen_at')[:12]
    )
    scored_tags = []
    for stat in stats:
        tag = str(stat.knowledge_tag or '').strip()
        if not tag:
            continue
        feedback_bucket = tag_feedback_map.get(tag) if isinstance(tag_feedback_map.get(tag), dict) else {}
        useful = int(feedback_bucket.get('useful') or 0)
        off_topic = int(feedback_bucket.get('off_topic') or 0)
        not_useful = int(feedback_bucket.get('not_useful') or 0)
        score = (stat.consecutive_wrong_count * 2.0) + (stat.wrong_count * 1.5) + useful - (off_topic * 2.0) - not_useful
        scored_tags.append((score, tag))

    scored_tags.sort(key=lambda item: item[0], reverse=True)
    strategy['priority_knowledge_tags'] = [tag for score, tag in scored_tags if score > 0][:3]

    policy.feedback_counts = feedback_counts
    policy.strategy = strategy
    policy.save(update_fields=['feedback_counts', 'strategy', 'updated_at'])

    return {
        'feedback_counts': feedback_counts,
        'strategy': strategy,
        'avg_score': round(avg_score, 2),
    }


def _apply_adaptive_policy_to_profile(practice_profile, strategy, focus_question=None):
    profile = dict(practice_profile or {})
    if focus_question:
        return profile

    bias = str((strategy or {}).get('difficulty_bias') or 'balanced').strip()
    if bias == 'reinforce':
        profile['difficulty_stage'] = 'reinforce'
        profile['difficulty_label'] = '同层巩固'
        profile['stage_instruction'] = '根据最近反馈先做同层巩固，减少一次跨越太多造成的挫败。'
    elif bias == 'progressive':
        profile['difficulty_stage'] = 'progressive'
        profile['difficulty_label'] = '进阶变式'
        profile['stage_instruction'] = '根据最近反馈适当提升难度，优先考查同知识点变式能力。'
    return profile


def _build_adaptive_generation_notes(strategy_snapshot):
    strategy = (strategy_snapshot or {}).get('strategy') if isinstance(strategy_snapshot, dict) else {}
    if not isinstance(strategy, dict):
        return []

    notes = []
    if strategy.get('off_topic_guard') == 'strict':
        notes.append('额外要求：题干必须围绕课程资料中的专业概念，不要使用学校口号、宣传语或泛化表述。')
    domain_signal_threshold = int(strategy.get('domain_signal_threshold') or 1)
    if domain_signal_threshold >= 2:
        notes.append('额外要求：每道题至少包含一个明确学科术语（如总线、寄存器、DMA、梯度、学习率等）。')

    preferred_question_type = str(strategy.get('preferred_question_type') or 'mixed').strip()
    if preferred_question_type == 'objective':
        notes.append('题型倾向：优先单选题或判断题，短答题最多 1 道。')
    elif preferred_question_type == 'short_answer':
        notes.append('题型倾向：至少包含 2 道简答题，优先考查概念解释与应用描述。')

    priority_tags = [str(item).strip() for item in strategy.get('priority_knowledge_tags') or [] if str(item).strip()]
    if priority_tags:
        notes.append('知识点优先级：请优先覆盖 ' + '、'.join(priority_tags[:3]) + '。')
    return notes


def _build_elo_generation_notes(practice_profile):
    gap = practice_profile.get('elo_gap')
    if not isinstance(gap, (int, float)) or abs(gap) < 30:
        return []
    direction = '略微提高' if gap > 0 else '略微降低'
    return [
        f"额外参考：根据 Elo 评分，该学生当前能力约为 {practice_profile.get('elo_ability_rating'):.0f}，"
        f"与近期题目难度相差约 {abs(gap):.0f} 分，可在保持"
        f"{practice_profile.get('difficulty_label') or ''}总体框架的基础上{direction}单题难度。"
    ]


def _prioritize_questions_by_knowledge_tags(questions, priority_tags):
    tags = [str(item).strip().lower() for item in (priority_tags or []) if str(item).strip()]
    if not tags:
        return questions

    scored_questions = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        question_text = str(question.get('question') or '').lower()
        knowledge_tag = str(question.get('knowledge_tag') or question.get('source_heading') or '').lower()
        hit_score = 0
        for tag in tags:
            if tag and (tag in knowledge_tag or tag in question_text):
                hit_score += 1
        scored_questions.append((hit_score, -index, question))

    scored_questions.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored_questions]


def _build_adaptive_policy_payload(strategy_snapshot):
    if not isinstance(strategy_snapshot, dict):
        return {'feedback_counts': _default_feedback_counts(), 'strategy': _default_adaptive_strategy(), 'avg_score': 0.0}
    return {
        'feedback_counts': _normalize_feedback_counts(strategy_snapshot.get('feedback_counts')),
        'strategy': _normalize_adaptive_strategy(strategy_snapshot.get('strategy')),
        'avg_score': float(strategy_snapshot.get('avg_score') or 0.0),
    }


def _build_practice_generation_notes(profile):
    lines = []
    recent_questions = list(profile.get('recent_questions') or [])
    if recent_questions:
        lines.append('额外要求：避免与学生最近做过的题目重复，不要只替换个别词语。')
        for index, question_text in enumerate(recent_questions[:6], start=1):
            lines.append(f'最近做过的题目{index}：{question_text[:120]}')

    difficulty_label = profile.get('difficulty_label')
    stage_instruction = profile.get('stage_instruction')
    if difficulty_label and stage_instruction:
        lines.append(f'当前练习阶段：{difficulty_label}。{stage_instruction}')
    return lines


def _dedupe_generated_questions(questions, recent_fingerprints=None, limit=None):
    recent_fingerprint_set = set(recent_fingerprints or [])
    unique_questions = []
    fallback_questions = []
    seen_fingerprints = set()
    removed_count = 0

    print(f"[DEDUPE DEBUG] limit={limit}, recent_count={len(recent_fingerprint_set)}")
    
    for question in questions:
        if not isinstance(question, dict):
            print(f"[DEDUPE DEBUG] Skipped: not a dict")
            continue
        fingerprint = _build_question_fingerprint(question)
        print(f"[DEDUPE DEBUG] fingerprint={fingerprint}, in_seen={fingerprint in seen_fingerprints}, in_recent={fingerprint in recent_fingerprint_set}")
        if not fingerprint or fingerprint in seen_fingerprints:
            removed_count += 1
            print(f"[DEDUPE DEBUG] Removed: fingerprint empty or duplicate")
            continue
        seen_fingerprints.add(fingerprint)
        question['question_fingerprint'] = fingerprint
        question.setdefault('knowledge_tag', _infer_question_knowledge_tag(question))
        if fingerprint in recent_fingerprint_set:
            removed_count += 1
            fallback_questions.append(question)
            print(f"[DEDUPE DEBUG] Added to fallback")
            continue
        unique_questions.append(question)
        print(f"[DEDUPE DEBUG] Added to unique, total={len(unique_questions)}")

    print(f"[DEDUPE DEBUG] After loop: unique={len(unique_questions)}, fallback={len(fallback_questions)}, removed={removed_count}")
    
    if limit is not None and len(unique_questions) < limit:
        for question in fallback_questions:
            unique_questions.append(question)
            print(f"[DEDUPE DEBUG] Added from fallback to unique, total={len(unique_questions)}")
            if len(unique_questions) >= limit:
                break

    if limit is not None:
        result = unique_questions[:limit]
        print(f"[DEDUPE DEBUG] Returning {len(result)} questions (limited to {limit})")
        return result, removed_count
    print(f"[DEDUPE DEBUG] Returning {len(unique_questions)} questions (no limit)")
    return unique_questions, removed_count


def _load_quiz_questions_from_resource(quiz_resource_id, user, course_id, material_id):
    if not quiz_resource_id:
        return None, []

    try:
        from agent_system.models import LearningResource
    except Exception:
        return None, []

    resource = LearningResource.objects.filter(id=quiz_resource_id, resource_type='quiz', author=user).first()
    if not resource:
        return None, []

    metadata = resource.metadata if isinstance(resource.metadata, dict) else {}
    if str(metadata.get('course_id') or '') != str(course_id) or str(metadata.get('material_id') or '') != str(material_id):
        return None, []

    _, questions = _parse_quiz_payload(resource.content)
    return resource, questions


def _build_wrong_book_snapshot(user, material, limit=6):
    stats = MaterialQuestionStat.objects.filter(user=user, material=material, wrong_count__gt=0).order_by(
        '-consecutive_wrong_count', '-wrong_count', '-last_seen_at'
    )[:limit]
    return [
        {
            'question': item.question_text,
            'knowledge_tag': item.knowledge_tag or '未归类',
            'wrong_count': item.wrong_count,
            'consecutive_wrong_count': item.consecutive_wrong_count,
            'source_page': item.source_page,
            'source_heading': item.source_heading,
        }
        for item in stats
    ]


def _build_knowledge_group_summary(user, material, limit=5):
    stats = MaterialQuestionStat.objects.filter(user=user, material=material, wrong_count__gt=0).order_by('-wrong_count', '-last_seen_at')
    grouped = {}
    for item in stats:
        key = item.knowledge_tag or '未归类'
        summary = grouped.setdefault(key, {
            'knowledge_tag': key,
            'wrong_count': 0,
            'question_count': 0,
            'source_page': item.source_page,
            'source_heading': item.source_heading,
        })
        summary['wrong_count'] += item.wrong_count
        summary['question_count'] += 1

    return sorted(grouped.values(), key=lambda entry: (-entry['wrong_count'], -entry['question_count']))[:limit]


def _build_weak_area_target_url(course_id, material_id, source_page=''):
    target_url = reverse('course_study', args=[course_id]) + f'?material={material_id}'
    if source_page:
        target_url += f'&page={source_page}'
    return target_url


def _tokenize_learning_plan_text(value):
    normalized = _normalize_practice_text(value)
    if not normalized:
        return []
    tokens = []
    seen = set()

    def _push(token):
        token = str(token or '').strip()
        if len(token) < 2 or token in seen:
            return
        seen.add(token)
        tokens.append(token)

    for token in re.split(r'[^\w\u4e00-\u9fff]+', normalized):
        if len(token) < 2:
            continue
        _push(token)
        for chinese_part in re.findall(r'[\u4e00-\u9fff]{2,}', token):
            if len(chinese_part) <= 4:
                _push(chinese_part)
                continue
            for size in (2, 3, 4):
                if len(chinese_part) < size:
                    continue
                for index in range(len(chinese_part) - size + 1):
                    _push(chinese_part[index:index + size])
    return tokens


def _match_lesson_material_anchor(course, lesson):
    if not course or not isinstance(lesson, dict):
        return None

    query_parts = [
        str(lesson.get('title') or '').strip(),
        str(lesson.get('objectives') or '').strip(),
    ]
    query_tokens = []
    seen_tokens = set()
    for part in query_parts:
        for token in _tokenize_learning_plan_text(part):
            if token not in seen_tokens:
                seen_tokens.add(token)
                query_tokens.append(token)
    if not query_tokens:
        return None

    best_match = None
    best_score = 0.0
    chunks = MaterialChunk.objects.filter(material__course=course).select_related('material').order_by('material_id', 'chunk_index')[:240]
    for chunk in chunks:
        heading_text = str(chunk.heading or '')
        keyword_text = str(chunk.keyword_summary or '')
        content_text = str(chunk.content or '')[:800]
        material_title = str(chunk.material.title or '')
        searchable_text = ' '.join([material_title, heading_text, keyword_text, content_text]).lower()
        overlap_tokens = [token for token in query_tokens if token in searchable_text]
        if not overlap_tokens:
            continue
        overlap_score = len(overlap_tokens) / max(len(query_tokens), 1)
        heading_bonus = 0.18 if heading_text and any(token in heading_text.lower() for token in overlap_tokens) else 0.0
        keyword_bonus = 0.12 if keyword_text and any(token in keyword_text.lower() for token in overlap_tokens) else 0.0
        page_bonus = 0.05 if str(chunk.source_page or '').strip() else 0.0
        score = overlap_score + heading_bonus + keyword_bonus + page_bonus
        if score > best_score:
            best_score = score
            best_match = chunk

    if not best_match:
        return None

    source_page = str(best_match.source_page or '').strip()
    return {
        'material_id': best_match.material_id,
        'material_title': best_match.material.title,
        'source_page': source_page,
        'source_heading': str(best_match.heading or best_match.keyword_summary or '').strip(),
        'study_url': _build_weak_area_target_url(course.id, best_match.material_id, source_page),
        'ai_chat_url': reverse('course_ai_chat', args=[course.id]) + f'?material={best_match.material_id}' + (f'&current_page={source_page}' if source_page else ''),
        'match_label': ' / '.join([part for part in [best_match.material.title, f'第{source_page}页' if source_page else '', str(best_match.heading or '').strip()] if part]),
    }


def _parse_short_answer_ai_result(raw_text):
    text = str(raw_text or '').strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        match = re.search(r'\{[\s\S]*\}', text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except Exception:
            return None
    if not isinstance(parsed, dict):
        return None

    verdict = str(parsed.get('verdict') or parsed.get('result') or '').strip().lower()
    score = parsed.get('score')
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    reason = str(parsed.get('reason') or parsed.get('analysis') or '').strip()

    if verdict in {'correct', 'partial', 'wrong'}:
        return {
            'verdict': verdict,
            'score': score,
            'reason': reason,
        }
    return None


def _fallback_short_answer_match(correct_answer, submitted_answer):
    correct_text = _normalize_practice_text(correct_answer)
    submitted_text = _normalize_practice_text(submitted_answer)
    if not correct_text or not submitted_text:
        return False
    if submitted_text == correct_text:
        return True
    # 学生答案完整包含了标准答案(且标准答案不是过短的字/词) → 判对；
    # 但"学生只写了标准答案的一个片段"(submitted in correct) 不再直接判对，避免"叶绿体"当"光合作用发生在叶绿体中"，
    # 交给下面的词覆盖率(coverage/precision)判定
    if len(correct_text) >= 4 and correct_text in submitted_text:
        return True

    correct_tokens = {
        token for token in re.split(r'[^\w\u4e00-\u9fff]+', correct_text)
        if len(token) >= 2
    }
    submitted_tokens = {
        token for token in re.split(r'[^\w\u4e00-\u9fff]+', submitted_text)
        if len(token) >= 2
    }
    if not correct_tokens or not submitted_tokens:
        return False

    overlap = len(correct_tokens & submitted_tokens)
    coverage = overlap / max(len(correct_tokens), 1)
    precision = overlap / max(len(submitted_tokens), 1)
    return coverage >= 0.6 and precision >= 0.5


def _grade_short_answer_with_ai(question_text, correct_answer, submitted_answer, explanation_text=''):
    submitted_text = str(submitted_answer or '').strip()
    correct_text = str(correct_answer or '').strip()
    if not submitted_text or not correct_text:
        return {
            'correct_flag': False,
            'grading_source': 'rule',
            'grading_note': '',
        }

    prompt = (
        '你是一个严格但允许同义表达的简答题判分器。\n'
        '请根据题目、标准答案、学生答案判断学生答案是否语义正确。\n'
        '评分规则：\n'
        '1. 只要学生答案在核心事实上正确、只是表述不同，也应判 correct；\n'
        '2. 如果只答对一部分关键点，判 partial；\n'
        '3. 如果核心事实错误或缺失，判 wrong；\n'
        '4. 只输出 JSON，不要输出任何额外说明。\n'
        '输出格式：{"verdict":"correct|partial|wrong","score":0到1之间的小数,"reason":"不超过40字"}\n\n'
        f'题目：{question_text}\n'
        f'标准答案：{correct_text}\n'
        f'学生答案：{submitted_text}\n'
        f'参考解析：{str(explanation_text or "").strip()}'
    )

    try:
        from core.xunfei_spark import spark_client, XunfeiSparkClient
        client = spark_client or XunfeiSparkClient()
        raw = client.get_response([{'role': 'user', 'content': prompt}], temperature=0.1)
        parsed = _parse_short_answer_ai_result(raw)
        if parsed:
            verdict = parsed.get('verdict')
            return {
                'correct_flag': verdict == 'correct',
                'grading_source': 'ai',
                'grading_note': parsed.get('reason') or '',
            }
    except Exception:
        logger.exception('AI short answer grading failed, falling back to rule-based grading')

    return {
        'correct_flag': _fallback_short_answer_match(correct_text, submitted_text),
        'grading_source': 'rule',
        'grading_note': '',
    }


def _build_archived_weak_area_context(user, limit=6):
    archives = (
        MaterialWeakAreaArchive.objects.filter(user=user)
        .select_related('course', 'material', 'question_stat')
        .order_by('-archived_at')[:limit]
    )
    archived_areas = []
    for archive in archives:
        stat = archive.question_stat
        archived_areas.append({
            'archive_id': archive.id,
            'knowledge_tag': archive.knowledge_tag or '未归类知识点',
            'course_title': archive.course.title,
            'material_title': archive.material.title,
            'wrong_count': stat.wrong_count if stat else 0,
            'consecutive_wrong_count': stat.consecutive_wrong_count if stat else 0,
            'source_page': archive.source_page,
            'source_heading': archive.source_heading,
            'archived_at': archive.archived_at,
            'target_url': _build_weak_area_target_url(archive.course_id, archive.material_id, archive.source_page),
        })
    return archived_areas


def _persist_material_quiz_records(user, course, material, quiz_resource, questions, answers, result, difficulty_stage='standard', focus_question_fingerprint='', adaptive_policy=None):
    details = list(result.get('details') or [])
    question_fingerprints = []
    knowledge_tags = []
    review_recommendations = []
    review_keys = set()

    for index, question in enumerate(questions):
        detail = details[index] if index < len(details) else {}
        fingerprint = detail.get('question_fingerprint') or _build_question_fingerprint(question)
        knowledge_tag = detail.get('knowledge_tag') or _infer_question_knowledge_tag(question)
        correct_answer = _get_question_correct_answer(question)

        stat, _ = MaterialQuestionStat.objects.get_or_create(
            user=user,
            course=course,
            material=material,
            question_fingerprint=fingerprint,
            defaults={
                'question_text': _get_question_text(question),
                'canonical_answer': correct_answer if not isinstance(correct_answer, (list, tuple)) else json.dumps(correct_answer, ensure_ascii=False),
                'explanation': str(question.get('explanation') or ''),
                'knowledge_tag': knowledge_tag,
                'source_page': str(question.get('source_page') or ''),
                'source_heading': str(question.get('source_heading') or ''),
            },
        )

        previous_streak = stat.consecutive_wrong_count
        previous_correct = stat.last_result_correct
        stat.question_text = _get_question_text(question)
        stat.canonical_answer = correct_answer if not isinstance(correct_answer, (list, tuple)) else json.dumps(correct_answer, ensure_ascii=False)
        stat.explanation = str(question.get('explanation') or '')
        stat.knowledge_tag = knowledge_tag
        stat.source_page = str(question.get('source_page') or '')
        stat.source_heading = str(question.get('source_heading') or '')
        stat.seen_count += 1
        stat.attempts_count += 1
        if detail.get('correct_flag'):
            stat.last_result_correct = True
            stat.consecutive_wrong_count = 0
        else:
            stat.wrong_count += 1
            stat.last_result_correct = False
            stat.consecutive_wrong_count = 1 if previous_correct or previous_streak == 0 else previous_streak + 1

        if adaptive_policy is not None:
            _apply_elo_update(stat, adaptive_policy, bool(detail.get('correct_flag')))

        stat.save()

        question_fingerprints.append(fingerprint)
        if knowledge_tag and knowledge_tag not in knowledge_tags:
            knowledge_tags.append(knowledge_tag)

        if not detail.get('correct_flag') and stat.consecutive_wrong_count >= 2:
            review_key = f"{stat.source_page}|{knowledge_tag}"
            if review_key not in review_keys:
                review_keys.add(review_key)
                review_recommendations.append({
                    'knowledge_tag': knowledge_tag or '未归类',
                    'source_page': stat.source_page,
                    'source_heading': stat.source_heading,
                    'consecutive_wrong_count': stat.consecutive_wrong_count,
                    'prompt': (
                        f"你已经连续 {stat.consecutive_wrong_count} 次在“{knowledge_tag or '该知识点'}”上出错，"
                        + (f"建议先回看第{stat.source_page}页/张。" if stat.source_page else '建议先回看对应资料片段。')
                    ),
                })

    if adaptive_policy is not None:
        adaptive_policy.save(update_fields=['ability_rating'])

    attempt = MaterialQuizAttempt.objects.create(
        user=user,
        course=course,
        material=material,
        quiz_resource=quiz_resource,
        quiz_snapshot={'questions': questions},
        answers=answers if isinstance(answers, dict) else {},
        result=result,
        question_fingerprints=question_fingerprints,
        knowledge_tags=knowledge_tags,
        recommended_review_pages=review_recommendations,
        difficulty_stage=difficulty_stage or 'standard',
        focus_question_fingerprint=focus_question_fingerprint or '',
        score=result.get('score') or 0,
        total_questions=result.get('total') or 0,
        correct_count=result.get('correct') or 0,
    )

    return attempt, {
        'wrong_book': _build_wrong_book_snapshot(user, material),
        'knowledge_groups': _build_knowledge_group_summary(user, material),
        'review_recommendations': review_recommendations,
        'difficulty_stage': difficulty_stage or 'standard',
    }


def _grade_quiz_questions(questions, answers):
    def _norm(value):
        return _normalize_practice_text(value)

    total = len(questions)
    correct_count = 0
    details = []
    for index, question in enumerate(questions):
        q_key = f'q{index}'
        submitted = answers.get(q_key) or answers.get(str(index)) if isinstance(answers, dict) else None
        submitted_norm = _norm(submitted)
        correct_raw = _get_question_correct_answer(question)
        question_fingerprint = _build_question_fingerprint(question)
        knowledge_tag = _infer_question_knowledge_tag(question)
        question_type = str(question.get('type') or '').strip().lower()
        
        if question_type == 'multiple_choice':
            try:
                submitted_list = json.loads(submitted) if submitted else []
            except (json.JSONDecodeError, TypeError):
                submitted_list = [submitted] if submitted else []
            submitted_norms = [_norm(item) for item in submitted_list]
            
            if isinstance(correct_raw, (list, tuple)):
                correct_norms = [_norm(item) for item in correct_raw]
                correct_flag = set(submitted_norms) == set(correct_norms)
                correct_display = correct_raw
            else:
                correct_norms = [_norm(correct_raw)]
                correct_flag = set(submitted_norms) == set(correct_norms)
                correct_display = correct_raw
            grading_source = 'exact'
            grading_note = ''
        elif isinstance(correct_raw, (list, tuple)):
            correct_norms = [_norm(item) for item in correct_raw]
            correct_flag = submitted_norm in correct_norms
            correct_display = correct_raw
            grading_source = 'exact'
            grading_note = ''
        else:
            if question_type == 'short_answer' and submitted_norm:
                short_answer_result = _grade_short_answer_with_ai(
                    question.get('question') or question.get('text') or '',
                    correct_raw,
                    submitted,
                    question.get('explanation') or '',
                )
                correct_flag = bool(short_answer_result.get('correct_flag'))
                grading_source = short_answer_result.get('grading_source') or 'rule'
                grading_note = short_answer_result.get('grading_note') or ''
            elif question_type == 'true_false':
                correct_flag = _normalize_true_false_token(submitted) == _normalize_true_false_token(correct_raw)
                grading_source = 'exact'
                grading_note = ''
            else:
                correct_flag = submitted_norm == _norm(correct_raw)
                grading_source = 'exact'
                grading_note = ''
            correct_display = correct_raw
        if correct_flag:
            correct_count += 1
        details.append({
            'question': question.get('question') or question.get('text') or '',
            'submitted': submitted,
            'correct': correct_display,
            'correct_flag': bool(correct_flag),
            'explanation': question.get('explanation') or '',
            'source_page': question.get('source_page') or '',
            'source_heading': question.get('source_heading') or '',
            'knowledge_tag': knowledge_tag,
            'question_fingerprint': question_fingerprint,
            'grading_source': grading_source,
            'grading_note': grading_note,
        })

    score = round((correct_count / total) * 100, 2) if total > 0 else 0.0
    return {
        'score': score,
        'total': total,
        'correct': correct_count,
        'details': details,
    }


def _template_blueprint_chapters(title: str):
    """通用骨架章节——仅在 AI 生成大纲失败/未配置大模型时兜底。"""
    chapter_titles = [
        '课程导入与全景理解',
        '核心概念与关键方法',
        '典型案例与步骤拆解',
        '综合练习与复盘提升',
    ]
    chapter_templates = [
        '建立整体认知，明确为什么学、学什么、怎么学。',
        '提炼高频概念、公式、方法与判断框架。',
        '用例题或案例把抽象内容落到具体步骤。',
        '通过练习、测验和复盘巩固课程成果。',
    ]
    chapters = []
    for index, base_title in enumerate(chapter_titles, start=1):
        chapters.append({
            'id': f'chapter_{index}',
            'title': f'第{index}章 {base_title}',
            'summary': chapter_templates[index - 1],
            'objectives': [
                f'理解本章与“{title}”整体目标的关系',
                f'掌握本章最核心的 1-2 个关键点',
            ],
            'estimated_hours': 1.5 if index < 4 else 2.0,
            'resources': ['doc', 'ppt', 'quiz'] if index < 4 else ['doc', 'ppt', 'quiz', 'code'],
        })
    return chapters


def _build_course_blueprint(topic: str, description: str = '', user=None, use_ai: bool = True):
    title = (topic or '').strip() or '未命名课程'
    description_text = (description or '').strip()

    profile = None
    try:
        profile = getattr(user, 'student_profile', None) if user else None
    except Exception:
        profile = None

    knowledge_profile = getattr(profile, 'knowledge_profile', {}) or {}
    learning_goals = list(getattr(profile, 'learning_goals', []) or [])
    learning_preferences = getattr(profile, 'learning_preferences', {}) or {}
    cognitive_style = getattr(profile, 'cognitive_style', '') or ''

    level = knowledge_profile.get('overall') or '初级'
    preferred_mode = learning_preferences.get('preferred_mode') or cognitive_style or '讲练结合'
    objectives = learning_goals[:3] or [
        f'建立关于“{title}”的整体知识框架',
        f'掌握“{title}”中的核心概念与基本方法',
        f'能够围绕“{title}”完成基础练习或简单应用',
    ]

    # 让 AI 真正生成主题相关的大纲章节；失败/未配置大模型时才退回通用骨架。
    chapters = None
    outline_source = 'template'
    if use_ai:
        try:
            from agent_system.planner_agent import PlannerAgent
            from agent_system.generation import _planner_blueprint_to_display
            planner = PlannerAgent(user, grade_level='college')
            gen_outline = planner.generate_outline(topic=title, description=description_text, duration=90)
            disp = _planner_blueprint_to_display({'blueprint': gen_outline}, title)
            if disp and disp.get('chapters'):
                chapters = disp['chapters']
                if disp.get('objectives'):
                    objectives = disp['objectives']
                outline_source = 'ai'
        except Exception:
            logger.exception('AI 生成课程大纲失败，退回通用骨架')

    if not chapters:
        chapters = _template_blueprint_chapters(title)

    total_hours = round(sum(item.get('estimated_hours', 0) or 0 for item in chapters), 1)
    return {
        'schema_version': 2,
        'generation_phase': 'blueprint',
        'title': title,
        'description': description_text,
        'blueprint': {
            'audience': {
                'level': level,
                'preferred_mode': preferred_mode,
                'cognitive_style': cognitive_style or '未指定',
            },
            'objectives': objectives,
            'estimated_hours': total_hours,
            'chapter_count': len(chapters),
            'chapters': chapters,
            'outline_source': outline_source,
        },
        'resources': {},
    }


def _normalize_course_topic(raw_topic: str) -> str:
    text = re.sub(r'\s+', ' ', str(raw_topic or '')).strip().strip('，。,！？!?；;：:')
    if not text:
        return ''

    request_patterns = [
        r'^(?:我)?(?:想|想要|想先|准备|打算|希望)?(?:学习一下|了解一下|学一下|学习|学|了解|掌握|复习|请你讲解一下|请讲解一下|讲解一下|请你讲解|请讲解|讲解)(.+)$',
        r'^(?:请|麻烦|帮我|请你|能不能|可以)?(?:帮我)?(?:学习一下|讲解一下|介绍一下|说明一下|分析一下|学习|讲解|介绍|说明|分析)(.+)$',
        r'^(?:我想知道|我想了解|我想弄懂|我想搞懂)(.+)$',
    ]
    for pattern in request_patterns:
        match = re.match(pattern, text)
        if match:
            text = match.group(1).strip()
            break

    trailing_patterns = [
        r'(.+?)(?:是|到底是)?怎么起作用的$',
        r'(.+?)怎么学会$',
        r'(.+?)怎么学习$',
        r'(.+?)怎么理解$',
        r'(.+?)是什么$',
        r'(.+?)的作用$',
        r'(.+?)相关内容$',
    ]
    for pattern in trailing_patterns:
        match = re.match(pattern, text)
        if match:
            candidate = match.group(1).strip('“”"《》<>：:，,；;。！？!? ')
            if candidate:
                text = candidate
                break

    learned = re.search(r'学习(.+?)(?:[,，。！？!?]|$)', text)
    if learned:
        candidate = learned.group(1).strip('“”"《》<>：:，,；;。！？!? ')
        if candidate:
            text = candidate

    text = text.strip('“”"《》<>：:，,；;。！？!? ')
    return text or str(raw_topic or '').strip()


def _schedule_outline_generation(outline, topic: str, user):
    task_id = None
    try:
        from agent_system.models import AgentTask
        task = AgentTask.objects.create(
            user=user,
            name=f"Generate course: {outline.title}",
            input_data={'outline_id': outline.id, 'topic': topic},
            status='pending',
            progress=outline.progress,  # 同步 CourseOutline 的进度
        )
        task_id = task.id
        logger.info(f'Created AgentTask {task_id} for outline {outline.id}')
    except Exception as e:
        logger.exception(f'Failed to create AgentTask: {e}')
        return None

    def _run_generation(tid, user_obj, topic_str):
        logger.info(f'=== generation thread START === task_id={tid}, topic={topic_str}')
        outline_id = None
        try:
            from agent_system.agents import orchestrate_generate_resources
            from agent_system.models import AgentTask
            task_obj = AgentTask.objects.get(pk=tid)
            input_data = task_obj.input_data
            if isinstance(input_data, dict):
                outline_id = input_data.get('outline_id')
            elif isinstance(input_data, str):
                try:
                    import json as _json
                    outline_id = _json.loads(input_data).get('outline_id')
                except Exception:
                    pass
            orchestrate_generate_resources(user_obj, topic_str, resource_types=None, task=task_obj, outline_id=outline_id)
            logger.info(f'=== generation thread END === task_id={tid}, completed')
        except Exception as ex:
            logger.exception("Local generation failed for task %s: %s", tid, ex)
            try:
                from agent_system.models import AgentTask as _AgentTask
                _AgentTask.objects.filter(pk=tid).update(status='failed', output_data={'error': str(ex)})
            except Exception:
                logger.exception("Failed to update AgentTask after failure %s", tid)
            try:
                if outline_id:
                    CourseOutline.objects.filter(pk=outline_id).update(status='failed', progress=100)
            except Exception:
                logger.exception("Failed to mark outline failed after failure %s", tid)

    try:
        from agent_system.tasks import run_agent_task
        job = run_agent_task.delay(task.id)
        try:
            data = task.output_data or {}
            data['celery_job_id'] = getattr(job, 'id', str(job))
            task.output_data = data
        except Exception:
            logger.exception("Failed to attach job id to AgentTask %s", getattr(task, 'id', None))
        task.status = 'pending'
        task.save(update_fields=['output_data', 'status'])
    except Exception as e:
        logger.exception("Celery call failed, falling back to local thread: %s", e)
        import threading
        threading.Thread(target=_run_generation, args=(task.id, user, topic), daemon=False).start()
        logger.info(f'Started generation thread for task {task.id}')
        try:
            task.status = 'running'
            task.save(update_fields=['status'])
        except Exception:
            task.status = 'running'
            task.save()

    try:
        outline.status = 'generating'
        outline.progress = max(outline.progress or 0, 8)
        outline.save(update_fields=['status', 'progress', 'updated_at'])
    except Exception:
        outline.status = 'generating'
        outline.progress = max(outline.progress or 0, 8)
        outline.save()

    return task_id


@login_required
@require_POST
def generate_course(request):
    """AJAX: create a CourseOutline and schedule generation (quick response).

    POST params:
    - topic/title: course topic
    - description: optional description
    - sync=1: optional, run generation synchronously (for demo/testing)
    """
    raw_topic = request.POST.get('topic') or request.POST.get('title') or ''
    topic = _normalize_course_topic(raw_topic)
    description = request.POST.get('description', '')
    if not topic or not topic.strip():
        return JsonResponse({'success': False, 'error': 'missing topic'}, status=400)

    # 初始蓝图用【快速模板】占位即可，别在这里同步调 LLM 生成章节——那会让 POST 卡住、
    # 前端一直停在"正在提交"，进不到进度条。真正的 AI 大纲由异步生成任务(GenerationManager
    # ._generate_outline→PlannerAgent)重新生成并写回，此处同步生成纯属冗余。
    blueprint = _build_course_blueprint(topic, description, request.user, use_ai=False)

    # create initial CourseOutline with a structured blueprint skeleton
    outline = CourseOutline.objects.create(
        user=request.user,
        title=topic.strip(),
        description=description.strip() if description else '',
        estimated_hours=(blueprint.get('blueprint') or {}).get('estimated_hours') or 0,
        outline_data=json.dumps(blueprint, ensure_ascii=False),
        status='pending',
        progress=5,
    )
    task_id = None

    # If sync generation is requested, attempt to run the generator inline
    if request.POST.get('sync') == '1':
        try:
            from agent_system.agents import orchestrate_generate_resources
            from agent_system.generation import _smart_build_slide_deck, normalize_animation_assets, slides_to_markdown
            outline.status = 'generating'
            outline.progress = max(outline.progress or 0, 8)
            outline.save(update_fields=['status', 'progress'])

            if getattr(settings, 'XINGHUO_API_URL', '') and getattr(settings, 'XINGHUO_API_KEY', ''):
                results = orchestrate_generate_resources(request.user, topic, resource_types=None, task=None, outline_id=outline.id)
                try:
                    outline.refresh_from_db()
                except Exception:
                    pass
                # 生成过程若已把大纲标记为失败（接口不可用），不要覆盖成 completed，直接反馈失败
                if outline.status == 'failed' or (isinstance(results, dict) and results.get('_generation_error')):
                    err = (results or {}).get('_generation_error') or 'AI 接口暂时不可用，课程内容生成失败，请稍后重试。'
                    return JsonResponse({'success': False, 'outline_id': outline.id, 'status': 'failed', 'error': err}, status=502)
                outline.status = 'completed'
                outline.progress = 100
                outline.save()
                try:
                    od = json.loads(outline.outline_data) if outline.outline_data else None
                except Exception:
                    od = outline.outline_data
                return JsonResponse({'success': True, 'outline_id': outline.id, 'task_id': task_id, 'generated': True, 'outline_data': od, 'results': results})
            else:
                # 未配置大模型的演示分支：直接用通用骨架兜底（不必尝试 AI 生成）
                sample = _build_course_blueprint(topic, description, request.user, use_ai=False)
                sample['generation_phase'] = 'courseware_ready'
                structured_slides = _smart_build_slide_deck(topic.strip(), sample)
                slide_markdown = slides_to_markdown(structured_slides, topic.strip())
                animations = normalize_animation_assets(None, topic.strip(), sample)
                sample['slide_deck'] = structured_slides
                sample['slides'] = slide_markdown
                sample['animations'] = animations
                sample.setdefault('resources', {})['ppt'] = {
                    'status': 'done',
                    'title': f'{topic.strip()} - 讲解课件',
                    'slides': slide_markdown,
                    'structured_slides': structured_slides,
                    'preview': '\n\n'.join(slide_markdown[:20]),
                }
                sample['resources']['animation'] = {
                    'status': 'done',
                    'title': f'{topic.strip()} - H5 动画',
                    'items': animations,
                    'preview': json.dumps([{'concept_name': item.get('concept_name'), 'animation_type': item.get('animation_type')} for item in animations], ensure_ascii=False),
                }
                outline.outline_data = json.dumps(sample, ensure_ascii=False)
                outline.status = 'completed'
                outline.progress = 100
                outline.save()
                try:
                    from .models import Slide, Animation
                    Slide.objects.update_or_create(course_outline=outline, chapter_id='ppt_main', defaults={'slide_data': json.dumps(structured_slides, ensure_ascii=False)})
                    Animation.objects.filter(course_outline=outline).delete()
                    for item in animations:
                        Animation.objects.create(
                            course_outline=outline,
                            chapter_id=item.get('chapter_id') or 'chapter_1',
                            concept_name=item.get('concept_name') or topic.strip(),
                            animation_code=item.get('animation_code') or '',
                            animation_type=item.get('animation_type') or 'css',
                        )
                except Exception:
                    logger.exception('Failed to persist fallback courseware assets')
                return JsonResponse({'success': True, 'outline_id': outline.id, 'task_id': task_id, 'generated': False, 'note': 'XINGHUO not configured; returned placeholder outline', 'outline_data': sample})
        except Exception as e:
            logger.exception("Sync generation failed")
            # 同步模式失败时，也调度异步任务继续生成
            task_id = _schedule_outline_generation(outline, topic, request.user)
            return JsonResponse({'success': True, 'outline_id': outline.id, 'task_id': task_id, 'blueprint_ready': True, 'status': 'pending', 'note': 'Sync generation failed; falling back to async'})

    # 非同步模式：直接调度异步任务开始生成（不再需要确认蓝图）
    outline.status = 'generating'
    outline.progress = 8
    outline.save(update_fields=['status', 'progress'])
    task_id = _schedule_outline_generation(outline, topic, request.user)
    return JsonResponse({'success': True, 'outline_id': outline.id, 'task_id': task_id, 'status': 'generating', 'message': '课程正在生成中'})


@login_required
def course_generator_view(request):
    outlines = CourseOutline.objects.filter(user=request.user).order_by('-created_at')
    context = {'outlines': outlines}
    return render(request, 'curriculum/course_generator.html', context)


@login_required
def course_outline_status_view(request, outline_id):
    """API: 返回课程大纲的生成状态（用于轮询）"""
    try:
        outline = CourseOutline.objects.get(id=outline_id, user=request.user)
        gen_error = ''
        try:
            _od = outline.outline_data
            if isinstance(_od, str):
                _od = json.loads(_od)
            if isinstance(_od, dict):
                gen_error = _od.get('generation_error') or ''
        except Exception:
            gen_error = ''
        return JsonResponse({
            'status': outline.status,
            'progress': outline.progress or 0,
            'message': gen_error if (outline.status == 'failed' and gen_error) else f'生成进度: {outline.progress}%',
            'error': gen_error if outline.status == 'failed' else '',
            'outline_id': outline.id,
        })
    except CourseOutline.DoesNotExist:
        return JsonResponse({'status': 'not_found', 'error': '课程不存在'}, status=404)


@login_required
def teacher_course_library_view(request):
    if request.method == 'POST':
        form = TeacherCourseForm(request.POST)
        if form.is_valid():
            course = form.save(commit=False)
            course.owner = request.user
            course.source_type = 'uploaded'
            course.save()
            messages.success(request, '课程已创建，可以继续上传资料。')
            return redirect('teacher_course_detail', course_id=course.id)
    else:
        form = TeacherCourseForm()

    courses = Course.objects.filter(owner=request.user).prefetch_related('materials')
    published_count = courses.filter(status='published').count()
    context = {
        'form': form,
        'courses': courses,
        'published_count': published_count,
    }
    return render(request, 'curriculum/teacher_course_library.html', context)


def _reorder_course_material(course, material, direction):
    """把某份资料上移/下移一位。先把全部资料按当前顺序重新编号成连续序列
    （历史数据 display_order 可能重复/不连续），再与相邻的一份互换序号，
    避免"顺序值相同导致移动无效"的问题。"""
    ordered = list(course.materials.order_by('display_order', 'created_at'))
    for i, m in enumerate(ordered):
        if m.display_order != i:
            CourseMaterial.objects.filter(pk=m.pk).update(display_order=i)
            m.display_order = i

    idx = next((i for i, m in enumerate(ordered) if m.id == material.id), None)
    if idx is None:
        return
    swap_idx = idx - 1 if direction == 'up' else idx + 1
    if 0 <= swap_idx < len(ordered):
        current, other = ordered[idx], ordered[swap_idx]
        current.display_order, other.display_order = other.display_order, current.display_order
        CourseMaterial.objects.filter(pk=current.pk).update(display_order=current.display_order)
        CourseMaterial.objects.filter(pk=other.pk).update(display_order=other.display_order)


@login_required
def teacher_course_detail_view(request, course_id):
    course = get_object_or_404(Course.objects.prefetch_related('materials'), id=course_id, owner=request.user)

    if request.method == 'POST':
        action = request.POST.get('action', 'upload_material')
        if action == 'publish_course':
            course.status = 'published'
            course.published_at = timezone.now()
            course.save(update_fields=['status', 'published_at', 'updated_at'])
            messages.success(request, '课程已发布，学生现在可以查看。')
            return redirect('teacher_course_detail', course_id=course.id)
        if action == 'archive_course':
            course.status = 'archived'
            course.save(update_fields=['status', 'updated_at'])
            messages.success(request, '课程已归档。')
            return redirect('teacher_course_detail', course_id=course.id)
        if action == 'set_visibility_public':
            course.visibility = 'public'
            course.save(update_fields=['visibility', 'updated_at'])
            messages.success(request, '课程已设为公开，任何人都可以查看。')
            return redirect('teacher_course_detail', course_id=course.id)
        if action == 'set_visibility_private':
            course.visibility = 'private'
            course.save(update_fields=['visibility', 'updated_at'])
            messages.success(request, '课程已设为私有，仅你自己可见。')
            return redirect('teacher_course_detail', course_id=course.id)
        if action in ('move_material_up', 'move_material_down'):
            material_id = request.POST.get('material_id')
            material = get_object_or_404(CourseMaterial, id=material_id, course=course)
            _reorder_course_material(course, material, 'up' if action == 'move_material_up' else 'down')
            return redirect('teacher_course_detail', course_id=course.id)
        if action == 'delete_material':
            material_id = request.POST.get('material_id')
            material = get_object_or_404(CourseMaterial, id=material_id, course=course)
            material_title = material.title
            if material.file:
                material.file.delete(save=False)
            material.delete()
            messages.success(request, f'资料“{material_title}”已删除。')
            return redirect('teacher_course_detail', course_id=course.id)
        if action == 'reprocess_material':
            material_id = request.POST.get('material_id')
            material = get_object_or_404(CourseMaterial, id=material_id, course=course)
            if material.processing_status in {'pending', 'processing'}:
                messages.info(request, f'资料“{material.title}”正在解析中。')
                return redirect('teacher_course_detail', course_id=course.id)

            metadata = material.metadata or {}
            for key in ('parse_error', 'parse_failed_at', 'parse_started_at', 'parse_completed_at', 'chunk_count', 'agent_task_id'):
                metadata.pop(key, None)
            material.metadata = metadata
            material.processing_status = 'pending'
            material.save(update_fields=['metadata', 'processing_status', 'updated_at'])

            task = enqueue_course_material_processing(material)
            launch_mode = ''
            try:
                launch_mode = ((task.output_data or {}).get('launch_mode') or '')
            except Exception:
                launch_mode = ''
            if launch_mode == 'subprocess':
                messages.success(request, f'资料“{material.title}”已重新加入解析队列，后台任务 #{task.id} 正在独立进程中运行。')
            else:
                messages.success(request, f'资料“{material.title}”已重新加入解析队列，任务 #{task.id} 已启动。')
            return redirect('teacher_course_detail', course_id=course.id)

        material_form = CourseMaterialForm(request.POST, request.FILES)
        if material_form.is_valid():
            from .utils.material_parser import infer_material_type_from_filename, title_from_filename
            material = material_form.save(commit=False)
            material.course = course
            material.uploaded_by = request.user
            uploaded_file = request.FILES.get('file')
            filename = uploaded_file.name if uploaded_file else ''
            # 标题留空则用文件名；类型按扩展名自动识别；顺序自动排到末尾——
            # 这三样都不再需要老师手动填。
            if not (material.title or '').strip():
                material.title = title_from_filename(filename) or '未命名资料'
            material.material_type = infer_material_type_from_filename(filename)
            last_order = course.materials.aggregate(models.Max('display_order'))['display_order__max']
            material.display_order = (last_order + 1) if last_order is not None else 0
            material.processing_status = 'pending'
            material.save()
            task = enqueue_course_material_processing(material)
            launch_mode = ''
            try:
                launch_mode = ((task.output_data or {}).get('launch_mode') or '')
            except Exception:
                launch_mode = ''
            if launch_mode == 'subprocess':
                messages.success(request, f'资料已上传，解析任务 #{task.id} 已转到独立后台进程。')
            else:
                messages.success(request, f'资料已上传，并已启动解析任务 #{task.id}。')
            return redirect('teacher_course_detail', course_id=course.id)
    else:
        material_form = CourseMaterialForm()

    materials = course.materials.all()
    context = {
        'course': course,
        'materials': materials,
        'material_form': material_form,
    }
    return render(request, 'curriculum/teacher_course_detail.html', context)


def course_library_view(request):
    courses = list(_course_queryset_for_user(request.user).prefetch_related('materials', 'owner'))
    week_start = timezone.now() - timedelta(days=7)

    if request.user.is_authenticated:
        recent_attempts = list(
            MaterialQuizAttempt.objects.filter(user=request.user)
            .select_related('course', 'material')
            .order_by('-created_at')[:8]
        )
        weekly_attempts = MaterialQuizAttempt.objects.filter(user=request.user, created_at__gte=week_start)
    else:
        recent_attempts = []
        weekly_attempts = MaterialQuizAttempt.objects.none()
    weekly_avg_score = weekly_attempts.aggregate(avg_score=models.Avg('score')).get('avg_score') or 0

    continue_learning = []
    seen_course_ids = set()
    for attempt in recent_attempts:
        if attempt.course_id in seen_course_ids:
            continue
        seen_course_ids.add(attempt.course_id)
        recommended_pages = attempt.recommended_review_pages if isinstance(attempt.recommended_review_pages, list) else []
        review_page = ''
        if recommended_pages:
            review_page = str(recommended_pages[0].get('source_page') or '').strip()
        continue_url = reverse('course_study', args=[attempt.course_id]) + f'?material={attempt.material_id}'
        if review_page:
            continue_url += f'&page={review_page}'
        continue_learning.append({
            'course': attempt.course,
            'material': attempt.material,
            'score': attempt.score,
            'difficulty_stage': attempt.get_difficulty_stage_display(),
            'review_page': review_page,
            'updated_at': attempt.created_at,
            'continue_url': continue_url,
        })
        if len(continue_learning) >= 3:
            break

    if not continue_learning:
        for course in courses[:3]:
            first_material = next(iter(course.materials.all()), None)
            continue_url = reverse('course_study', args=[course.id])
            if first_material:
                continue_url += f'?material={first_material.id}'
            continue_learning.append({
                'course': course,
                'material': first_material,
                'score': None,
                'difficulty_stage': '待开始',
                'review_page': '',
                'updated_at': course.updated_at,
                'continue_url': continue_url,
            })

    if request.user.is_authenticated:
        archived_stat_ids = list(
            MaterialWeakAreaArchive.objects.filter(user=request.user).values_list('question_stat_id', flat=True)
        )

        focus_stats = list(
            MaterialQuestionStat.objects.filter(user=request.user, wrong_count__gt=0)
            .exclude(id__in=archived_stat_ids)
            .select_related('course', 'material')
            .order_by('-consecutive_wrong_count', '-wrong_count', '-last_seen_at')[:4]
        )
        archived_weak_areas = _build_archived_weak_area_context(request.user)
    else:
        focus_stats = []
        archived_weak_areas = []

    weak_areas = []
    for stat in focus_stats:
        target_url = _build_weak_area_target_url(stat.course_id, stat.material_id, stat.source_page)
        weak_areas.append({
            'stat_id': stat.id,
            'knowledge_tag': stat.knowledge_tag or '未归类知识点',
            'course_title': stat.course.title,
            'material_title': stat.material.title,
            'wrong_count': stat.wrong_count,
            'consecutive_wrong_count': stat.consecutive_wrong_count,
            'source_page': stat.source_page,
            'source_heading': stat.source_heading,
            'target_url': target_url,
        })

    featured_courses = []
    for course in courses[:6]:
        materials = list(course.materials.all())
        first_material = materials[0] if materials else None
        study_url = reverse('course_study', args=[course.id])
        if first_material:
            study_url += f'?material={first_material.id}'
        course_attempts = [attempt for attempt in recent_attempts if attempt.course_id == course.id]
        featured_courses.append({
            'course': course,
            'material_count': len(materials),
            'first_material': first_material,
            'study_url': study_url,
            'latest_score': course_attempts[0].score if course_attempts else None,
        })

    context = {
        'courses': courses,
        'continue_learning': continue_learning,
        'weak_areas': weak_areas,
        'archived_weak_areas': archived_weak_areas,
        'featured_courses': featured_courses,
        'learning_summary': {
            'course_count': len(courses),
            'weekly_practice_count': weekly_attempts.count(),
            'weekly_avg_score': weekly_avg_score,
            'focus_count': len(focus_stats),
            'archived_focus_count': len(archived_weak_areas),
        },
    }
    return render(request, 'curriculum/course_library.html', context)


@login_required
def course_study_view(request, course_id):
    course = get_object_or_404(_course_queryset_for_user(request.user), id=course_id)
    materials = list(course.materials.all())
    selected_material = None
    requested_material_id = request.GET.get('material')
    if requested_material_id:
        for material in materials:
            if str(material.id) == str(requested_material_id):
                selected_material = material
                break
    if selected_material is None and materials:
        selected_material = materials[0]

    requested_page = request.GET.get('page')
    initial_page = 1
    if requested_page not in (None, ''):
        try:
            initial_page = max(1, int(requested_page))
        except (TypeError, ValueError):
            initial_page = 1
    if selected_material and selected_material.page_count:
        try:
            initial_page = min(initial_page, max(1, int(selected_material.page_count)))
        except (TypeError, ValueError):
            pass

    preview_kind = _get_material_preview_kind(selected_material) if selected_material else 'download'
    try:
        record_profile_event(
            request.user,
            'course_material_viewed',
            {
                'course_id': course.id,
                'course_title': course.title,
                'material_id': getattr(selected_material, 'id', None),
                'material_title': getattr(selected_material, 'title', ''),
                'page': initial_page,
            },
            source_app='curriculum_app.study',
            course_id=course.id,
            material_id=getattr(selected_material, 'id', None),
            confidence=0.45,
        )
    except Exception:
        logger.exception('Failed to record profile event for course study view')
    # 获取选中材料的学习进度
    has_learning_progress = False
    learning_progress = None
    if selected_material:
        try:
            learning_progress = selected_material.learningprogress_set.filter(user=request.user).first()
            has_learning_progress = learning_progress is not None
        except Exception:
            pass
    
    context = {
        'course': course,
        'materials': materials,
        'selected_material': selected_material,
        'initial_page': initial_page,
        'preview_kind': preview_kind,
        'has_learning_progress': has_learning_progress,
        'learning_progress': learning_progress,
    }
    return render(request, 'curriculum/course_study.html', context)


@login_required
@require_POST
def generate_material_quiz(request, course_id, material_id):
    course = get_object_or_404(_course_queryset_for_user(request.user), id=course_id)

    material = get_object_or_404(CourseMaterial.objects.prefetch_related('chunks'), id=material_id, course=course)
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}') if request.content_type and request.content_type.startswith('application/json') else request.POST.dict()
    except Exception:
        payload = {}

    question_count = _safe_positive_int(payload.get('count'), default=5, minimum=3, maximum=20)
    question_type = payload.get('question_type', 'mixed')
    if question_type not in ['mixed', 'single_choice', 'short_answer', 'true_false']:
        question_type = 'mixed'
    logger.debug(f"[QUIZ DEBUG] Original question_count: {question_count}, question_type: {question_type}")
    variant_seed = _normalize_quiz_variant_seed(payload.get('variant_seed'))
    # 本地 CPU + 7b：出题会分 3 轮、每轮都把整段上下文重新喂给模型。原来 25000 字在 CPU 上
    # 光预填就要几分钟/轮，3 轮直接超过前端 10 分钟超时 → "timeout"。压到 8000 字，每轮快约 3 倍，
    # 稳稳落在超时内。（提交用讯飞星火时它快、能吃大上下文，可把这两个值调大以覆盖更全资料。）
    context_text = _build_material_quiz_context(material, max_chunks=12, max_total_chars=8000)
    focus_question = payload.get('focus_question') if isinstance(payload.get('focus_question'), dict) else None
    practice_profile = _build_practice_profile(request.user, material, focus_question=focus_question)
    adaptive_policy = _get_or_create_adaptive_policy(request.user, course, material)
    adaptive_snapshot = _recompute_adaptive_strategy(adaptive_policy, request.user, material)
    adaptive_strategy = adaptive_snapshot.get('strategy') if isinstance(adaptive_snapshot, dict) else {}
    logger.debug(f"[QUIZ DEBUG] Adaptive delta: {adaptive_strategy.get('question_count_delta')}, difficulty_stage: {practice_profile.get('difficulty_stage')}")
    # 用户明确请求的题目数量不应该被自适应策略覆盖
    # 只在用户没有明确指定数量时，才应用自适应调整
    # question_count = max(3, min(20, question_count + int(adaptive_strategy.get('question_count_delta') or 0)))
    logger.debug(f"[QUIZ DEBUG] Final question_count: {question_count}")
    practice_profile = _apply_adaptive_policy_to_profile(practice_profile, adaptive_strategy, focus_question=focus_question)
    practice_profile['elo_ability_rating'] = round(adaptive_policy.ability_rating or ELO_DEFAULT_RATING, 1)
    practice_profile['elo_gap'] = round(_compute_elo_gap(adaptive_policy, request.user, material), 1)
    generation_notes = _build_practice_generation_notes(practice_profile)
    generation_notes.extend(_build_adaptive_generation_notes(adaptive_snapshot))
    generation_notes.extend(_build_elo_generation_notes(practice_profile))
    if focus_question:
        focus_lines = [
            '请围绕下面这道题对应的知识点生成同类训练题。',
            '要求：考查相同或相近概念，但不要直接复用原题表述；优先保持难度接近，并给出可评分答案与简明解析。',
        ]
        question_text = str(focus_question.get('question') or '').strip()
        correct_answer = str(focus_question.get('correct') or '').strip()
        source_page = str(focus_question.get('source_page') or '').strip()
        source_heading = str(focus_question.get('source_heading') or '').strip()
        explanation = str(focus_question.get('explanation') or '').strip()
        if question_text:
            focus_lines.append(f'参考原题：{question_text}')
        if correct_answer:
            focus_lines.append(f'参考答案：{correct_answer}')
        if source_page:
            focus_lines.append(f'优先覆盖资料页码：第{source_page}页/张')
        if source_heading:
            focus_lines.append(f'优先覆盖资料部分：{source_heading}')
        if explanation:
            focus_lines.append(f'原题解析参考：{explanation}')
        context_text = context_text + '\n\n' + '\n'.join(focus_lines)
    if generation_notes:
        context_text = context_text + '\n\n' + '\n'.join(generation_notes)
    if variant_seed:
        context_text = context_text + '\n\n' + f'题目变体编号：{variant_seed}。请在保持知识点一致的前提下，尽量避免与上一轮题目重复。'
    logger.debug(f"[QUIZ DEBUG] Context text length: {len(context_text)} chars")
    logger.debug(f"[QUIZ DEBUG] Context text preview: {context_text[:200]}...")
    if not context_text:
        return JsonResponse({'success': False, 'error': '当前资料还没有可用于出题的解析内容'}, status=400)

    try:
        from agent_system.agents import QuizAgent
        logger.debug("[QUIZ DEBUG] Creating QuizAgent...")
        agent = QuizAgent(request.user)
        print("[QUIZ DEBUG] QuizAgent created, calling generate_quiz_from_context...")
        quiz_resource = agent.generate_quiz_from_context(
            topic=f"{course.title} / {material.title}",
            context_text=context_text,
            count=question_count,
            question_type=question_type,
            metadata={
                'course_id': course.id,
                'material_id': material.id,
                'material_title': material.title,
                'focus_question': focus_question or {},
                'practice_profile': {
                    'difficulty_stage': practice_profile.get('difficulty_stage'),
                    'difficulty_label': practice_profile.get('difficulty_label'),
                    'focus_question_fingerprint': practice_profile.get('focus_fingerprint'),
                    'elo_ability_rating': practice_profile.get('elo_ability_rating'),
                    'elo_gap': practice_profile.get('elo_gap'),
                },
                'adaptive_policy': _build_adaptive_policy_payload(adaptive_snapshot),
            },
        )
        print("[QUIZ DEBUG] generate_quiz_from_context returned")
        raw_quiz_payload = {}
        try:
            raw_quiz_payload = json.loads(quiz_resource.content or '{}')
        except Exception:
            raw_quiz_payload = {}
        
        raw_question_count = len(raw_quiz_payload.get('questions') or [])
        quiz_payload, questions = _parse_quiz_payload(quiz_resource.content)
        parsed_count = len(questions)
        
        logger.debug(f"[QUIZ DEBUG] Step 1 - After parsing: raw={raw_question_count}, parsed={parsed_count}")
        
        if len(questions) < question_count and raw_question_count >= question_count:
            questions = [dict(q) for q in (raw_quiz_payload.get('questions') or []) if isinstance(q, dict)]
            logger.debug(f"[QUIZ DEBUG] Step 1a - Used raw questions: count={len(questions)}")
        
        parsed_count = len(questions)
        if not questions:
            fallback_questions = _build_fallback_quiz_questions(
                material,
                raw_questions=raw_quiz_payload.get('questions') if isinstance(raw_quiz_payload, dict) else [],
                count=question_count,
                variant_seed=variant_seed,
            )
            if fallback_questions:
                quiz_payload = {'questions': fallback_questions}
                questions = fallback_questions
                quiz_resource.content = json.dumps(quiz_payload, ensure_ascii=False)
                quiz_resource.save(update_fields=['content'])
                logger.debug(f"[QUIZ DEBUG] Step 2 - Used fallback questions: count={len(questions)}")
        questions = _annotate_quiz_questions_with_source(material, questions)
        logger.debug(f"[QUIZ DEBUG] Step 3 - After annotating: count={len(questions)}")
        questions = _prioritize_questions_by_knowledge_tags(
            questions,
            (adaptive_strategy or {}).get('priority_knowledge_tags') or [],
        )
        logger.debug(f"[QUIZ DEBUG] Step 4 - After prioritizing: count={len(questions)}")
        logger.debug(f"[QUIZ DEBUG] Questions before dedupe: {len(questions)}")
        for i, q in enumerate(questions):
            logger.debug(f"[QUIZ DEBUG] Q{i+1}: {str(q.get('question', ''))[:30]}... fingerprint={_build_question_fingerprint(q)}")
        
        questions, dedupe_removed = _dedupe_generated_questions(
            questions,
            recent_fingerprints=practice_profile.get('recent_fingerprints'),
            limit=question_count,
        )
        logger.debug(f"[QUIZ DEBUG] Step 5 - After deduping: count={len(questions)}, removed={dedupe_removed}")
        
        if len(questions) < question_count:
            fallback_questions = _build_fallback_quiz_questions(
                material,
                raw_questions=raw_quiz_payload.get('questions') if isinstance(raw_quiz_payload, dict) else [],
                count=question_count - len(questions),
                variant_seed=variant_seed,
            )
            if fallback_questions:
                questions.extend(fallback_questions)
                logger.debug(f"[QUIZ DEBUG] Step 6 - Added fallback questions: count={len(questions)}")
        
        questions = questions[:question_count]
        final_count = len(questions)
        logger.debug(f"[QUIZ DEBUG] Step 7 - Final: count={final_count}")
        for question in questions:
            if isinstance(question, dict):
                question['question_fingerprint'] = _build_question_fingerprint(question)
        if isinstance(quiz_payload, dict):
            quiz_payload['questions'] = questions
        if quiz_resource and isinstance(quiz_payload, dict):
            quiz_resource.content = json.dumps(quiz_payload, ensure_ascii=False)
            quiz_resource.save(update_fields=['content'])
        if not questions:
            return JsonResponse({'success': False, 'error': '当前资料暂时没能生成可作答的题目，请再试一次。'}, status=502)
        if focus_question and practice_profile.get('focus_stat_id'):
            MaterialQuestionStat.objects.filter(id=practice_profile['focus_stat_id']).update(
                similar_generation_count=models.F('similar_generation_count') + 1
            )
        try:
            record_profile_event(
                request.user,
                'material_quiz_generated',
                {
                    'course_id': course.id,
                    'course_title': course.title,
                    'material_id': material.id,
                    'material_title': material.title,
                    'quiz_resource_id': quiz_resource.id,
                    'question_count': len(questions),
                    'difficulty_stage': practice_profile.get('difficulty_stage'),
                    'focus_stat_id': practice_profile.get('focus_stat_id'),
                },
                source_app='curriculum_app.quiz',
                course_id=course.id,
                material_id=material.id,
                confidence=0.5,
                dedupe_key=f'material_quiz_generated:{quiz_resource.id}',
            )
        except Exception:
            logger.exception('Failed to record profile event for generated material quiz %s', getattr(quiz_resource, 'id', None))
        logger.info(f"Quiz generation stats: target={question_count}, raw={raw_question_count}, parsed={parsed_count}, final={final_count}, dedupe_removed={dedupe_removed}")
        
        logger.debug(f"[QUIZ DEBUG] Returning quiz: questions_count={len(quiz_payload.get('questions', [])) if isinstance(quiz_payload, dict) else 'N/A'}")
        
        return JsonResponse({
            'success': True,
            'quiz_resource_id': quiz_resource.id,
            'quiz': quiz_payload,
            'question_count': len(questions),
            'material': {'id': material.id, 'title': material.title},
            'practice_profile': {
                'difficulty_stage': practice_profile.get('difficulty_stage'),
                'difficulty_label': practice_profile.get('difficulty_label'),
                'dedupe_removed': dedupe_removed,
                'elo_ability_rating': practice_profile.get('elo_ability_rating'),
                'elo_gap': practice_profile.get('elo_gap'),
            },
            'adaptive_policy': _build_adaptive_policy_payload(adaptive_snapshot),
        })
    except Exception as exc:
        logger.exception('Failed to generate material quiz for course %s material %s', course_id, material_id)
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)


@login_required
@require_POST
def submit_material_quiz(request, course_id, material_id):
    course = get_object_or_404(_course_queryset_for_user(request.user), id=course_id)

    material = get_object_or_404(CourseMaterial, id=material_id, course=course)
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}') if request.content_type and request.content_type.startswith('application/json') else request.POST.dict()
    except Exception:
        payload = {}

    quiz_resource_id = payload.get('quiz_resource_id')
    logger.debug(f"[QUIZ DEBUG] submit_material_quiz: quiz_resource_id={quiz_resource_id}")
    logger.debug(f"[QUIZ DEBUG] submit_material_quiz: payload keys={list(payload.keys())}")
    quiz_resource, questions = _load_quiz_questions_from_resource(quiz_resource_id, request.user, course.id, material.id)
    logger.debug(f"[QUIZ DEBUG] submit_material_quiz: loaded {len(questions)} questions")
    if not questions:
        quiz_payload = payload.get('quiz') or {}
        quiz_resource = None
        _, questions = _parse_quiz_payload(quiz_payload)
    if not questions:
        return JsonResponse({'success': False, 'error': '没有可评分的题目'}, status=400)

    submitted_fingerprints = payload.get('question_fingerprints') if isinstance(payload, dict) else None
    if submitted_fingerprints:
        fingerprint_mismatch = False
        if isinstance(submitted_fingerprints, dict):
            for index, question in enumerate(questions):
                expected = _build_question_fingerprint(question)
                submitted = str(
                    submitted_fingerprints.get(f'q{index}')
                    or submitted_fingerprints.get(str(index))
                    or ''
                ).strip()
                if not submitted or submitted != expected:
                    fingerprint_mismatch = True
                    break
        elif isinstance(submitted_fingerprints, list):
            if len(submitted_fingerprints) < len(questions):
                fingerprint_mismatch = True
            else:
                for index, question in enumerate(questions):
                    expected = _build_question_fingerprint(question)
                    submitted = str(submitted_fingerprints[index] or '').strip()
                    if not submitted or submitted != expected:
                        fingerprint_mismatch = True
                        break
        if fingerprint_mismatch:
            return JsonResponse({'success': False, 'error': '题目版本已更新，请重新生成后再提交。'}, status=409)

    # 从payload中提取答案（支持JSON和FormData格式）
    answers = payload.get('answers') or {}
    if not answers and isinstance(payload, dict):
        answers = {k: v for k, v in payload.items() if k.startswith('q') and k[1:].isdigit()}
    logger.debug(f"[QUIZ DEBUG] Answers received: {answers}")
    result = _grade_quiz_questions(questions, answers)
    resource_metadata = quiz_resource.metadata if quiz_resource and isinstance(quiz_resource.metadata, dict) else {}
    practice_profile = resource_metadata.get('practice_profile') if isinstance(resource_metadata.get('practice_profile'), dict) else {}
    adaptive_policy = _get_or_create_adaptive_policy(request.user, course, material)
    ability_rating_before = adaptive_policy.ability_rating
    # 事务包裹：每题 stat、ELO(ability_rating)、attempt 记录必须要么全成、要么全滚回，
    # 否则中途异常会留下错题本/ELO 已加分却无对应 attempt 的脏数据，客户端重试还会二次计数
    with transaction.atomic():
        attempt_record, practice_insights = _persist_material_quiz_records(
            request.user,
            course,
            material,
            quiz_resource,
            questions,
            answers,
            result,
            difficulty_stage=practice_profile.get('difficulty_stage') or 'standard',
            focus_question_fingerprint=practice_profile.get('focus_question_fingerprint') or '',
            adaptive_policy=adaptive_policy,
        )
    try:
        record_profile_event(
            request.user,
            'material_quiz_submitted',
            {
                'attempt_id': attempt_record.id,
                'course_id': course.id,
                'course_title': course.title,
                'material_id': material.id,
                'material_title': material.title,
                'score': attempt_record.score,
                'total_questions': attempt_record.total_questions,
                'correct_count': attempt_record.correct_count,
                'knowledge_tags': attempt_record.knowledge_tags or [],
                'review_recommendations': practice_insights.get('review_recommendations') if isinstance(practice_insights, dict) else [],
                'difficulty_stage': attempt_record.difficulty_stage,
            },
            source_app='curriculum_app',
            course_id=course.id,
            material_id=material.id,
            confidence=0.9,
            dedupe_key=f'material_quiz_attempt:{attempt_record.id}',
        )
    except Exception:
        logger.exception('Failed to record profile event for quiz attempt %s', getattr(attempt_record, 'id', None))

    # 发送画像自动更新信号
    try:
        from agent_system.services.profile_signal_collector import ProfileSignalCollector, ProfileSignalType
        
        is_correct = attempt_record.correct_count > 0
        knowledge_tags = attempt_record.knowledge_tags or []
        
        if is_correct:
            ProfileSignalCollector.emit(
                user=request.user,
                signal_type=ProfileSignalType.QUIZ_CORRECT,
                trigger_source='material_quiz',
                data={
                    'attempt_id': attempt_record.id,
                    'knowledge_tags': knowledge_tags,
                    'is_correct': True,
                    'difficulty': attempt_record.difficulty_stage,
                    'score': attempt_record.score,
                },
                course_id=course.id,
                material_id=material.id,
            )
        else:
            # 查找每道题的连续错误次数，取最大值
            # 注意：MaterialQuestionStat 按 question_fingerprint 分组，需要按 knowledge_tag 聚合
            consecutive_wrong = 0
            for tag in knowledge_tags:
                # 获取该知识点下所有题目的统计，取最大连续错误
                stats = MaterialQuestionStat.objects.filter(
                    user=request.user,
                    material=material,
                    knowledge_tag=tag
                )
                for stat in stats:
                    if stat.consecutive_wrong_count:
                        consecutive_wrong = max(consecutive_wrong, stat.consecutive_wrong_count)
            
            ProfileSignalCollector.emit(
                user=request.user,
                signal_type=ProfileSignalType.QUIZ_WRONG,
                trigger_source='material_quiz',
                data={
                    'attempt_id': attempt_record.id,
                    'knowledge_tags': knowledge_tags,
                    'is_correct': False,
                    'difficulty': attempt_record.difficulty_stage,
                    'consecutive_wrong': consecutive_wrong,
                },
                course_id=course.id,
                material_id=material.id,
            )
    except Exception:
        logger.exception('Failed to emit profile signal for quiz attempt')
    auto_learning_adjustment = None
    try:
        auto_learning_adjustment = _maybe_auto_refresh_learning_plan_from_quiz(
            request.user,
            course,
            material,
            attempt_record,
            practice_insights,
        )
    except Exception:
        logger.exception('Auto learning plan refresh failed for attempt %s', attempt_record.id)
    adaptive_snapshot = _recompute_adaptive_strategy(adaptive_policy, request.user, material)
    result.update({
        'success': True,
        'material': {'id': material.id, 'title': material.title},
        'quiz_resource_id': quiz_resource_id,
        'practice_insights': practice_insights,
        'quiz_attempt_id': attempt_record.id,
        'adaptive_policy': _build_adaptive_policy_payload(adaptive_snapshot),
        'auto_learning_adjustment': auto_learning_adjustment,
        'ability_rating': round(adaptive_policy.ability_rating, 1),
        'ability_rating_delta': round(adaptive_policy.ability_rating - ability_rating_before, 1),
    })
    return JsonResponse(result)


@login_required
@require_POST
def submit_material_quiz_feedback(request, course_id, material_id):
    course = get_object_or_404(_course_queryset_for_user(request.user), id=course_id)

    material = get_object_or_404(CourseMaterial, id=material_id, course=course)
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}') if request.content_type and request.content_type.startswith('application/json') else request.POST.dict()
    except Exception:
        payload = {}

    feedback_type = str(payload.get('feedback_type') or '').strip()
    if feedback_type not in {'useful', 'not_useful', 'too_easy', 'too_hard', 'off_topic'}:
        return JsonResponse({'success': False, 'error': '反馈类型无效'}, status=400)

    attempt_id = payload.get('quiz_attempt_id')
    if not attempt_id:
        return JsonResponse({'success': False, 'error': '缺少 quiz_attempt_id'}, status=400)

    attempt = MaterialQuizAttempt.objects.filter(
        id=attempt_id,
        user=request.user,
        course=course,
        material=material,
    ).first()
    if not attempt:
        return JsonResponse({'success': False, 'error': '练习记录不存在'}, status=404)

    feedback_note = str(payload.get('feedback_note') or '').strip()
    question_fingerprint = str(payload.get('question_fingerprint') or '').strip()
    stat = None
    if question_fingerprint:
        stat = MaterialQuestionStat.objects.filter(
            user=request.user,
            material=material,
            question_fingerprint=question_fingerprint,
        ).first()

    result_payload = attempt.result if isinstance(attempt.result, dict) else {}
    feedback_history = list(result_payload.get('user_feedback') or [])
    feedback_item = {
        'feedback_type': feedback_type,
        'feedback_note': feedback_note[:200],
        'question_fingerprint': question_fingerprint,
        'created_at': timezone.now().isoformat(),
    }
    feedback_history.append(feedback_item)
    result_payload['user_feedback'] = feedback_history[-20:]
    attempt.result = result_payload
    attempt.save(update_fields=['result'])

    try:
        record_profile_event(
            request.user,
            'material_quiz_feedback',
            {
                'course_id': course.id,
                'material_id': material.id,
                'quiz_attempt_id': attempt.id,
                'feedback_type': feedback_type,
                'feedback_note': feedback_note[:200],
                'question_fingerprint': question_fingerprint,
                'knowledge_tag': getattr(stat, 'knowledge_tag', '') if stat else '',
            },
            source_app='curriculum_app.quiz_feedback',
            course_id=course.id,
            material_id=material.id,
            confidence=0.6,
        )
    except Exception:
        logger.exception('Failed to record profile event for material quiz feedback %s', attempt.id)

    # 发送画像自动更新信号（偏好反馈）
    try:
        from agent_system.services.profile_signal_collector import ProfileSignalCollector, ProfileSignalType
        
        knowledge_tag = str(getattr(stat, 'knowledge_tag', '') or '').strip()
        
        ProfileSignalCollector.emit(
            user=request.user,
            signal_type=ProfileSignalType.QUIZ_FEEDBACK,
            trigger_source='material_quiz_feedback',
            data={
                'feedback_type': feedback_type,
                'knowledge_tag': knowledge_tag,
                'quiz_attempt_id': attempt.id,
            },
            course_id=course.id,
            material_id=material.id,
        )
    except Exception:
        logger.exception('Failed to emit profile signal for quiz feedback')

    adaptive_policy = _get_or_create_adaptive_policy(request.user, course, material)
    feedback_counts = _normalize_feedback_counts(adaptive_policy.feedback_counts)
    feedback_counts[feedback_type] = feedback_counts.get(feedback_type, 0) + 1
    adaptive_policy.feedback_counts = feedback_counts

    strategy = _normalize_adaptive_strategy(adaptive_policy.strategy)
    knowledge_feedback = strategy.get('knowledge_feedback') if isinstance(strategy.get('knowledge_feedback'), dict) else {}
    knowledge_tag = str(getattr(stat, 'knowledge_tag', '') or '').strip()
    if knowledge_tag:
        bucket = _normalize_feedback_counts(knowledge_feedback.get(knowledge_tag))
        bucket[feedback_type] = bucket.get(feedback_type, 0) + 1
        knowledge_feedback[knowledge_tag] = bucket
        strategy['knowledge_feedback'] = knowledge_feedback
        adaptive_policy.strategy = strategy

    update_fields = ['feedback_counts', 'updated_at']
    if knowledge_tag:
        update_fields.append('strategy')
    adaptive_policy.save(update_fields=update_fields)

    adaptive_snapshot = _recompute_adaptive_strategy(adaptive_policy, request.user, material)
    return JsonResponse({
        'success': True,
        'feedback_type': feedback_type,
        'quiz_attempt_id': attempt.id,
        'knowledge_tag': knowledge_tag,
        'adaptive_policy': _build_adaptive_policy_payload(adaptive_snapshot),
    })


@login_required
def learning_progress_view(request):
    progress_list = LearningProgress.objects.filter(user=request.user).select_related('course_outline')
    recent_plans = []
    for item in LearningPlan.objects.filter(user=request.user).order_by('-updated_at')[:5]:
        recent_plans.append({
            'id': item.id,
            'title': item.title,
            'status': item.status,
            'data': _safe_json_loads(item.plan_data, {}),
            'updated_at': item.updated_at,
        })
    context = {'progress_list': progress_list, 'recent_plans': recent_plans}
    return render(request, 'curriculum/learning_progress.html', context)


def _build_course_ai_focus_snapshot(course, latest_plan, selected_material=None, current_page=''):
    matched_course = latest_plan.get('matched_course') if isinstance((latest_plan or {}).get('matched_course'), dict) else {}
    weak_areas = [str(item).strip() for item in ((latest_plan or {}).get('weak_areas') or []) if str(item).strip()][:3]
    recommendation_reason = [str(item).strip() for item in ((latest_plan or {}).get('recommendation_reason') or []) if str(item).strip()][:3]
    top_module_name = str((latest_plan or {}).get('top_module_name') or '').strip()
    top_module_focus = str((latest_plan or {}).get('top_module_focus') or '').strip()
    focus = {
        'matched_course_title': matched_course.get('title') or getattr(course, 'title', ''),
        'top_module_name': top_module_name,
        'top_module_focus': top_module_focus,
        'weak_areas': weak_areas,
        'recommendation_reason': recommendation_reason,
        'current_page': str(current_page or '').strip(),
        'selected_material_title': getattr(selected_material, 'title', '') or '',
        'suggested_action': '',
        'suggested_question': '',
        'evidence_points': [],
    }

    if focus['weak_areas']:
        action = '先补“' + '、'.join(focus['weak_areas'][:2]) + '”，再进入主线学习。'
        if focus['selected_material_title']:
            action += ' 建议先回看当前资料。'
        if focus['current_page']:
            action += f' 当前可直接从第{focus["current_page"]}页继续。'
        focus['suggested_action'] = action
        focus['suggested_question'] = '我当前最该先补哪个薄弱点？请结合这门课资料告诉我下一步该看什么、怎么练。'
    elif focus['top_module_name']:
        action = '建议先按“' + focus['top_module_name'] + '”推进。'
        if focus['top_module_focus']:
            action += ' 当前重点是：' + focus['top_module_focus']
        focus['suggested_action'] = action
        focus['suggested_question'] = '请结合当前学习路径，告诉我这门课现在最适合先看哪部分资料。'
    else:
        action = '先从当前资料入手，补齐课程整体框架后再继续推进。'
        if focus['selected_material_title']:
            action = f'先从《{focus["selected_material_title"]}》入手，确认这门课当前资料的核心内容。'
        focus['suggested_action'] = action
        focus['suggested_question'] = '请先结合当前资料，帮我判断这门课最值得优先看的部分。'

    if focus['top_module_name']:
        focus['evidence_points'].append({'label': '当前阶段', 'text': focus['top_module_name']})
    if focus['weak_areas']:
        focus['evidence_points'].append({'label': '优先补弱', 'text': '、'.join(focus['weak_areas'])})
    if focus['selected_material_title']:
        focus['evidence_points'].append({'label': '当前资料', 'text': focus['selected_material_title']})
    if focus['current_page']:
        focus['evidence_points'].append({'label': '当前页码', 'text': f'第{focus["current_page"]}页/张'})
    if focus['recommendation_reason']:
        focus['evidence_points'].append({'label': '推荐依据', 'text': focus['recommendation_reason'][0]})

    return focus


@login_required
def learning_plan_detail_view(request, plan_id):
    plan = get_object_or_404(LearningPlan, id=plan_id, user=request.user)
    plan_data = _safe_json_loads(plan.plan_data, {})
    matched_course_data = plan_data.get('matched_course') if isinstance(plan_data.get('matched_course'), dict) else {}
    matched_course = None
    course_actions = []
    primary_material = None
    course_id = matched_course_data.get('id')
    if course_id:
        matched_course = _course_queryset_for_user(request.user).filter(id=course_id).first()
    if matched_course:
        primary_material = matched_course.materials.order_by('display_order', 'created_at').first()
        study_url = reverse('course_study', args=[matched_course.id])
        ai_chat_url = reverse('course_ai_chat', args=[matched_course.id])
        if primary_material:
            study_url = f'{study_url}?material={primary_material.id}'
            ai_chat_url = f'{ai_chat_url}?material={primary_material.id}'
        course_actions = [
            {'label': '进入资料学习', 'url': study_url},
            {'label': '打开 AI 辅导', 'url': ai_chat_url},
        ]

    normalized_recent_progress = []
    raw_recent_progress = plan_data.get('recent_progress') if isinstance(plan_data.get('recent_progress'), list) else []
    for item in raw_recent_progress:
        if not isinstance(item, dict):
            continue
        outline_id = item.get('outline_id')
        outline_url = ''
        if outline_id:
            try:
                outline_url = reverse('course_outline', args=[int(outline_id)])
            except Exception:
                outline_url = ''
        normalized_recent_progress.append({
            'outline_id': outline_id,
            'outline_title': item.get('outline_title') or item.get('course_title') or '最近学习内容',
            'chapter_id': item.get('chapter_id') or '',
            'status': item.get('status') or '',
            'quiz_score': item.get('quiz_score'),
            'outline_url': outline_url,
        })

    primary_outline_url = normalized_recent_progress[0]['outline_url'] if normalized_recent_progress and normalized_recent_progress[0].get('outline_url') else ''

    enriched_modules = []
    raw_modules = plan_data.get('modules') if isinstance(plan_data.get('modules'), list) else []
    for module in raw_modules:
        if not isinstance(module, dict):
            continue
        lessons = []
        for lesson in module.get('lessons') or []:
            if not isinstance(lesson, dict):
                continue
            resource_labels = [str(item).strip().lower() for item in lesson.get('resources') or [] if str(item).strip()]
            lesson_actions = []
            lesson_anchor = _match_lesson_material_anchor(matched_course, lesson) if matched_course else None
            if matched_course:
                study_url = reverse('course_study', args=[matched_course.id])
                ai_chat_url = reverse('course_ai_chat', args=[matched_course.id])
                if primary_material:
                    study_url = f'{study_url}?material={primary_material.id}'
                    ai_chat_url = f'{ai_chat_url}?material={primary_material.id}'
                if lesson_anchor:
                    study_url = lesson_anchor['study_url']
                    ai_chat_url = lesson_anchor['ai_chat_url']
                if any(item in {'doc', 'ppt', 'code'} for item in resource_labels):
                    lesson_actions.append({'label': '阅读资料', 'url': study_url})
                if 'quiz' in resource_labels:
                    lesson_actions.append({'label': '开始练习', 'url': f'{study_url}#studyQuizPanelShell'})
                if 'animation' in resource_labels and primary_outline_url:
                    lesson_actions.append({'label': '查看课件', 'url': primary_outline_url})
                if any(item in {'doc', 'ppt', 'animation'} for item in resource_labels):
                    lesson_actions.append({'label': '向 AI 追问', 'url': ai_chat_url})
            next_lesson = dict(lesson)
            next_lesson['actions'] = lesson_actions
            next_lesson['anchor_match'] = lesson_anchor
            lessons.append(next_lesson)

        next_module = dict(module)
        next_module['lessons'] = lessons
        enriched_modules.append(next_module)

    context = {
        'plan': plan,
        'plan_data': plan_data,
        'modules': enriched_modules,
        'recommendation_reason': plan_data.get('recommendation_reason') if isinstance(plan_data.get('recommendation_reason'), list) else [],
        'recent_progress': normalized_recent_progress,
        'weak_areas': plan_data.get('weak_areas') if isinstance(plan_data.get('weak_areas'), list) else [],
        'course_actions': course_actions,
    }
    return render(request, 'curriculum/learning_plan_detail.html', context)


@login_required
@require_POST
def refresh_learning_plan_view(request, plan_id):
    plan = get_object_or_404(LearningPlan, id=plan_id, user=request.user)
    refreshed_plan_data = _build_refreshed_learning_plan(plan, request.user)
    new_plan = LearningPlan.objects.create(
        user=request.user,
        title=str(refreshed_plan_data.get('title') or plan.title)[:200],
        plan_data=json.dumps(refreshed_plan_data, ensure_ascii=False),
        status='generated',
    )
    try:
        record_profile_event(
            request.user,
            'learning_plan_refreshed',
            {
                'plan_id': new_plan.id,
                'source_plan_id': plan.id,
                'plan_title': new_plan.title,
                'weak_areas': refreshed_plan_data.get('weak_areas') or [],
                'top_module_name': (refreshed_plan_data.get('modules') or [{}])[0].get('name') if isinstance(refreshed_plan_data.get('modules'), list) and refreshed_plan_data.get('modules') else '',
            },
            source_app='curriculum_app.plan',
            confidence=0.75,
            dedupe_key=f'learning_plan_refreshed:{new_plan.id}',
        )
    except Exception:
        logger.exception('Failed to record profile event for refreshed learning plan %s', new_plan.id)
    messages.success(request, '学习路径已根据当前薄弱点和最近进度重新调整。')
    return redirect('learning_plan_detail', plan_id=new_plan.id)


@login_required
def course_outline_view(request, outline_id):
    outline = get_object_or_404(CourseOutline, id=outline_id, user=request.user)
    exports = outline.exports.order_by('-created_at') if hasattr(outline, 'exports') else []
    context = {'outline': outline, 'exports': exports}
    return render(request, 'curriculum/course_outline.html', context)


@login_required
@require_POST
def delete_course_outline(request, outline_id):
    outline = get_object_or_404(CourseOutline, id=outline_id)
    if not (request.user.is_staff or outline.user == request.user):
        return JsonResponse({'success': False, 'error': 'forbidden'}, status=403)

    try:
        title = _delete_course_outline_with_files(outline)
    except Exception:
        logger.exception('Failed to delete CourseOutline %s', outline_id)
        return JsonResponse({'success': False, 'error': 'delete_failed'}, status=500)

    return JsonResponse({'success': True, 'deleted_title': title})


def _delete_course_outline_with_files(outline):
    title = outline.title
    export_paths = []
    try:
        export_paths = list(outline.exports.exclude(file_path__isnull=True).exclude(file_path='').values_list('file_path', flat=True))
    except Exception:
        export_paths = []

    for rel_path in export_paths:
        try:
            file_path = os.path.join(settings.MEDIA_ROOT, rel_path)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            logger.exception('Failed to remove outline export file %s', rel_path)

    outline.delete()
    return title


@login_required
@require_POST
def bulk_delete_course_outlines(request):
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}') if request.body else {}
    except Exception:
        payload = request.POST

    raw_ids = payload.get('outline_ids') or payload.get('ids') or request.POST.getlist('outline_ids') or []
    if isinstance(raw_ids, str):
        raw_ids = [item.strip() for item in raw_ids.split(',')]

    outline_ids = []
    for raw_id in raw_ids:
        try:
            outline_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if outline_id not in outline_ids:
            outline_ids.append(outline_id)

    if not outline_ids:
        return JsonResponse({'success': False, 'error': '请选择要删除的课程'}, status=400)

    queryset = CourseOutline.objects.filter(id__in=outline_ids)
    if not request.user.is_staff:
        queryset = queryset.filter(user=request.user)

    outlines = list(queryset)
    deleted_ids = []
    deleted_titles = []
    failed_ids = []
    for outline in outlines:
        outline_id = outline.id
        try:
            deleted_titles.append(_delete_course_outline_with_files(outline))
            deleted_ids.append(outline_id)
        except Exception:
            logger.exception('Failed to bulk delete CourseOutline %s', outline_id)
            failed_ids.append(outline_id)

    skipped_ids = [outline_id for outline_id in outline_ids if outline_id not in deleted_ids and outline_id not in failed_ids]
    return JsonResponse({
        'success': not failed_ids,
        'deleted_count': len(deleted_ids),
        'deleted_ids': deleted_ids,
        'deleted_titles': deleted_titles,
        'skipped_ids': skipped_ids,
        'failed_ids': failed_ids,
    }, status=200 if not failed_ids else 500)


@login_required
@require_POST
def confirm_course_blueprint(request, outline_id):
    outline = get_object_or_404(CourseOutline, id=outline_id, user=request.user)

    if outline.status == 'generating':
        return JsonResponse({'success': True, 'outline_id': outline.id, 'status': outline.status, 'message': '课程资源已在生成中'})

    if outline.status == 'completed':
        return JsonResponse({'success': True, 'outline_id': outline.id, 'status': outline.status, 'message': '课程资源已经生成完成'})

    if outline.status == 'failed':
        return JsonResponse({'success': False, 'error': '当前课程状态异常，请重新创建蓝图或重试'}, status=400)

    task_id = _schedule_outline_generation(outline, outline.title, request.user)
    return JsonResponse({'success': True, 'outline_id': outline.id, 'task_id': task_id, 'status': outline.status})


@login_required
def tutor_chat(request):
    return render(request, 'tutor/chat.html', {})


@login_required
def course_ai_chat_view(request, course_id):
    course = get_object_or_404(_course_queryset_for_user(request.user), id=course_id)
    materials = list(course.materials.all())
    requested_material_id = request.GET.get('material')
    selected_material = None
    if requested_material_id:
        for material in materials:
            if str(material.id) == str(requested_material_id):
                selected_material = material
                break
    if selected_material is None and materials:
        selected_material = materials[0]

    current_page = request.GET.get('current_page') or ''
    latest_plan = _get_latest_course_plan_snapshot(request.user, course=course)
    course_ai_focus = _build_course_ai_focus_snapshot(course, latest_plan, selected_material=selected_material, current_page=current_page)
    try:
        record_profile_event(
            request.user,
            'course_ai_opened',
            {
                'course_id': course.id,
                'course_title': course.title,
                'material_id': getattr(selected_material, 'id', None),
                'material_title': getattr(selected_material, 'title', ''),
                'current_page': current_page,
                'suggested_question': course_ai_focus.get('suggested_question') if isinstance(course_ai_focus, dict) else '',
            },
            source_app='curriculum_app.course_ai',
            course_id=course.id,
            material_id=getattr(selected_material, 'id', None),
            confidence=0.45,
        )
    except Exception:
        logger.exception('Failed to record profile event for course AI view %s', course.id)
    context = {
        'course': course,
        'materials': materials,
        'selected_material': selected_material,
        'current_page': current_page,
        'latest_plan': latest_plan,
        'course_ai_focus': course_ai_focus,
    }
    return render(request, 'curriculum/course_ai_chat.html', context)


@login_required
@require_POST
def tutor_generate(request):
    """Classify the user query and trigger roadmap or courseware generation.

    Returns JSON with keys: success, type ('roadmap'|'courseware'|'clarify'), and related ids.
    """
    try:
        if request.content_type and request.content_type.startswith('application/json'):
            data = json.loads(request.body.decode('utf-8') or '{}')
            query = data.get('query') or data.get('text') or ''
        else:
            query = request.POST.get('query') or request.POST.get('text') or ''
    except Exception:
        query = request.POST.get('query') or request.POST.get('text') or ''

    if not query:
        return JsonResponse({'success': False, 'error': 'missing query parameter'}, status=400)

    try:
        from .utils.tutor_classifier import classify_request
    except Exception:
        classify_request = None
    try:
        from .utils.tutor_prompts import build_roadmap_prompt, build_courseware_prompt
    except Exception:
        build_roadmap_prompt = build_courseware_prompt = None

    if classify_request:
        label, conf = classify_request(query)
    else:
        label, conf = ('roadmap', 0.5)

    if label == 'roadmap':
        plan_json = {}
        try:
            seed_plan = _build_personalized_learning_plan(query, request.user)
            prompt = build_roadmap_prompt(query) if build_roadmap_prompt else None
            generated = None
            try:
                from agent_system.llm import call_llm  # optional
                generated = call_llm(prompt)
            except Exception:
                generated = None

            if generated:
                try:
                    plan_json = json.loads(generated)
                except Exception:
                    plan_json = seed_plan
            else:
                plan_json = seed_plan
        except Exception:
            plan_json = _build_personalized_learning_plan(query, request.user)

        if not isinstance(plan_json, dict):
            plan_json = _build_personalized_learning_plan(query, request.user)
        else:
            seed_plan = _build_personalized_learning_plan(query, request.user)
            plan_json.setdefault('title', seed_plan.get('title'))
            plan_json.setdefault('description', seed_plan.get('description'))
            plan_json.setdefault('profile_summary', seed_plan.get('profile_summary'))
            plan_json.setdefault('matched_course', seed_plan.get('matched_course'))
            plan_json.setdefault('recent_progress', seed_plan.get('recent_progress'))
            plan_json.setdefault('weak_areas', seed_plan.get('weak_areas'))
            plan_json.setdefault('recommendation_reason', seed_plan.get('recommendation_reason'))
            if not isinstance(plan_json.get('modules'), list) or not plan_json.get('modules'):
                plan_json['modules'] = seed_plan.get('modules') or []

        try:
            lp = LearningPlan.objects.create(user=request.user, title=query, plan_data=json.dumps(plan_json, ensure_ascii=False), status='generated')
        except Exception:
            logger.exception('Failed to create LearningPlan')
            return JsonResponse({'success': False, 'error': 'unable to save LearningPlan'}, status=500)

        try:
            record_profile_event(
                request.user,
                'learning_plan_generated',
                {
                    'plan_id': lp.id,
                    'plan_title': lp.title,
                    'query': query,
                    'matched_course': plan_json.get('matched_course') if isinstance(plan_json, dict) else None,
                    'weak_areas': plan_json.get('weak_areas') if isinstance(plan_json, dict) else [],
                    'recommendation_reason': plan_json.get('recommendation_reason') if isinstance(plan_json, dict) else [],
                    'profile_delta': {'learning_goals': [query]},
                },
                source_app='curriculum_app.tutor_generate',
                confidence=0.65,
                dedupe_key=f'learning_plan_generated:{lp.id}',
            )
        except Exception:
            logger.exception('Failed to record profile event for generated learning plan %s', lp.id)

        return JsonResponse({'success': True, 'type': 'roadmap', 'plan_id': lp.id, 'plan_data': plan_json})

    elif label == 'courseware':
        blueprint = _build_course_blueprint(query, '', request.user)
        outline = CourseOutline.objects.create(
            user=request.user,
            title=query,
            description='',
            estimated_hours=(blueprint.get('blueprint') or {}).get('estimated_hours') or 0,
            outline_data=json.dumps(blueprint, ensure_ascii=False),
            status='generating',
            progress=5,
        )
        task_id = None
        try:
            from agent_system.models import AgentTask
            task = AgentTask.objects.create(
                user=request.user,
                name=f"Generate course (tutor): {outline.title}",
                input_data={'outline_id': outline.id, 'topic': query},
                status='pending',
                progress=0,
            )
            task_id = task.id
            try:
                from agent_system.tasks import run_agent_task
                job = run_agent_task.delay(task.id)
                try:
                    data = task.output_data or {}
                    if isinstance(data, str):
                        try:
                            data = json.loads(data)
                        except Exception:
                            data = {}
                    data['celery_job_id'] = getattr(job, 'id', str(job))
                    task.output_data = data
                except Exception:
                    logger.exception("Failed to attach job id to AgentTask %s", getattr(task, 'id', None))
                task.status = 'pending'
                task.save(update_fields=['output_data', 'status'])
            except Exception as e:
                logger.exception("Celery call failed in tutor_generate, falling back to local thread: %s", e)
                import threading

                def _local_run(tid, user_obj, topic_str):
                    try:
                        from agent_system.agents import orchestrate_generate_resources
                        orchestrate_generate_resources(user_obj, topic_str, resource_types=None, task=AgentTask.objects.get(pk=tid))
                    except Exception as ex:
                        logger.exception("Local generation failed for task %s: %s", tid, ex)
                        try:
                            from agent_system.models import AgentTask as _AgentTask
                            _AgentTask.objects.filter(pk=tid).update(status='failed', output_data={'error': str(ex)})
                        except Exception:
                            logger.exception("Failed to update AgentTask after local failure %s", tid)

                t = threading.Thread(target=_local_run, args=(task.id, request.user, query), daemon=True)
                t.start()
                try:
                    task.status = 'running'
                    task.save(update_fields=['status'])
                except Exception:
                    try:
                        task.status = 'running'
                        task.save()
                    except Exception:
                        logger.exception('Failed to save task status for task %s', getattr(task, 'id', None))
        except Exception:
            # Fallback: produce a placeholder outline if agent_system unavailable
            blueprint['generation_phase'] = 'blueprint_ready'
            outline.outline_data = json.dumps(blueprint, ensure_ascii=False)
            outline.status = 'completed'
            outline.progress = 100
            outline.save()

        return JsonResponse({'success': True, 'type': 'courseware', 'outline_id': outline.id, 'task_id': task_id})

    else:
        return JsonResponse({'success': True, 'type': 'clarify', 'message': 'Please provide more specific goals (e.g., what to learn, duration, target level).', 'confidence': conf})


@login_required
def export_outline_pptx(request, outline_id):
    """Export a CourseOutline to PPTX. Returns a FileResponse if ready, otherwise schedules an export and returns JSON."""
    outline = get_object_or_404(CourseOutline, id=outline_id, user=request.user)

    # If an exported file already exists, return it directly
    try:
        if getattr(outline, 'exported_pptx', None):
            fp = os.path.join(settings.MEDIA_ROOT, outline.exported_pptx)
            if os.path.exists(fp) and os.path.getsize(fp) > 0:
                try:
                    fh = open(fp, 'rb')
                    return FileResponse(fh, as_attachment=True, filename=os.path.basename(fp))
                except Exception:
                    pass
    except Exception:
        logger.exception('Failed checking exported_pptx')

    # Try to schedule async export (Celery preferred), otherwise start a background thread.
    try:
        from .tasks import export_outline_task, export_outline_task_sync
    except Exception:
        export_outline_task = None
        export_outline_task_sync = None

    try:
        use_celery_export = bool(getattr(settings, 'USE_CELERY_FOR_PPTX_EXPORT', False))
        if use_celery_export and export_outline_task and hasattr(export_outline_task, 'delay'):
            try:
                # mark outline export pending
                try:
                    outline.export_status = 'pending'
                    outline.export_progress = 0
                    outline.save(update_fields=['export_status', 'export_progress'])
                except Exception:
                    pass
                # create export record
                export_entry = None
                try:
                    export_entry = OutlineExport.objects.create(course_outline=outline, user=request.user, status='pending')
                except Exception:
                    logger.exception('Failed to create OutlineExport record')

                # schedule Celery task with export_id
                try:
                    job = export_outline_task.delay(outline.id, export_entry.id if export_entry else None)
                    if export_entry:
                        try:
                            export_entry.task_id = getattr(job, 'id', None) or str(job)
                            export_entry.save(update_fields=['task_id'])
                        except Exception:
                            pass
                    return JsonResponse({'success': True, 'scheduled': True, 'export_id': export_entry.id if export_entry else None, 'note': 'export scheduled'})
                except Exception:
                    logger.exception('Celery scheduling failed, falling back to thread')
            except Exception:
                logger.exception('Inner scheduling logic failed')

        # 未开启 USE_CELERY_FOR_PPTX_EXPORT，或 Celery 调度失败：用后台线程导出
        if export_outline_task_sync:
            import threading
            export_entry = None
            try:
                export_entry = OutlineExport.objects.create(course_outline=outline, user=request.user, status='pending')
            except Exception:
                logger.exception('Failed to create OutlineExport record (thread fallback)')
            try:
                outline.export_status = 'running'
                outline.export_progress = 0
                outline.save(update_fields=['export_status', 'export_progress'])
            except Exception:
                pass
            t = threading.Thread(target=export_outline_task_sync, args=(outline.id, export_entry.id if export_entry else None), daemon=True)
            t.start()
            return JsonResponse({'success': True, 'scheduled': True, 'export_id': export_entry.id if export_entry else None, 'note': 'export started in background (fallback)'})
    except Exception:
        logger.exception('Failed to schedule export task')

    # Final fallback: run synchronous export and return file
    try:
        result = None
        try:
            result = export_outline_task_sync(outline.id)
        except TypeError:
            result = export_outline_task_sync(outline.id, None)

        if not result or not result.get('success'):
            raise Exception('export failed or did not produce a file')

        rel = result.get('path')
        if not rel:
            raise Exception('export task did not return a file path')
        file_path = os.path.join(settings.MEDIA_ROOT, rel)
        filename = os.path.basename(file_path)
    except Exception as e:
        try:
            outline.export_status = 'failed'
            outline.save(update_fields=['export_status'])
        except Exception:
            pass
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

    try:
        fh = open(file_path, 'rb')
        return FileResponse(fh, as_attachment=True, filename=filename,
                            content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation')
    except Exception:
        raise Http404('cannot read export file')


@login_required
def background_image(request, keyword):
    """Return or cache a background image for a keyword. Path: /curriculum/background/<keyword>/"""
    try:
        import re
        safe_kw = re.sub(r'[^0-9A-Za-z _\-]', '', (keyword or '')).strip()
    except Exception:
        safe_kw = 'education'
    if not safe_kw:
        safe_kw = 'education'

    fname = safe_kw.replace(' ', '_').lower() + '.jpg'
    exports_dir = os.path.join(settings.MEDIA_ROOT, 'backgrounds')
    os.makedirs(exports_dir, exist_ok=True)
    file_path = os.path.join(exports_dir, fname)

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        try:
            return FileResponse(open(file_path, 'rb'), content_type='image/jpeg')
        except Exception:
            pass

    try:
        import requests
        url = f'https://source.unsplash.com/1600x900/?{safe_kw}'
        r = requests.get(url, timeout=8)
        if r.status_code == 200 and r.content:
            with open(file_path, 'wb') as fh:
                fh.write(r.content)
            return FileResponse(open(file_path, 'rb'), content_type='image/jpeg')
    except Exception:
        logger.exception('Failed to fetch background for %s', safe_kw)

    return HttpResponseRedirect(f'https://source.unsplash.com/1600x900/?{safe_kw}')


@login_required
@require_POST
def submit_quiz(request, outline_id):
    """Accept quiz submission, grade it, and save results to LearningProgress."""
    outline = get_object_or_404(CourseOutline, id=outline_id, user=request.user)

    try:
        if request.content_type and request.content_type.startswith('application/json'):
            data = json.loads(request.body.decode('utf-8') or '{}')
        else:
            data = request.POST.dict()
    except Exception:
        data = {}

    answers = data.get('answers') if isinstance(data, dict) else None
    if answers is None:
        answers = {k: v for k, v in (data.items() if isinstance(data, dict) else []) if k.startswith('q')}

    try:
        od = json.loads(outline.outline_data) if outline.outline_data else {}
        if not isinstance(od, dict):
            od = {}
    except Exception as e:
        logger.exception('Failed to parse CourseOutline.outline_data for outline %s', getattr(outline, 'id', None))
        od = {}

    quiz_res = (od.get('resources') or {}).get('quiz') or {}
    preview = quiz_res.get('preview') or quiz_res.get('content') or ''
    qobj = None
    if isinstance(preview, str):
        ps = preview.strip()
        if ps.startswith('{') or ps.startswith('['):
            try:
                qobj = json.loads(preview)
            except Exception as e:
                logger.exception('Failed to parse quiz preview JSON for outline %s', getattr(outline, 'id', None))
                qobj = None
        else:
            qobj = preview
    else:
        qobj = preview

    questions = []
    if isinstance(qobj, dict) and isinstance(qobj.get('questions'), list):
        questions = qobj.get('questions')
    elif isinstance(qobj, list):
        questions = qobj
    else:
        return JsonResponse({'success': False, 'error': 'no questions found'}, status=400)

    def _norm(x):
        return _normalize_practice_text(x)

    def _get_correct(q):
        for k in ('answer', 'correct_answer', 'answer_text', 'correct'):
            if k in q:
                return q.get(k)
        if 'choices' in q and isinstance(q.get('choices'), list):
            for opt in q.get('choices'):
                if isinstance(opt, dict) and opt.get('correct'):
                    return opt.get('value') or opt.get('label') or opt.get('text') or opt
        return ''

    total = len(questions)
    correct_count = 0
    details = []
    for i, q in enumerate(questions):
        q_key = f'q{i}'
        submitted = None
        if isinstance(answers, dict):
            submitted = answers.get(q_key) or answers.get(str(i))
        submitted_norm = _norm(submitted)
        correct_raw = _get_correct(q)
        q_type = str(q.get('type') or q.get('question_type') or '').strip().lower()
        # 判断题识别：显式题型，或正确答案本身是明确的判断词（用精确 token 集，不能用恒不为 None 的归一函数）
        _TF_TOKENS = {'true', 't', 'yes', 'y', '对', '正确', '是', 'false', 'f', 'no', 'n', '错', '错误', '否'}
        is_tf = q_type in ('true_false', 'judge', 'tf') or (
            not isinstance(correct_raw, (list, tuple)) and _norm(correct_raw).rstrip('的。！!.') in _TF_TOKENS
        )
        if isinstance(correct_raw, (list, tuple)):
            correct_norms = [_norm(x) for x in correct_raw]
            correct_flag = submitted_norm in correct_norms
            correct_display = correct_raw
        elif is_tf:
            # 判断题：把"正确/对/√/T/是"与"true"等统一，避免文本不一致误判
            correct_flag = (_normalize_true_false_token(submitted) == _normalize_true_false_token(correct_raw))
            correct_display = correct_raw
        else:
            correct_norm = _norm(correct_raw)
            correct_flag = (submitted_norm == correct_norm)
            correct_display = correct_raw

        if correct_flag:
            correct_count += 1

        details.append({
            'question': q.get('question') or q.get('text') or '',
            'submitted': submitted,
            'correct': correct_display,
            'correct_flag': bool(correct_flag),
        })

    score = round((correct_count / total) * 100, 2) if total > 0 else 0.0

    chapter_id = f"quiz_{quiz_res.get('id') or 'quiz'}"
    try:
        lp, created = LearningProgress.objects.update_or_create(
            user=request.user,
            course_outline=outline,
            chapter_id=chapter_id,
            defaults={
                'status': 'completed',
                'completed_slides': total,
                'total_slides': total,
                'quiz_score': score,
                'completed_at': timezone.now(),
            }
        )
    except Exception:
        logger.exception('Failed to create/update LearningProgress for outline %s user %s', getattr(outline, 'id', None), getattr(request.user, 'id', None))
        lp = None

    try:
        record_profile_event(
            request.user,
            'outline_quiz_submitted',
            {
                'outline_id': outline.id,
                'chapter_id': chapter_id,
                'progress_id': getattr(lp, 'id', None),
                'score': score,
                'total': total,
                'correct_count': correct_count,
                'knowledge_tags': [str((q or {}).get('knowledge_tag') or (q or {}).get('source_heading') or '').strip() for q in questions if isinstance(q, dict)],
            },
            source_app='curriculum_app.outline_quiz',
            confidence=0.8,
        )
    except Exception:
        logger.exception('Failed to record profile event for outline quiz %s', outline.id)

    return JsonResponse({'success': True, 'score': score, 'total': total, 'correct': correct_count, 'details': details})


@login_required
def generate_course_stream(request):
    """SSE endpoint to stream AgentTask progress and partial outputs.

    Params: task_id
    Events: 'resources', 'progress', 'output', 'completed'
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'only GET supported'}, status=405)

    task_id = request.GET.get('task_id')
    if not task_id:
        return JsonResponse({'error': 'missing task_id'}, status=400)

    try:
        from agent_system.models import AgentTask
    except Exception:
        return JsonResponse({'error': 'agent system not available'}, status=500)

    task = get_object_or_404(AgentTask, pk=task_id)
    if not (request.user.is_staff or task.user == request.user):
        return JsonResponse({'error': 'forbidden'}, status=403)

    def event_stream():
        last_output = None
        last_progress = None
        start = time.time()
        timeout = int(request.GET.get('timeout', '300'))
        while True:
            try:
                task.refresh_from_db()
            except Exception:
                yield f"event: error\ndata: {json.dumps({'error': 'cannot read task'})}\n\n"
                break

            output_raw = task.output_data or {}
            output = _safe_json_loads(output_raw, {}) if not isinstance(output_raw, dict) else output_raw
            progress = getattr(task, 'progress', 0)

            if output != last_output:
                try:
                    prev_resources = (last_output or {}).get('resources') if isinstance(last_output, dict) else None
                    curr_resources = output.get('resources') if isinstance(output, dict) else None
                    new_resources = {}
                    if curr_resources:
                        if not prev_resources:
                            new_resources = curr_resources
                        else:
                            for k, v in curr_resources.items():
                                if not prev_resources.get(k) or prev_resources.get(k) != v:
                                    new_resources[k] = v

                    if new_resources:
                        yield f"event: resources\ndata: {json.dumps(new_resources, ensure_ascii=False)}\n\n"
                    else:
                        yield f"event: output\ndata: {json.dumps(output, ensure_ascii=False)}\n\n"
                except Exception:
                    yield f"event: output\ndata: {json.dumps(output, ensure_ascii=False)}\n\n"
                last_output = output

            if progress != last_progress:
                yield f"event: progress\ndata: {json.dumps({'progress': progress})}\n\n"
                last_progress = progress

            if task.status in ('done', 'failed', 'completed'):
                # 兜底：output 里若混入不可序列化内容，也要发出终止事件，否则前端收不到 completed/error 会一直挂到超时
                try:
                    yield f"event: completed\ndata: {json.dumps({'status': task.status, 'output': output}, ensure_ascii=False)}\n\n"
                except Exception:
                    yield f"event: completed\ndata: {json.dumps({'status': task.status})}\n\n"
                break

            if time.time() - start > timeout:
                yield f"event: timeout\ndata: {json.dumps({'message': 'subscription timeout'})}\n\n"
                break

            time.sleep(1)

    return StreamingHttpResponse(event_stream(), content_type='text/event-stream')


@login_required
def generate_outline_stream(request):
    """SSE: stream CourseOutline outline_data updates by outline_id."""
    if request.method != 'GET':
        return JsonResponse({'error': 'only GET supported'}, status=405)

    outline_id = request.GET.get('outline_id')
    if not outline_id:
        return JsonResponse({'error': 'missing outline_id'}, status=400)

    try:
        outline = CourseOutline.objects.get(pk=outline_id)
    except CourseOutline.DoesNotExist:
        return JsonResponse({'error': 'outline not found'}, status=404)

    if not (request.user.is_staff or outline.user == request.user):
        return JsonResponse({'error': 'forbidden'}, status=403)

    def event_stream():
        last_output = None
        last_progress = None
        start = time.time()
        timeout = int(request.GET.get('timeout', '300'))
        while True:
            try:
                outline.refresh_from_db()
            except Exception:
                yield f"event: error\ndata: {json.dumps({'error': 'cannot read outline'})}\n\n"
                break

            # outline_data 落库是 JSON 字符串；先解析成对象再发，避免二次编码（前端拿到字符串还要再 parse）
            output = _safe_json_loads(outline.outline_data, {})

            progress = getattr(outline, 'progress', 0)

            if output != last_output:
                try:
                    yield f"event: output\ndata: {json.dumps({'outline_data': output}, ensure_ascii=False)}\n\n"
                except Exception:
                    yield f"event: output\ndata: {json.dumps({'outline_data': str(output)})}\n\n"
                last_output = output

            if progress != last_progress:
                yield f"event: progress\ndata: {json.dumps({'progress': progress})}\n\n"
                last_progress = progress

            if outline.status in ('completed', 'failed'):
                try:
                    yield f"event: completed\ndata: {json.dumps({'status': outline.status, 'outline_data': output}, ensure_ascii=False)}\n\n"
                except Exception:
                    yield f"event: completed\ndata: {json.dumps({'status': outline.status})}\n\n"
                break

            if time.time() - start > timeout:
                yield f"event: timeout\ndata: {json.dumps({'message': 'subscription timeout'})}\n\n"
                break

            time.sleep(1)

    return StreamingHttpResponse(event_stream(), content_type='text/event-stream')


@login_required
def export_outline_stream(request, outline_id):
    """SSE: stream export status and progress for a CourseOutline."""
    if request.method != 'GET':
        return JsonResponse({'error': 'only GET supported'}, status=405)

    try:
        outline = CourseOutline.objects.get(pk=outline_id)
    except CourseOutline.DoesNotExist:
        return JsonResponse({'error': 'outline not found'}, status=404)

    if not (request.user.is_staff or outline.user == request.user):
        return JsonResponse({'error': 'forbidden'}, status=403)

    def event_stream():
        last_status = None
        last_progress = None
        start = time.time()
        timeout = int(request.GET.get('timeout', '600'))
        while True:
            try:
                outline.refresh_from_db()
            except Exception:
                yield f"event: error\ndata: {json.dumps({'error': 'cannot read outline'})}\n\n"
                break

            status = getattr(outline, 'export_status', None)
            progress = getattr(outline, 'export_progress', 0)
            if status != last_status:
                yield f"event: export_status\ndata: {json.dumps({'status': status, 'progress': progress, 'path': outline.exported_pptx}, ensure_ascii=False)}\n\n"
                last_status = status

            if progress != last_progress:
                yield f"event: export_progress\ndata: {json.dumps({'progress': progress})}\n\n"
                last_progress = progress

            if status in ('completed', 'failed'):
                yield f"event: export_completed\ndata: {json.dumps({'status': status, 'path': outline.exported_pptx}, ensure_ascii=False)}\n\n"
                break

            if time.time() - start > timeout:
                yield f"event: timeout\ndata: {json.dumps({'message': 'subscription timeout'})}\n\n"
                break

            time.sleep(1)

    return StreamingHttpResponse(event_stream(), content_type='text/event-stream')


@login_required
def download_export(request, export_id):
    """Download a previously created OutlineExport file."""
    try:
        exp = OutlineExport.objects.get(pk=export_id)
    except OutlineExport.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'not_found'}, status=404)

    if not (request.user.is_staff or exp.user == request.user):
        return JsonResponse({'success': False, 'error': 'forbidden'}, status=403)

    if not exp.file_path:
        return JsonResponse({'success': False, 'error': 'no_file'}, status=404)

    fp = os.path.join(settings.MEDIA_ROOT, exp.file_path)
    if not os.path.exists(fp):
        return JsonResponse({'success': False, 'error': 'file_not_found'}, status=404)

    try:
        fh = open(fp, 'rb')
        return FileResponse(fh, as_attachment=True, filename=exp.filename or os.path.basename(fp))
    except Exception:
        return JsonResponse({'success': False, 'error': 'read_failed'}, status=500)


@login_required
@require_POST
def delete_export(request, export_id):
    """Delete an OutlineExport record and attempt to remove its file (owner or staff only)."""
    try:
        exp = OutlineExport.objects.get(pk=export_id)
    except OutlineExport.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'not_found'}, status=404)

    if not (request.user.is_staff or exp.user == request.user):
        return JsonResponse({'success': False, 'error': 'forbidden'}, status=403)

    try:
        if exp.file_path:
            fp = os.path.join(settings.MEDIA_ROOT, exp.file_path)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception:
                    logger.exception('Failed to remove export file %s', fp)
    except Exception:
        logger.exception('Failed during file removal check for export %s', export_id)

    try:
        exp.delete()
    except Exception:
        logger.exception('Failed to delete export record %s', export_id)
        return JsonResponse({'success': False, 'error': 'delete_failed'}, status=500)

    return JsonResponse({'success': True})
