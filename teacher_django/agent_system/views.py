import io
import json
import uuid
import zipfile
import re
from types import SimpleNamespace
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.utils import timezone
import logging

from django.db.models import Q

from .models import StudentProfile, LearningResource, AgentTask, Conversation, Message
from .agents import orchestrate_generate_resources, build_analogy_seed
from django.conf import settings
from .services.profile_builder import ProfileBuilder
from .services.profile_events import record_profile_event
from .knowledge_tracing import get_tracer, record_interaction as record_kt_interaction, get_mastery_summary as get_kt_summary

logger = logging.getLogger(__name__)

profile_builder = ProfileBuilder()
from .services.embeddings import compute_embedding, cosine_similarity
from django.http import StreamingHttpResponse
from django.views.decorators.http import require_http_methods

try:
    from curriculum_app.models import Course, CourseMaterial, LearningPlan, MaterialChunk
except Exception:
    Course = None
    CourseMaterial = None
    LearningPlan = None
    MaterialChunk = None


def _build_course_knowledge_map(course):
    empty = {
        'material_count': 0,
        'chunk_count': 0,
        'topics': [],
        'headings': [],
        'summary': '',
    }
    if not course or not CourseMaterial or not MaterialChunk:
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
        summary_parts.append(f"本课程共 {len(materials)} 份资料")
    if chunks:
        summary_parts.append(f"已解析 {len(chunks)} 个知识片段")
    if topics:
        summary_parts.append("重点主题：" + '、'.join(topics[:6]))
    elif headings:
        summary_parts.append("主要结构：" + '、'.join(headings[:5]))

    return {
        'material_count': len(materials),
        'chunk_count': len(chunks),
        'topics': topics[:8],
        'headings': headings[:6],
        'summary': '；'.join(summary_parts),
    }


def _lexical_overlap_score(query_text: str, candidate_text: str) -> float:
    # \u590d\u7528 embeddings \u7684\u5206\u8bcd\uff08\u4e2d\u6587\u6309\u5b57+\u76f8\u90bb\u53cc\u5b57\u3001\u82f1\u6587\u6309\u8bcd+\u53cc\u8bcd\uff09\uff0c\u5426\u5219\u8fde\u7eed\u4e2d\u6587\u4e32\u4f1a\u88ab\u5207\u6210
    # \u4e00\u6574\u4e2a token\uff0c\u5bfc\u81f4\u4e2d\u6587\u573a\u666f lexical \u51e0\u4e4e\u6052\u4e3a 0\uff08\u53ea\u6709\u6574\u6bb5\u5b8c\u5168\u4e00\u81f4\u624d\u7b97\u91cd\u5408\uff09\u3002
    from agent_system.services.embeddings import _tokens
    query_tokens = set(_tokens((query_text or '').lower()))
    candidate_tokens = set(_tokens((candidate_text or '').lower()))
    if not query_tokens or not candidate_tokens:
        return 0.0
    overlap = query_tokens & candidate_tokens
    return len(overlap) / max(len(query_tokens), 1)


def _resolve_course_context(user, course_id=None, material_id=None):
    context = {
        'course': None,
        'material': None,
        'access_denied': False,
    }
    if not Course or not CourseMaterial:
        return context

    course = None
    if course_id:
        try:
            course = Course.objects.get(pk=int(course_id))
            if not (course.owner_id == user.id or course.status == 'published'):
                course = None
        except Exception:
            course = None

    material = None
    if material_id:
        try:
            material = CourseMaterial.objects.select_related('course').get(pk=int(material_id))
            if course and material.course_id != course.id:
                material = None
            elif not course:
                course = material.course
        except Exception:
            material = None

    course_accessible = False
    if course:
        course_accessible = Course.objects.filter(
            Q(id=course.id)
            & (
                Q(owner=user)
                | (
                    Q(status='published')
                    & (Q(visibility='public') | Q(visibility='login'))
                )
            )
        ).exists()
    if not course_accessible and material and not course:
        course = material.course
        course_accessible = Course.objects.filter(
            Q(id=course.id)
            & (
                Q(owner=user)
                | (
                    Q(status='published')
                    & (Q(visibility='public') | Q(visibility='login'))
                )
            )
        ).exists()

    if not course_accessible:
        context['access_denied'] = True
        return context

    context['course'] = course
    context['material'] = material
    return context


def _retrieve_material_chunks(user, question_text: str, course_id=None, material_id=None, current_page=None, limit: int = 4):
    if not MaterialChunk or not CourseMaterial:
        return [], _resolve_course_context(user, course_id=course_id, material_id=material_id)

    resolved = _resolve_course_context(user, course_id=course_id, material_id=material_id)
    if resolved.get('access_denied'):
        return [], resolved
    course = resolved.get('course')
    material = resolved.get('material')

    queryset = MaterialChunk.objects.select_related('material', 'material__course')
    if course:
        queryset = queryset.filter(material__course=course)
    elif material:
        queryset = queryset.filter(material=material)
    else:
        queryset = queryset.filter(material__course__status='published').filter(Q(material__course__visibility='public') | Q(material__course__visibility='login') | Q(material__course__owner=user))

    query_embedding = compute_embedding(question_text or '')
    scored = []
    for chunk in queryset[:120]:
        # 现在的 compute_embedding 是词袋哈希向量（能反映内容重合），旧库里可能存着早期
        # sha256 噪声向量，故对分块统一按内容实时重算，保证语义信号真实、对存量数据也生效。
        chunk_text = ' '.join([chunk.heading or '', chunk.keyword_summary or '', chunk.content or ''])
        semantic_score = cosine_similarity(query_embedding, compute_embedding(chunk_text))
        lexical_score = _lexical_overlap_score(question_text or '', chunk_text)
        material_bonus = 0.12 if material and chunk.material_id == material.id else 0.0
        score = (semantic_score * 0.55) + (lexical_score * 0.45) + material_bonus
        scored.append((score, chunk))

    scored.sort(key=lambda item: item[0], reverse=True)
    results = []
    for score, chunk in scored[:limit]:
        if score <= 0:
            continue
        results.append({
            'chunk_id': chunk.id,
            'score': round(score, 4),
            'heading': chunk.heading,
            'source_page': chunk.source_page,
            'content': (chunk.content or '')[:1000],
            'material_id': chunk.material_id,
            'material_title': chunk.material.title,
            'course_id': chunk.material.course_id,
            'course_title': chunk.material.course.title,
        })
    return results, resolved


def _build_material_context_block(question_text: str, user, course_id=None, material_id=None, current_page=None):
    chunks, resolved = _retrieve_material_chunks(user, question_text, course_id=course_id, material_id=material_id, current_page=current_page)
    course = resolved.get('course')
    material = resolved.get('material')
    course_map = _build_course_knowledge_map(course)
    current_page_text = str(current_page).strip() if current_page is not None else ''

    lines = []
    if course:
        lines.append(f"当前课程：{course.title}")
        lines.append('说明：本次答疑默认基于该课程下全部已解析资料，不局限于当前打开的单份资料。')
        if course_map.get('summary'):
            lines.append(f"课程知识地图：{course_map['summary']}")
    if material:
        lines.append(f"当前关注资料：{material.title}")

    if chunks:
        lines.append('以下是与学生问题最相关的课程资料片段，请优先基于这些内容回答，并尽量说明依据页码或页序：')
        for index, chunk in enumerate(chunks, start=1):
            location = f"第{chunk['source_page']}页/张" if chunk.get('source_page') else '页码未知'
            lines.append(f"[{index}] {chunk['material_title']} - {chunk.get('heading') or '片段'}（{location}）")
            lines.append(chunk['content'])
    elif course or material:
        lines.append('当前课程资料中暂未检索到高相关片段。回答时请明确说明“未在本课程资料中找到直接依据”，再给出一般性解释。')

    return {
        'text': '\n'.join(lines).strip(),
        'course': course,
        'material': material,
        'current_page': current_page_text,
        'course_map': course_map,
        'chunks': chunks,
    }


def _load_latest_learning_plan_context(user, course=None):
    if not LearningPlan or not user or not getattr(user, 'is_authenticated', False):
        return None

    target_course_id = getattr(course, 'id', None)
    for item in LearningPlan.objects.filter(user=user, status='generated').order_by('-updated_at')[:12]:
        payload = item.plan_data
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            continue
        matched_course = payload.get('matched_course') if isinstance(payload.get('matched_course'), dict) else {}
        if target_course_id and str(matched_course.get('id')) != str(target_course_id):
            continue

        modules = payload.get('modules') if isinstance(payload.get('modules'), list) else []
        top_module = modules[0] if modules and isinstance(modules[0], dict) else {}
        top_lessons = top_module.get('lessons') if isinstance(top_module.get('lessons'), list) else []
        recommendation_reason = [str(entry).strip() for entry in payload.get('recommendation_reason') or [] if str(entry).strip()]
        weak_areas = [str(entry).strip() for entry in payload.get('weak_areas') or [] if str(entry).strip()]

        return {
            'plan_id': item.id,
            'title': payload.get('title') or item.title,
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


def _format_learning_plan_context(plan_context):
    if not isinstance(plan_context, dict):
        return ''

    lines = [f"最近同步的学习路径：{plan_context.get('title') or '未命名路径'}"]
    weak_areas = [str(item).strip() for item in plan_context.get('weak_areas') or [] if str(item).strip()]
    if weak_areas:
        lines.append('当前优先补弱：' + '、'.join(weak_areas[:3]))
    if plan_context.get('top_module_name'):
        lines.append('当前优先阶段：' + str(plan_context.get('top_module_name')))
    if plan_context.get('top_module_focus'):
        lines.append('当前阶段关注：' + str(plan_context.get('top_module_focus')))
    top_lessons = plan_context.get('top_lessons') if isinstance(plan_context.get('top_lessons'), list) else []
    for index, lesson in enumerate(top_lessons[:2], start=1):
        if not isinstance(lesson, dict):
            continue
        lesson_title = str(lesson.get('title') or '').strip()
        lesson_objectives = str(lesson.get('objectives') or '').strip()
        if lesson_title:
            lines.append(f'优先任务{index}：{lesson_title}')
        if lesson_objectives:
            lines.append(f'任务说明{index}：{lesson_objectives}')
    for reason in plan_context.get('recommendation_reason') or []:
        reason_text = str(reason).strip()
        if reason_text:
            lines.append('推荐原因：' + reason_text)
    return '\n'.join(lines)


def _detect_response_mode(question_text: str):
    text = str(question_text or '').strip()
    if not text:
        return 'guided'

    direct_explanation_markers = [
        '为什么错',
        '为什么会错',
        '总是出错',
        '解释这题',
        '讲解这题',
        '讲解',
        '帮我拆解',
        '错题',
        '下列哪项不是',
        '哪项不是',
        '为什么不对',
        '帮我分析',
        '知识点',
    ]
    if any(marker in text for marker in direct_explanation_markers):
        return 'direct_explanation'

    planning_markers = [
        '学习计划',
        '怎么学',
        '路线',
        '规划',
        '建议我先学',
        '我应该先学',
        '帮我安排',
    ]
    if any(marker in text for marker in planning_markers):
        return 'guided_planning'

    return 'guided'


# 关键词兜底表（仅在 LLM 不可用/失败时使用；LLM 抽取才是主路径）
_CONCEPT_KEYWORDS = {
    '二次方程': 0.6, '判别式': 0.5, '求根公式': 0.7, '因式分解': 0.5,
    '函数': 0.4, '导数': 0.7, '积分': 0.8, '极限': 0.7, '矩阵': 0.6,
    '向量': 0.5, '概率': 0.6, '统计': 0.5,
}

_HINT_MARKERS = ['提示', 'hint', '想想', '思考', '试着']


def _extract_concepts_via_llm(client, student_question: str, reply_text: str) -> dict:
    """
    用 LLM 从「学生问 + AI 讲」这轮真实对话里抽取当前正在学的知识点（学科无关）。
    这样面板里的知识点反映真实学习内容，而不是固定词表占位。失败/不可用返回 None。
    """
    if client is None or not reply_text:
        return None
    prompt = (
        "你是知识点标注器。阅读下面这轮学习对话，提取学生此刻正在学习的核心知识点"
        "（具体的学科概念或技能名，2~8个字，最多4个），并估计每个的掌握难度"
        "（0到1的小数，越难越大）。只输出一个 JSON 数组，不要任何解释或多余文字，"
        "例如：[{\"name\":\"指针\",\"difficulty\":0.6},{\"name\":\"递归\",\"difficulty\":0.7}]。\n\n"
        f"【学生】{(student_question or '')[:500]}\n【老师】{reply_text[:1200]}"
    )
    try:
        out = client.generate_text(prompt, max_tokens=160) or ''
    except Exception:
        return None
    m = re.search(r'\[.*\]', out, re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(arr, list):
        return None
    concepts = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        if not name or len(name) > 20:
            continue
        try:
            diff = float(item.get('difficulty', 0.5))
        except (TypeError, ValueError):
            diff = 0.5
        concepts[name] = {'difficulty': min(1.0, max(0.0, diff)), 'hint_used': False}
        if len(concepts) >= 4:
            break
    return concepts or None


def _extract_knowledge_concepts_from_reply(reply_text: str, material_context: dict,
                                           client=None, student_question: str = '') -> dict:
    """
    从对话中提取知识点，喂给真实的 AKT 知识追踪引擎。
    优先用 LLM 真实抽取（学科无关、反映真在学什么）；LLM 不可用时回退关键词表。
    """
    if not reply_text:
        return {}

    # 主路径：LLM 真实抽取
    concepts = _extract_concepts_via_llm(client, student_question, reply_text)
    if not concepts:
        # 兜底：关键词匹配（LLM 关闭/失败时仍有基本可用度）
        concepts = {}
        reply_lower = reply_text.lower()
        for concept, diff in _CONCEPT_KEYWORDS.items():
            if concept in reply_lower:
                concepts[concept] = {'difficulty': diff, 'hint_used': False}
        if material_context and material_context.get('text'):
            material_text = material_context['text'].lower()
            for concept, diff in _CONCEPT_KEYWORDS.items():
                if concept in material_text and concept not in concepts:
                    concepts[concept] = {'difficulty': diff, 'hint_used': False}

    # 回复里有引导性措辞 → 标记 hint_used（BKT 会据此降低"猜对"权重）
    if any(kw in reply_text for kw in _HINT_MARKERS):
        for concept in concepts:
            concepts[concept]['hint_used'] = True
    return concepts


def _infer_student_understanding(student_question: str, ai_reply: str, concept_info: dict) -> bool:
    """
    推断学生是否理解了知识点
    
    基于问题类型和AI回复内容推断
    """
    question_lower = student_question.lower()
    reply_lower = ai_reply.lower()
    
    # 学生提问包含困惑信号
    confusion_signals = ['不懂', '不明白', '困惑', '不清楚', '不会', '为什么错', '怎么错']
    is_confused = any(kw in question_lower for kw in confusion_signals)
    
    # 学生要求直接答案（可能没有理解）
    direct_answer_request = any(kw in question_lower for kw in ['答案', '直接给', '告诉我', '怎么做'])
    
    # AI回复包含验证性问题
    has_verification = any(kw in reply_lower for kw in ['你理解了吗', '明白了吗', '懂了吗', '你觉得', '你认为'])
    
    # 推断理解程度
    if is_confused:
        return False  # 学生明确表示不懂
    elif direct_answer_request and not has_verification:
        return False  # 学生要答案但没有验证理解
    elif has_verification:
        return True  # AI验证了理解
    else:
        # 默认推断：如果AI回复包含解释，假设学生理解了
        return len(ai_reply) > 100


# 自我解释提示（Self-Explanation Effect, Chi et al.）：固定文案，
# 便于下一轮通过精确匹配判断学生是否在回应这条提示。
SELF_EXPLANATION_PROMPT = "\n\n📝 现在请你用自己的话简单解释一下刚才讲的内容，确认你真正理解了——这一步很重要哦。"


# 多模态答疑指令：让 AI 在答疑时不只给文字，还能给公式(LaTeX)与图解(Mermaid)，
# 前端会把 $...$/$$...$$ 渲染成公式、把 ```mermaid 代码块渲染成流程图/概念图。
_MULTIMODAL_ANSWER_GUIDE = (
    "【多模态表达】在有帮助时，除了文字讲解还要用："
    "①公式：涉及数学/推导就用 LaTeX，行内 $...$、独立公式 $$...$$；"
    "②图解：讲流程/步骤/结构/关系/对比时，尽量配一段 Mermaid 图（用 ```mermaid 代码块包裹，"
    "如 flowchart TD、graph LR、mindmap），节点文字用中文、控制在约 8 个节点内、力求一眼看懂；"
    "③用 Markdown 组织：重点**加粗**、分点、必要时用 `行内代码`/代码块。不要为了用而用，"
    "确实能让学生更好理解时才加图或公式。"
)


def _build_conversation_system_prompt(question_text: str, guidance_summary: str, known_summary: str, material_context=None, tutor_mode='learning', memories=None, analogy_seed='', recent_topics=''):
    """构建对话系统提示词"""
    material_context = material_context or {}
    has_course_context = bool(material_context.get('text'))

    # 对话模式：普通AI聊天机器人
    if tutor_mode == 'chat':
        prompt = (
            "你是一位友好、乐于助人的AI助手。"
            "你的任务是回答用户的问题，提供有用的信息和帮助。"
            "回答要自然、简洁，不要涉及学习阶段、课程、学习路径等内容。"
            "如果用户的问题不明确，可以适当追问以获取更多信息。"
            "重要：回答时不要使用'Student:'、'Assistant:'、'用户:'、'助手:'等角色前缀，直接输出内容即可。"
        )
        return prompt

    # 助教答疑模式：数字人主讲讲课时，学生随时点开的"课程助教"，直接、清晰地就当前这页答疑
    if tutor_mode == 'ta':
        prompt = (
            "你是这门课程的助教。学生正在看老师讲的课件，遇到不懂的地方来问你。"
            "你的任务是直接、清晰、有耐心地把学生的疑问讲明白。"
            "【回答原则】"
            "1. 直接回答问题，先给结论，再解释为什么——不要苏格拉底式反问，不要绕弯子、不要卖关子。"
            "2. 紧扣学生当前正在看的这一页内容（题目里会带上本页标题和要点），就事论事地讲透。"
            "3. 如果提供了课程资料片段，优先结合这些内容作答。"
            "4. 讲解要有条理：必要时用'先…再…最后…'或分点，把一个概念拆开讲清楚。"
            "5. 如果学生的理解有偏差，明确指出错在哪、正确的是什么，并说清楚区别。"
            "6. 结尾可以给一句'下一步该看/该练什么'的小建议，帮学生衔接回课件。"
            "7. 语气亲切自然、口语化，像一个耐心的助教在旁边讲，不要长篇大论、不要空话套话。"
            + _MULTIMODAL_ANSWER_GUIDE +
            "重要：回答时不要使用'Student:'、'Assistant:'、'用户:'、'助手:'、'助教:'等角色前缀，直接输出内容即可。"
        )
        return prompt

    # 费曼互教模式：AI扮演学习进度稍慢的同伴"小艾"，向学生请教并追问
    if tutor_mode == 'peer_teaching':
        topic_hint = ''
        material_obj = material_context.get('material')
        course_obj = material_context.get('course')
        if material_obj is not None:
            topic_hint = getattr(material_obj, 'title', '') or ''
        if not topic_hint and course_obj is not None:
            topic_hint = getattr(course_obj, 'title', '') or ''

        prompt = (
            "你扮演一个名叫'小艾'的同学，学习进度比对方（你的同学）稍慢，"
            "对当前学习的内容还不太熟悉。"
        )
        if topic_hint:
            prompt += f"你们正在学习的内容是《{topic_hint}》，"
        if recent_topics:
            prompt += (
                f"对方告诉过你，自己最近学了这些内容：{recent_topics}。"
                "请从中挑一个具体的知识点，"
            )
        prompt += "你主动请对方给你讲讲这部分内容。"

        if memories:
            memory_lines = "；".join(
                str(m.get('content', '')) for m in memories if isinstance(m, dict) and m.get('content')
            )
            if memory_lines:
                prompt += (
                    f"【你的记忆】你还记得之前和这位同学交流时的一些印象：{memory_lines}。"
                    "如果合适，可以在对话中自然地提起这些印象（例如确认对方是否还会犯类似的错误），"
                    "但不要每句话都提，也不要让对话显得像是在做总结。"
                )

        prompt += (
            "【对话规则】"
            "1. 每次只问一个具体的问题，不要一次问多个。"
            "2. 如果对方刚讲解完，先简单回应你听懂了哪些部分，再针对讲解中可能被忽略的"
            "边界条件、前提或容易混淆的地方提出一个具体追问；不要只说'还有吗'这种泛泛的话。"
            "3. 如果发现对方的讲解中有错误或遗漏，不要直接说'你错了'，而是通过追问引导对方"
            "自己发现问题（例如举一个反例或追问一个边界情况）。"
            "4. 语气是同学之间请教交流，亲切、口语化，不要用老师的语气。"
            "【什么时候结束这次互教】"
            "5. 当对方已经把这个知识点讲清楚了——核心概念说明白、也正确回应了你的追问、你确实听懂了——"
            "就不要再为了继续而硬找问题。此时给一个简短真诚的收尾：谢谢对方、说你现在懂了、"
            "用一句话复述你最大的收获，然后在**最后单独一行**输出结束标记 [本次互教结束]。"
            "6. 但不要过早结束：至少要对方做过两三轮有实质内容的讲解、你也追问过之后才可以结束；"
            "如果对方只说了一两句、或还有明显没讲清/讲错的地方，就继续追问，不要输出结束标记。"
            "重要：回答时不要使用'Student:'、'Assistant:'、'小艾:'等角色前缀，直接输出内容即可。"
        )
        return prompt

    # 学习模式：课程导师（苏格拉底式 + 防护栏）
    mode = _detect_response_mode(question_text)
    
    prompt = (
        "你是一位大学课程的智能导师，采用苏格拉底式教学法引导学生学习。"
        "你的核心原则是：不直接给答案，通过启发式提问引导学生自己思考、探索和发现真理。"
        ""
        "【苏格拉底式对话规则】"
        "1. 当学生提问时，先确认学生已经理解了什么，而不是直接给出答案。"
        "2. 提出一个引导性问题帮助学生思考下一步，例如：'你觉得下一步该如何计算？'或'这个答案合理吗？为什么？'"
        "3. 等待学生回答后再继续引导，不要一次性给出所有步骤。"
        "4. 如果学生回答错误，给出提示而非正确答案，引导学生自己发现错误。"
        "5. 通过多轮对话逐步引导学生掌握问题解决能力，而不是被动接受答案。"
        ""
        "【学习防护栏】"
        "1. 如果学生直接要求答案，先拒绝并引导思考：'我建议你先尝试自己思考，我可以给你一些提示。'"
        "2. 只有在学生尝试过3次后，才给出部分提示，而不是完整答案。"
        "3. 每次提示只给一个关键点，不要一次性给出所有解题步骤。"
        "4. 在给出提示后，必须追问：'你能根据这个提示继续思考吗？'"
        "5. 验证学生理解后再继续下一个知识点，不要跳过学生的理解过程。"
        ""
        "【回答原则】"
        "1. 先判断学生当前处于哪个阶段（初次接触、尝试中、困惑、接近掌握）。"
        "2. 优先给出下一步该做什么，而不是长篇大论的解释。"
        "3. 如果信息不足，只追问一个最关键的问题。"
        "4. 回答尽量分步、可执行、像老师带着学生往前走。"
        "5. 除了解释知识点，还要给出练习、任务或下一轮互动建议。"
        "6. 语气自然、具体，不要只说'你可以问我'。"
        "7. 如果学生刚刚已经回答了目标、偏好或基础，不要重复询问同一信息。"
        "8. 当已知信息足够时，直接进入路径规划、讲解或练习，不要停留在收集信息阶段。"
        "9. 如果提供了课程资料片段，优先结合这些内容作答；如果上下文不完整，直接给出当前最有帮助的解释和下一步建议。"
        "10. 如果已经同步了最近调整版学习路径，要明确说明当前建议与哪些薄弱点、阶段目标或推荐理由有关。"
        + _MULTIMODAL_ANSWER_GUIDE +
        "重要：回答时不要使用'Student:'、'Assistant:'、'用户:'、'助手:'等角色前缀，直接输出内容即可。"
    )

    if mode == 'direct_explanation':
        prompt += (
            "当前请求类型：错题讲解或知识点拆解。"
            "此类请求必须先直接回答，不要先询问学生在学什么课程、基础如何、目标是什么。"
            "即使课程上下文不完整，也要先基于题干和常识给出一个可执行的讲解，不要把回答停在背景说明上。"
            "回答必须严格使用以下 5 段小标题，且按顺序输出："
            "1. 结论；"
            "2. 为什么会错；"
            "3. 正确区分点；"
            "4. 回看哪里；"
            "5. 立刻自测。"
            "每一段控制在 1 到 3 句话内，优先短句，不要写成长篇大论。"
            "“立刻自测”必须给出 1 个紧贴当前错题的小追问，帮助学生马上检验是否听懂。"
            "如果题干已经给出，就把它视为当前要讲解的对象，不要要求学生再次提供题目。"
        )
    elif mode == 'guided_planning':
        prompt += (
            "当前请求类型：学习规划。"
            "如果信息不完整，可以追问一个最关键的问题，但拿到信息后要立刻给出阶段化建议。"
        )
    else:
        prompt += "当前请求类型：通用课程问答。优先结合当前问题直接回答，再决定是否需要补充追问。"

    if has_course_context:
        prompt += "当前已有课程资料上下文，默认问题属于这门课，不要再反问学生是不是这门课程。"
        prompt += (
            "当你在这门课程上下文中回答时，默认按三段输出："
            "1. 先给结论；"
            "2. 再给资料依据或推理过程；"
            "3. 最后给下一步行动建议。"
            "每段都尽量具体，避免只给泛泛建议。"
        )

    if analogy_seed:
        prompt += (
            f'【类比教学提示】学生已经较好掌握了"{analogy_seed}"相关内容。'
            f'如果当前要讲解的新概念和这些内容在结构或原理上有相似之处，'
            f'优先尝试用学生已掌握的"{analogy_seed}"来做类比，帮助学生建立联系；'
            f'如果没有合适的类比，就不必勉强。'
        )

    prompt += f"当前引导策略：{guidance_summary}"
    prompt += f"已知画像摘要：{known_summary}"
    return prompt


def _restore_latex_json_escapes(text: str):
    if text is None:
        return ''

    value = str(text)
    escape_map = {
        '\x08': '\\b',
        '\x0c': '\\f',
        '\n': '\\n',
        '\r': '\\r',
        '\t': '\\t',
    }
    for escaped_char, latex_prefix in escape_map.items():
        value = value.replace(escaped_char.encode('utf-8').decode('unicode_escape'), latex_prefix)
    return value


def _parse_generated_reply_options(raw_text: str):
    if not raw_text:
        return []

    text = str(raw_text).strip()
    candidates = []

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            candidates = parsed
        elif isinstance(parsed, dict):
            candidates = parsed.get('options') or parsed.get('replies') or []
    except Exception:
        match = re.search(r'\[[\s\S]*\]', text)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, list):
                    candidates = parsed
            except Exception:
                candidates = []

    if not candidates:
        for line in text.splitlines():
            line = re.sub(r'^(?:[-*•]|\d+[\.、\)]\s*)', '', line).strip()
            if line:
                candidates.append(line)

    cleaned = []
    for item in candidates:
        option = _restore_latex_json_escapes(item).strip().strip('"').strip("'").strip()
        option = option.strip('。；; ')
        if not option:
            continue
        if len(option) < 6 or len(option) > 48:
            continue
        if '？' in option or '?' in option:
            continue
        if re.match(r'^(当然|下面|现在|接下来|首先|例如|比如|想象一下|我们|如果我们|假设我们)', option):
            continue
        if option not in cleaned:
            cleaned.append(option)
    return cleaned[:3]


def _generate_reply_options_with_ai(user_text: str, reply_text: str):
    client = getattr(profile_builder, 'client', None)
    if not client or not getattr(client, 'api_key', None):
        return []

    prompt = (
        '你要为教育对话生成 3 条“用户下一轮可以直接点击发送的回答”。\n'
        '要求：\n'
        '1. 必须站在用户口吻，用第一人称。\n'
        '2. 必须是用户对导师刚才回复的自然回应，不要重复导师原话。\n'
        '3. 每条 12 到 36 个汉字，简短、具体、可直接发送。\n'
        '4. 不要输出问题句，不要输出解释，不要编号。\n'
        '5. 只输出 JSON 数组，例如：["...","...","..."]。\n'
        '6. 如果内容里包含 LaTeX 公式，JSON 字符串中的反斜杠必须写成双反斜杠，例如 \\frac 和 \\binom。\n\n'
        f'用户上一轮输入：{user_text or ""}\n'
        f'导师本轮回复：{reply_text or ""}\n'
    )

    try:
        raw = client.generate_text(prompt, max_tokens=180)
    except Exception:
        return []
    return _parse_generated_reply_options(raw)


def _extract_reply_question(reply_text: str):
    if not reply_text:
        return None

    normalized = re.sub(r'\s+', ' ', str(reply_text)).strip()
    if not normalized:
        return None

    sentences = re.split(r'(?<=[？?。!！])\s*', normalized)
    question_candidates = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if ('？' in sentence or '?' in sentence) and 6 <= len(sentence) <= 90:
            question_candidates.append(sentence)

    if question_candidates:
        return question_candidates[-1]

    follow_up_patterns = [
        r'(请你[^。！？]{4,60})',
        r'(你可以先[^。！？]{4,60})',
        r'(接下来[^。！？]{4,60})',
    ]
    for pattern in follow_up_patterns:
        match = re.search(pattern, normalized)
        if match:
            text = match.group(1).strip('：:，,；; ')
            if text:
                return text + '？'

    return None


def _extract_reply_options(reply_text: str):
    if not reply_text:
        return []

    options = []
    normalized_lines = [line.strip() for line in str(reply_text).replace('\r\n', '\n').split('\n') if line.strip()]
    bullet_pattern = re.compile(r'^(?:[-*•]|(?:\d+|[一二三四五六七八九十])[\.、\)])\s*(.+)$')

    for line in normalized_lines:
        match = bullet_pattern.match(line)
        if not match:
            continue
        option = match.group(1).strip().strip('。；;')
        if len(option) < 6 or len(option) > 48:
            continue
        if '?' in option or '？' in option:
            continue
        if re.match(r'^(当然|下面|现在|接下来|首先|例如|比如|想象一下|我们|如果我们|假设我们)', option):
            continue
        if not re.search(r'^(我|请|先|你先|可以先|希望|想)', option):
            continue
        if option not in options:
            options.append(option)

    if options:
        return options[:3]

    sentence_candidates = re.split(r'[。！？\n]', str(reply_text))
    for sentence in sentence_candidates:
        option = sentence.strip(' ：:；;，,')
        if not option:
            continue
        if '?' in option or '？' in option:
            continue
        if len(option) < 8 or len(option) > 40:
            continue
        if re.match(r'^(你|现在，让我们|接下来让我们|下面我们)', option):
            continue
        if not re.search(r'我|请|先|想|希望|可以|帮我|告诉我', option):
            continue
        if option not in options:
            options.append(option)

    return options[:3]


def _extract_topic_from_question(question_text: str):
    if not question_text:
        return ''

    normalized = str(question_text).strip().strip('？?。')
    patterns = [
        r'对(.+?)的理解',
        r'什么是(.+)',
        r'关于(.+?)你',
        r'(.+?)是什么',
        r'(.+?)的核心',
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            topic = match.group(1).strip('“”"：:，,；; ')
            if 1 <= len(topic) <= 20:
                return topic
    return ''


def _extract_topic_from_text(text: str):
    if not text:
        return ''

    normalized = re.sub(r'\s+', ' ', str(text)).strip().strip('？?。！!')
    if not normalized:
        return ''

    patterns = [
        r'什么是(.+?)(?:[,，。！？]|$)',
        r'理解(.+?)(?:[,，。！？]|$)',
        r'关于(.+?)(?:[,，。！？]|$)',
        r'学习(.+?)(?:[,，。！？]|$)',
        r'(.+?)代表了',
        r'(.+?)的定义',
        r'(.+?)的概念',
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        topic = match.group(1).strip('“”"：:，,；; ')
        if 1 <= len(topic) <= 20:
            return topic

    keywords = ['导数', '极限', '积分', '函数', '概率', '线性代数', '矩阵', 'Python', '机器学习', '深度学习', '神经网络', '算法', '数据结构']
    for keyword in keywords:
        if keyword.lower() in normalized.lower():
            return keyword

    return ''


def _options_from_reply_question(question_text: str):
    if not question_text:
        return []

    topic = _extract_topic_from_question(question_text)
    normalized = str(question_text)

    if '目标' in normalized or '希望达成' in normalized or '想达到' in normalized:
        return [
            '我的目标是先打基础，再逐步提升。',
            '我的目标是尽快解决当前学习任务。',
            '我的目标是应对考试，所以更看重解题能力。',
            '我还不太确定目标，你可以先帮我判断方向。',
        ]

    if '方式' in normalized or '怎么学' in normalized or '更希望我' in normalized:
        return [
            '我更希望你一步一步讲，别一下子讲太快。',
            '我更喜欢先看例子，再理解概念。',
            '我更喜欢边讲边练，你可以多给我小题。',
            '你先用最直观的方法带我入门。',
        ]

    if '基础' in normalized or '掌握' in normalized or '水平' in normalized:
        return [
            '我现在几乎是零基础，需要从头开始。',
            '我懂一点基础，但很多地方不扎实。',
            '我学过一些内容，但不会实际应用。',
            '你可以先测一下我现在的水平。',
        ]

    if '理解' in normalized or '什么是' in normalized or topic:
        topic_label = topic or '这个概念'
        return [
            f'我目前只知道{topic_label}和基础定义有关，但还不太理解。',
            f'我几乎没学过{topic_label}，你可以从最基础开始讲。',
            f'我会一点{topic_label}的公式或定义，但不会实际应用。',
            f'你先用一个直观例子帮我理解{topic_label}。',
        ]

    return [
        '我现在还不太确定，你先根据我的情况继续引导。',
        '我懂一点，但希望你从最关键的部分开始。',
        '你先用一个简单例子带我进入这部分内容。',
        '我想一步一步来，你先判断我现在卡在哪里。',
    ]


def _options_from_reply_context(user_text: str, reply_text: str):
    topic = _extract_topic_from_text(user_text) or _extract_topic_from_text(reply_text)
    reply_lower = str(reply_text or '').lower()

    if topic:
        if topic in ['导数', '极限', '积分', '函数', '概率', '线性代数', '矩阵']:
            return [
                f'我对{topic}还很模糊，你先用更直观的方式讲一下。',
                f'我知道一点{topic}的定义，但不会拿它来做题。',
                f'你先给我一个关于{topic}的简单例子，我再跟着理解。',
            ]
        if topic in ['Python', '机器学习', '深度学习', '神经网络', '算法', '数据结构']:
            return [
                f'我对{topic}还是初学阶段，你先带我理解最核心的概念。',
                f'我学过一点{topic}，但还不会自己动手应用。',
                f'你先给我一个最简单的{topic}例子，我边看边学。',
            ]
        return [
            f'我对{topic}还不太理解，你先从最基础的部分开始讲。',
            f'我知道一点{topic}，但还不会真正应用。',
            f'你先给我一个和{topic}相关的简单例子，我更容易跟上。',
        ]

    if '例子' in reply_text or '举例' in reply_text:
        return [
            '这个例子我能跟上，你再带我往下推一步。',
            '你先把这个例子拆慢一点，我想先看清每一步。',
            '你再换一个更贴近实际的例子，我会更容易理解。',
        ]

    if '练习' in reply_text or '题' in reply_text:
        return [
            '你先给我一道最基础的小题试试。',
            '先别给答案，你先给我一点提示。',
            '我做完题后，你再帮我分析哪里容易错。',
        ]

    if '计划' in reply_lower or '步骤' in reply_lower or '阶段' in reply_lower:
        return [
            '你先把第一步要做什么告诉我。',
            '你先按每天可执行的任务帮我拆开。',
            '你先说我现在最该补的那一块。',
        ]

    return []


def _infer_guided_actions(user_text: str, reply_text: str):
    text = f"{user_text} {reply_text}".lower()
    actions = []

    if 'python' in text:
        actions.extend([
            '先帮我评估一下我现在的 Python 基础',
            '给我设计一个 2 周 Python 入门计划',
            '先用一个简单例子带我理解语法和循环',
        ])
    if '机器学习' in text or 'machine learning' in text or 'ai' in text or '人工智能' in text:
        actions.extend([
            '请先帮我判断我学机器学习前还缺哪些基础',
            '按入门到项目实战给我拆成阶段路线',
            '先从一个最核心概念开始教我，并给一个小练习',
        ])
    if '项目' in text or '实战' in text or '代码' in text:
        actions.extend([
            '给我一个适合当前水平的小项目',
            '把这个项目拆成每天可以完成的小任务',
            '先讲思路，再给我示例代码和练习',
        ])
    if '考试' in text or '复习' in text or '刷题' in text:
        actions.extend([
            '按考试重点帮我列复习顺序',
            '先出 5 道题测一下我当前水平',
            '针对薄弱点给我一个强化训练方案',
        ])

    if not actions:
        actions = [
            '先帮我评估当前基础，再决定从哪里学起',
            '请根据我的目标给我一个分阶段学习路径',
            '先问我 3 个关键问题，再开始制定计划',
        ]

    deduped = []
    for item in actions:
        if item not in deduped:
            deduped.append(item)
    return deduped[:3]


def _infer_response_options(profile, stage, user_text: str, reply_text: str):
    missing_goals = not bool(getattr(profile, 'learning_goals', None)) if profile else True
    missing_style = not bool(getattr(profile, 'cognitive_style', None)) if profile else True
    missing_knowledge = not bool((getattr(profile, 'knowledge_profile', None) or {})) if profile else True

    if missing_goals:
        return [
            '我的目标是通过考试，并建立系统知识框架。',
            '我的目标是做一个完整项目，边做边学。',
            '我的目标是先打基础，后面再进入实战。',
            '我现在还不确定目标，你帮我判断一条最适合的路线。',
        ]

    if missing_style:
        return [
            '我更喜欢图示和视频式讲解。',
            '我更喜欢代码示例和实际操作。',
            '我更喜欢先讲概念，再做题巩固。',
            '我希望每次都先给我一个小例子再展开。',
        ]

    if missing_knowledge:
        return [
            '我是零基础，需要从最基础的概念开始。',
            '我有一点基础，但很多知识点不扎实。',
            '我学过理论，但实战和应用还比较弱。',
            '你先出几道题测一下我目前的水平。',
        ]

    stage_key = (stage or {}).get('key')
    text = f"{user_text} {reply_text}".lower()

    if stage_key == 'planning':
        return [
            '请先给我一个 2 周的学习计划。',
            '请按每天可执行任务帮我拆开。',
            '请先告诉我必须先补的前置知识。',
            '请给我一条偏考试的路线和一条偏项目的路线。',
        ]

    if stage_key == 'teaching':
        return [
            '先讲最核心的概念，再给我一个例子。',
            '先不要直接给答案，先给我一点提示。',
            '请出一道小题让我试试是否理解了。',
            '请把这部分讲得更简单、更适合初学者。',
        ]

    if stage_key == 'feedback':
        return [
            '请帮我总结我目前最薄弱的 3 个点。',
            '请根据我刚才的表现调整后续学习难度。',
            '请给我下一轮练习，并解释错因。',
            '请把后面的学习重点重新排一下顺序。',
        ]

    if 'python' in text:
        return [
            '我想先学 Python 基础语法。',
            '请直接用一个小项目带我入门 Python。',
            '先测试我对 Python 的掌握程度。',
            '请告诉我学 Python 最容易卡住的地方。',
        ]

    if '机器学习' in text or 'machine learning' in text or 'ai' in text or '人工智能' in text:
        return [
            '请先告诉我学机器学习前必须补哪些基础。',
            '请给我一条从入门到项目的学习路线。',
            '请先讲一个最核心概念，再给我例子。',
            '请先测试我是否适合直接学机器学习。',
        ]

    return [
        '请根据我现在的情况，直接告诉我下一步做什么。',
        '请把任务拆小一点，我想一步一步完成。',
        '请先给我一个小练习，再继续往下讲。',
        '请你主动带着我学，不要只等我提问。',
    ]


def _infer_current_question(profile, stage, user_text: str, reply_text: str):
    reply_question = _extract_reply_question(reply_text)
    if reply_question:
        return {
            'key': 'reply_follow_up',
            'label': '跟进回答',
            'question': reply_question,
            'hint': '这条提示是根据导师刚才这一轮的回复自动提炼的。',
        }

    missing_goals = not bool(getattr(profile, 'learning_goals', None)) if profile else True
    missing_style = not bool(getattr(profile, 'cognitive_style', None)) if profile else True
    missing_knowledge = not bool((getattr(profile, 'knowledge_profile', None) or {})) if profile else True

    if missing_goals:
        return {
            'key': 'goal',
            'label': '学习目标',
            'question': '你这阶段最希望达成的学习结果是什么？',
            'hint': '先明确方向，我才能决定是带你走考试路线、项目路线，还是打基础路线。',
        }
    if missing_style:
        return {
            'key': 'style',
            'label': '学习方式',
            'question': '你更希望我用什么方式带你学这一部分？',
            'hint': '你可以直接选图示讲解、代码实践、先讲概念再做题，或者边学边练。',
        }
    if missing_knowledge:
        return {
            'key': 'knowledge',
            'label': '当前基础',
            'question': '你目前的基础更接近哪一种情况？',
            'hint': '只要判断你是零基础、略懂一点，还是学过但不扎实，我就能调整难度。',
        }

    stage_key = (stage or {}).get('key')
    if stage_key == 'planning':
        return {
            'key': 'planning',
            'label': '学习路径',
            'question': '接下来你更希望我先帮你规划哪一部分？',
            'hint': '我可以先做周计划、每日拆解、前置知识补齐，或者考试/项目双路线设计。',
        }
    if stage_key == 'teaching':
        return {
            'key': 'teaching',
            'label': '讲解方式',
            'question': '这一轮你希望我怎么继续讲？',
            'hint': '我可以直接讲概念、先给提示、先出小题，或者把内容再讲简单一点。',
        }
    if stage_key == 'feedback':
        return {
            'key': 'feedback',
            'label': '反馈调整',
            'question': '接下来你更想先做哪种复盘或强化？',
            'hint': '我可以先总结薄弱点、重新调难度、继续练习，或者重排学习重点。',
        }

    return {
        'key': 'next_step',
        'label': '下一步',
        'question': '现在你更希望我先带你做哪一步？',
        'hint': '你可以继续诊断、直接规划、先学一个概念，或者先做小练习。',
    }


def _options_for_question(question_key, profile, stage, user_text: str, reply_text: str):
    ai_generated_options = _generate_reply_options_with_ai(user_text, reply_text)
    if ai_generated_options:
        return ai_generated_options[:3]

    reply_question = _extract_reply_question(reply_text)
    if reply_question:
        generated_options = _options_from_reply_question(reply_question)
        if generated_options:
            return generated_options[:3]

    contextual_options = _options_from_reply_context(user_text, reply_text)
    if contextual_options:
        return contextual_options[:3]

    reply_options = _extract_reply_options(reply_text)
    if reply_options:
        return reply_options[:3]

    if question_key == 'goal':
        return [
            '我的目标是通过考试，并建立系统知识框架。',
            '我的目标是做一个完整项目，边做边学。',
            '我的目标是先打基础，后面再进入实战。',
            '我现在还不确定目标，你帮我判断一条最适合的路线。',
        ][:3]
    if question_key == 'style':
        return [
            '我更喜欢图示和视频式讲解。',
            '我更喜欢代码示例和实际操作。',
            '我更喜欢先讲概念，再做题巩固。',
            '我希望每次都先给我一个小例子再展开。',
        ][:3]
    if question_key == 'knowledge':
        return [
            '我是零基础，需要从最基础的概念开始。',
            '我有一点基础，但很多知识点不扎实。',
            '我学过理论，但实战和应用还比较弱。',
            '你先出几道题测一下我目前的水平。',
        ][:3]
    if question_key == 'planning':
        return [
            '请先给我一个 2 周的学习计划。',
            '请按每天可执行任务帮我拆开。',
            '请先告诉我必须先补的前置知识。',
            '请给我一条偏考试的路线和一条偏项目的路线。',
        ][:3]
    if question_key == 'teaching':
        return [
            '先讲最核心的概念，再给我一个例子。',
            '先不要直接给答案，先给我一点提示。',
            '请出一道小题让我试试是否理解了。',
            '请把这部分讲得更简单、更适合初学者。',
        ][:3]
    if question_key == 'feedback':
        return [
            '请帮我总结我目前最薄弱的 3 个点。',
            '请根据我刚才的表现调整后续学习难度。',
            '请给我下一轮练习，并解释错因。',
            '请把后面的学习重点重新排一下顺序。',
        ][:3]
    return _infer_response_options(profile, stage, user_text, reply_text)[:3]


def _should_offer_response_options(current_question, reply_text: str = ''):
    if current_question and current_question.get('question'):
        return False
    if _extract_reply_question(reply_text):
        return False
    return True


def _build_guidance_summary(profile):
    missing = []
    if not getattr(profile, 'learning_goals', None):
        missing.append('学习目标')
    if not getattr(profile, 'cognitive_style', None):
        missing.append('偏好的学习方式')
    if not (getattr(profile, 'knowledge_profile', None) or {}):
        missing.append('当前知识基础')

    if missing:
        return '我会先带你补齐这些关键信息：' + '、'.join(missing) + '。'
    return '我会根据你的画像，主动为你安排下一步学习任务和练习。'


def _shorten_memory_text(value, max_chars=120):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + '…'


def _build_long_term_memory(convo, older_messages=None, learning_plan_context=None):
    fragments = []

    existing = _shorten_memory_text(getattr(convo, 'context_summary', '') or '', max_chars=260)
    if existing:
        fragments.append('既有记忆：' + existing)

    if isinstance(learning_plan_context, dict):
        module_name = _shorten_memory_text(learning_plan_context.get('top_module_name') or '', max_chars=80)
        weak_areas = [str(item).strip() for item in learning_plan_context.get('weak_areas') or [] if str(item).strip()]
        if module_name:
            fragments.append('当前学习阶段：' + module_name)
        if weak_areas:
            fragments.append('当前优先补弱：' + '、'.join(weak_areas[:3]))

    if isinstance(older_messages, list) and older_messages:
        recent_old = older_messages[-4:]
        old_lines = []
        for message in recent_old:
            role = '学生' if getattr(message, 'role', '') == 'student' else '导师'
            old_lines.append(f"{role}：{_shorten_memory_text(getattr(message, 'content', ''), max_chars=80)}")
        if old_lines:
            fragments.append('更早对话：' + ' | '.join(old_lines))

    return '\n'.join(fragment for fragment in fragments if fragment).strip()


def _update_conversation_memory(convo, student_text, assistant_text, learning_plan_context=None):
    segments = []
    existing = str(getattr(convo, 'context_summary', '') or '').strip()
    if existing:
        segments.extend([item.strip() for item in re.split(r'\s*\|\s*', existing) if item.strip()])

    if isinstance(learning_plan_context, dict):
        module_name = _shorten_memory_text(learning_plan_context.get('top_module_name') or '', max_chars=80)
        if module_name:
            segments.append('当前阶段：' + module_name)

    student_piece = _shorten_memory_text(student_text, max_chars=80)
    assistant_piece = _shorten_memory_text(assistant_text, max_chars=120)
    if student_piece:
        segments.append('学生近期诉求：' + student_piece)
    if assistant_piece:
        segments.append('导师最近建议：' + assistant_piece)

    condensed = []
    for segment in segments:
        if segment and segment not in condensed:
            condensed.append(segment)

    convo.context_summary = ' | '.join(condensed[-8:])[:1200]


def _conversation_mode(convo):
    """从最近几条助手消息的 metadata.persona 推断该会话所属的对话模式
    （chat/learning/peer_teaching/ta）——每条助手消息都记了它出自哪个模式，
    据此让历史记录点击后能跳回对应模式。推断不出时按 'chat' 兜底。"""
    try:
        for m in convo.messages.filter(role='assistant').order_by('-created_at')[:3]:
            if isinstance(m.metadata, dict):
                p = m.metadata.get('persona')
                if p in ('chat', 'learning', 'peer_teaching', 'ta'):
                    return p
    except Exception:
        pass
    return 'chat'


def _serialize_conversation_summary(convo):
    return {
        'id': convo.id,
        'title': convo.title or f'对话 {convo.id}',
        'updated_at': convo.updated_at.strftime('%m-%d %H:%M'),
        'context_summary': (convo.context_summary or '')[:220],
        'mode': _conversation_mode(convo),
    }


def _extract_profile_from_chat_text(text: str):
    text = (text or '').strip()
    if not text:
        return {}

    lowered = text.lower()
    parsed = {}

    goals = []
    goal_keywords = ['考试', '考研', '刷题', '项目', '竞赛', '找工作', '面试', '入门', '系统学习', '复习']
    for keyword in goal_keywords:
        if keyword in text and keyword not in goals:
            goals.append(keyword)
    match = re.search(r'我(想|希望|准备|计划)([^。！？\n]{2,30})', text)
    if match:
        inferred_goal = match.group(2).strip('，。；;、 ')
        if inferred_goal and inferred_goal not in goals:
            goals.append(inferred_goal)
    if goals:
        parsed['learning_goals'] = goals

    style_map = {
        '视频': '视觉型',
        '图': '视觉型',
        '动画': '视觉型',
        '听': '听觉型',
        '讲解': '听觉型',
        '代码': '动手型',
        '实践': '动手型',
        '项目': '动手型',
    }
    for keyword, style in style_map.items():
        if keyword in text:
            parsed['cognitive_style'] = style
            parsed.setdefault('preferences', {})['preferred_mode'] = keyword
            break

    knowledge = {}
    if '零基础' in text or '完全不会' in text or '没学过' in text:
        knowledge['overall'] = '初级'
    elif '入门' in text or '刚开始' in text or '基础一般' in text:
        knowledge['overall'] = '初级'
    elif '有基础' in text or '学过' in text or '了解一些' in text:
        knowledge['overall'] = '中级'
    elif '熟悉' in text or '做过项目' in text or '比较熟练' in text:
        knowledge['overall'] = '高级'

    topic_patterns = ['Python', '机器学习', '深度学习', '线性代数', '概率论', '数据结构', '算法']
    for topic in topic_patterns:
        if topic.lower() in lowered:
            knowledge[topic] = knowledge.get('overall', '初级')
    if knowledge:
        parsed['knowledge_profile'] = knowledge

    hours_match = re.search(r'每周(?:能|可以)?(?:学|投入)?(\d{1,2})\s*小时', text)
    if hours_match:
        parsed.setdefault('engagement', {})['weekly_hours'] = int(hours_match.group(1))

    return parsed


def _known_profile_summary(profile):
    parts = []
    if getattr(profile, 'learning_goals', None):
        parts.append('学习目标：' + '、'.join([str(item) for item in profile.learning_goals[:3]]))
    if getattr(profile, 'cognitive_style', None):
        parts.append('学习方式：' + str(profile.cognitive_style))
    knowledge = getattr(profile, 'knowledge_profile', None)
    if isinstance(knowledge, dict) and knowledge:
        preview = []
        for key, value in list(knowledge.items())[:3]:
            preview.append(f'{key}={value}')
        parts.append('当前基础：' + '，'.join(preview))
    return '；'.join(parts) if parts else '暂无稳定画像信息'


def _build_conversation_profile(conversation, latest_text: str = ''):
    aggregated = {
        'knowledge_profile': {},
        'preferences': {},
        'learning_preferences': {},
        'engagement': {},
    }

    try:
        stored_profile = getattr(conversation.user, 'student_profile', None)
    except Exception:
        stored_profile = None

    if stored_profile:
        # 这些 JSONField 边界情况下可能不是 dict（如知识点被写成 list/标量）；
        # 不做 isinstance 校验直接 dict.update() 会抛 "dictionary update sequence element..." 而崩掉整条对话。
        _sp_kp = stored_profile.knowledge_profile
        if isinstance(_sp_kp, dict):
            aggregated['knowledge_profile'].update(_sp_kp)
        _sp_eg = stored_profile.engagement
        if isinstance(_sp_eg, dict):
            aggregated['engagement'].update(_sp_eg)
        _sp_lp = stored_profile.learning_preferences
        if isinstance(_sp_lp, dict):
            aggregated['learning_preferences'].update(_sp_lp)
            aggregated['preferences'].update(_sp_lp)

        if getattr(stored_profile, 'learning_goals', None):
            aggregated['learning_goals'] = list(stored_profile.learning_goals or [])
        if getattr(stored_profile, 'cognitive_style', None):
            aggregated['cognitive_style'] = stored_profile.cognitive_style

    texts = []
    try:
        for message in conversation.messages.filter(role='student').order_by('created_at'):
            if message.content:
                texts.append(message.content)
    except Exception:
        texts = []

    if latest_text and (not texts or texts[-1] != latest_text):
        texts.append(latest_text)

    for text in texts:
        parsed = _extract_profile_from_chat_text(text)
        if not parsed:
            continue
        for key, value in parsed.items():
            if isinstance(value, dict):
                if key == 'preferences':
                    _lp = aggregated.setdefault('learning_preferences', {})
                    if isinstance(_lp, dict):
                        _lp.update(value)
                _tgt = aggregated.setdefault(key, {})
                # aggregated[key] 可能早先被设成 list/标量（如 learning_goals），此时不能 .update()
                if isinstance(_tgt, dict):
                    _tgt.update(value)
                else:
                    aggregated[key] = dict(value)
            elif isinstance(value, list):
                existing = aggregated.setdefault(key, [])
                for item in value:
                    if item not in existing:
                        existing.append(item)
            elif value:
                aggregated[key] = value

    return SimpleNamespace(
        knowledge_profile=aggregated.get('knowledge_profile') or {},
        cognitive_style=aggregated.get('cognitive_style'),
        learning_goals=aggregated.get('learning_goals') or [],
        learning_preferences=aggregated.get('learning_preferences') or aggregated.get('preferences') or {},
        preferences=aggregated.get('preferences') or aggregated.get('learning_preferences') or {},
        engagement=aggregated.get('engagement') or {},
    )


def _infer_learning_stage(profile, message_count: int):
    if message_count <= 2:
        return {'index': 1, 'key': 'diagnosis', 'label': '目标诊断'}

    has_goals = bool(getattr(profile, 'learning_goals', None))
    has_style = bool(getattr(profile, 'cognitive_style', None))
    has_knowledge = bool((getattr(profile, 'knowledge_profile', None) or {}))

    if not (has_goals and has_style and has_knowledge):
        return {'index': 2, 'key': 'assessment', 'label': '基础评估'}
    if message_count <= 6:
        return {'index': 3, 'key': 'planning', 'label': '路径规划'}
    if message_count <= 10:
        return {'index': 4, 'key': 'teaching', 'label': '讲解练习'}
    return {'index': 5, 'key': 'feedback', 'label': '反馈调整'}


@login_required
@require_GET
def overview(request):
    counts = {
        'profiles': StudentProfile.objects.count(),
        'resources': LearningResource.objects.count(),
        'tasks': AgentTask.objects.count(),
    }
    return render(request, 'agent_system/overview.html', {'counts': counts})


@login_required
def generator_page(request):
    return render(request, 'agent_system/generator.html')


@require_POST
def api_build_profile(request):
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        data = request.POST.dict()
    text = data.get('text', '')
    user_id = data.get('user_id')
    User = get_user_model()
    # 仅允许登录用户创建/更新自己的画像，管理员可为他人操作
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)
    if user_id:
        user = get_object_or_404(User, pk=user_id)
        if not request.user.is_staff and user != request.user:
            return JsonResponse({'error': '无权为他人创建画像'}, status=403)
    else:
        user = request.user

    profile, created = StudentProfile.objects.get_or_create(user=user)
    # 使用 ProfileBuilder 从文本中解析结构化画像
    parsed = profile_builder.build_from_text(text)
    # 合并到画像
    profile.update_from_dict(parsed)
    return JsonResponse({'ok': True, 'profile_id': profile.id, 'created': created, 'profile': {
        'knowledge_profile': profile.knowledge_profile,
        'cognitive_style': profile.cognitive_style,
        'learning_goals': profile.learning_goals,
        'misconceptions': profile.misconceptions,
        'engagement': profile.engagement,
        'learning_preferences': profile.learning_preferences,
    }})


@require_GET
def api_get_profile(request):
    # 获取当前用户或管理员可查询指定用户的画像
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)
    user_id = request.GET.get('user_id')
    User = get_user_model()
    if user_id:
        if not request.user.is_staff:
            return JsonResponse({'error': '无权查看他人画像'}, status=403)
        user = get_object_or_404(User, pk=user_id)
    else:
        user = request.user
    profile, created = StudentProfile.objects.get_or_create(user=user)
    return JsonResponse({'ok': True, 'profile': {
        'user': user.username,
        'knowledge_profile': profile.knowledge_profile,
        'cognitive_style': profile.cognitive_style,
        'learning_goals': profile.learning_goals,
        'misconceptions': profile.misconceptions,
        'engagement': profile.engagement,
        'learning_preferences': profile.learning_preferences,
    }})


@require_POST
def api_create_task(request):
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        data = request.POST.dict()
    user_id = data.get('user_id')
    User = get_user_model()
    # 强制要求登录：仅允许为自己创建任务，管理员可为任意用户创建
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)
    if user_id:
        user = get_object_or_404(User, pk=user_id)
        if not request.user.is_staff and user != request.user:
            return JsonResponse({'error': '无权为他人创建任务'}, status=403)
    else:
        user = request.user
    task = AgentTask.objects.create(user=user, name=data.get('name','agent task'), input_data=data)
    return JsonResponse({'ok': True, 'task_id': task.id})


@require_POST
def api_generate_multi_resources(request):
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        data = request.POST.dict()
    topic = data.get('topic', '未指定主题')
    types = data.get('resource_types') or data.get('types') or ['doc', 'ppt', 'animation', 'quiz']
    user_id = data.get('user_id')
    User = get_user_model()
    # 仅允许登录用户（管理员可为他人创建）
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)
    if user_id:
        user = get_object_or_404(User, pk=user_id)
        if not request.user.is_staff and user != request.user:
            return JsonResponse({'error': '无权为他人创建任务'}, status=403)
    else:
        user = request.user

    # 创建任务并返回任务 id，默认后台 Worker 处理；可通过传入 sync=true 来同步执行并返回结果
    task = AgentTask.objects.create(user=user, name=f'generate:{topic}', input_data=data, status='pending')
    sync = str(data.get('sync', '')).lower() in ('1', 'true', 'yes')
    if sync:
        # 如果未配置 XINGHUO，则直接生成占位资源并返回，避免同步调用抛错影响前端体验
        if not getattr(settings, 'XINGHUO_API_URL', '') or not getattr(settings, 'XINGHUO_API_KEY', ''):
            from .models import LearningResource
            results = {}
            for r in types:
                title = f'{topic} - 占位 {r}'
                content = f'未配置 XINGHUO，已生成占位{r}内容。请在 .env 中配置 XINGHUO_API_URL 与 XINGHUO_API_KEY 以启用真实生成。'
                lr = LearningResource.objects.create(title=title, resource_type=r, content=content, author=user, metadata={'source': 'placeholder'})
                results[r] = {'id': lr.id, 'title': lr.title, 'metadata': lr.metadata}
            # 更新任务状态
            task.status = 'done'
            task.progress = 100
            task.output_data = {'resources': {k: {'id': v['id'], 'title': v['title']} for k, v in results.items()}, 'note': 'XINGHUO 未配置，已生成占位资源'}
            task.save()
            return JsonResponse({'ok': True, 'task_id': task.id, 'result': results, 'note': 'XINGHUO 未配置，已生成占位资源'})
        try:
            result = orchestrate_generate_resources(user, topic, resource_types=types, task=task)
            return JsonResponse({'ok': True, 'task_id': task.id, 'result': result})
        except Exception as e:
            task.status = 'failed'
            task.output_data = {'error': str(e)}
            task.save()
            return JsonResponse({'ok': False, 'error': str(e)}, status=500)
    else:
        # 优先入队 Celery；broker 不可用/入队失败时回退本地线程，保证任务始终会被执行
        from .tasks import run_agent_task
        try:
            job = run_agent_task.delay(task.id)
            return JsonResponse({'ok': True, 'task_id': task.id, 'status': 'queued', 'job_id': str(job.id)})
        except Exception:
            logger.exception('Celery 入队失败，回退本地线程执行 task %s', task.id)
            import threading
            threading.Thread(target=run_agent_task, args=(task.id,), daemon=False).start()
            return JsonResponse({'ok': True, 'task_id': task.id, 'status': 'running'})



@require_GET
def api_task_status(request, pk: int):
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)
    task = get_object_or_404(AgentTask, pk=pk)
    if not (request.user.is_staff or task.user == request.user):
        return JsonResponse({'error': '无权查看该任务'}, status=403)
    return JsonResponse({
        'id': task.id,
        'status': task.status,
        'progress': getattr(task, 'progress', 0),
        'output': task.output_data,
        'result_summary': getattr(task, 'result_summary', ''),
    })


@require_GET
def api_search_resources(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)
    q = request.GET.get('q', '').strip()
    rtype = request.GET.get('type')
    tag = request.GET.get('tag')
    qs = LearningResource.objects.all()
    if not request.user.is_staff:
        qs = qs.filter(author=request.user)
    if rtype:
        qs = qs.filter(resource_type=rtype)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(content__icontains=q))
    results = []
    # 如果指定 tag，做 Python 端过滤以兼容 SQLite
    for r in qs.order_by('-created_at')[:200]:
        if tag:
            try:
                if not (r.tags and tag in r.tags):
                    continue
            except Exception:
                continue
        results.append({'id': r.id, 'title': r.title, 'type': r.resource_type, 'tags': r.tags or [], 'created_at': r.created_at})
    try:
        # 记录搜索行为以更新画像（弱信号）
        if request.user.is_authenticated and (q or tag):
            record_profile_event(request.user, 'resource_search', {'q': q, 'tag': tag, 'result_count': len(results)}, source_app='agent_system.resource', confidence=0.25)
    except Exception:
        logger.exception('Failed to record resource_search event')
    return JsonResponse({'ok': True, 'count': len(results), 'results': results})


@require_GET
def api_export_resource(request, pk: int):
    # 导出资源为 zip（包含 content.txt 和 metadata.json）
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)
    res = get_object_or_404(LearningResource, pk=pk)
    if not (request.user.is_staff or res.author == request.user):
        return JsonResponse({'error': '无权导出该资源'}, status=403)
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        # 内容
        zf.writestr('content.txt', res.content or '')
        # metadata
        zf.writestr('metadata.json', json.dumps({'id': res.id, 'title': res.title, 'resource_type': res.resource_type, 'metadata': res.metadata, 'tags': res.tags}, ensure_ascii=False, default=str))
    mem.seek(0)
    resp = HttpResponse(mem.read(), content_type='application/zip')
    resp['Content-Disposition'] = f'attachment; filename=resource_{res.id}.zip'
    try:
        if request.user.is_authenticated:
            record_profile_event(request.user, 'resource_exported', {'resource_id': res.id, 'resource_type': res.resource_type}, source_app='agent_system.resource', confidence=0.7)
    except Exception:
        logger.exception('Failed to record resource_exported event for %s', getattr(res, 'id', None))
    return resp


@require_POST
def api_quiz_grade(request):
    # 对 quiz 类型资源进行自动批改，返回分数与明细
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        data = request.POST.dict()
    resource_id = data.get('resource_id')
    answers = data.get('answers') or {}
    if not resource_id:
        return JsonResponse({'error': '缺少 resource_id'}, status=400)
    res = get_object_or_404(LearningResource, pk=resource_id)
    if not (request.user.is_staff or res.author == request.user):
        return JsonResponse({'error': '无权访问该资源'}, status=403)
    if res.resource_type != 'quiz':
        return JsonResponse({'error': '资源不是练习题类型'}, status=400)
    # 解析 content，支持多种 JSON 格式
    try:
        payload = json.loads(res.content)
    except Exception:
        return JsonResponse({'error': '资源内容无法解析为 JSON，无法自动批改'}, status=400)
    questions = None
    if isinstance(payload, dict):
        for key in ('questions', 'items', 'problems', 'data'):
            if key in payload and isinstance(payload[key], list):
                questions = payload[key]
                break
    elif isinstance(payload, list):
        questions = payload
    if not isinstance(questions, list):
        return JsonResponse({'error': '找不到题目列表，无法批改'}, status=400)
    total_score = 0
    max_score = 0
    details = []
    for idx, q in enumerate(questions):
        qid = q.get('id') or q.get('qid') or str(idx)
        correct = q.get('correct_answer') or q.get('answer') or q.get('correct')
        item_score = q.get('score') or q.get('points') or 1
        provided = answers.get(str(qid)) if isinstance(answers, dict) else None
        awarded = 0
        try:
            if correct is not None and provided is not None and str(provided).strip().lower() == str(correct).strip().lower():
                awarded = item_score
        except Exception:
            awarded = 0
        total_score += awarded
        max_score += item_score
        details.append({'id': qid, 'awarded': awarded, 'max': item_score, 'correct': correct, 'provided': provided})
    return JsonResponse({'ok': True, 'score': total_score, 'max_score': max_score, 'details': details})


@require_POST
def api_compute_embedding(request, pk: int):
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)
    res = get_object_or_404(LearningResource, pk=pk)
    if not (request.user.is_staff or res.author == request.user):
        return JsonResponse({'error': '无权操作该资源'}, status=403)
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        data = request.POST.dict()
    text = data.get('text') or res.content or ''
    vec = compute_embedding(text)
    res.embedding = vec
    res.save()
    return JsonResponse({'ok': True, 'id': res.id, 'embedding_dim': len(vec)})


@require_GET
def api_nearest_resources(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)
    # 支持通过 resource_id 或直接提交 embedding
    resource_id = request.GET.get('resource_id')
    try:
        k = int(request.GET.get('k', '5'))
    except (ValueError, TypeError):
        k = 5
    emb = None
    if resource_id:
        try:
            r = LearningResource.objects.get(pk=int(resource_id))
            emb = r.embedding or []
        except Exception:
            return JsonResponse({'error': '指定资源不存在'}, status=400)
    else:
        # 解析 embedding 参数（逗号分隔）或文本
        emb_param = request.GET.get('embedding')
        text = request.GET.get('text')
        if emb_param:
            try:
                emb = [float(x) for x in emb_param.split(',')]
            except Exception:
                return JsonResponse({'error': 'embedding 格式错误'}, status=400)
        elif text:
            emb = compute_embedding(text)
        else:
            return JsonResponse({'error': '请提供 resource_id 或 text 或 embedding'}, status=400)

    candidates = LearningResource.objects.exclude(embedding=[]).all()
    scored = []
    for c in candidates:
        try:
            score = cosine_similarity(emb, c.embedding or [])
            scored.append((score, c))
        except Exception:
            continue
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for sc, c in scored[:k]:
        out.append({'id': c.id, 'title': c.title, 'score': sc, 'type': c.resource_type})
    return JsonResponse({'ok': True, 'results': out})


@require_http_methods(['GET', 'POST'])
def api_stream_generate(request):
    """流式生成接口（将讯飞的流式输出直接透传为 SSE）。"""
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)
    if request.method == 'GET':
        prompt = request.GET.get('topic') or request.GET.get('prompt') or ''
    else:
        try:
            data = json.loads(request.body.decode('utf-8') or '{}')
        except Exception:
            data = request.POST.dict()
        prompt = data.get('prompt') or data.get('topic') or ''
    client = profile_builder.client
    try:
        stream_iter = client.stream_generate(prompt)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    def event_stream():
        for chunk in stream_iter:
            # chunk 为字符串，按 SSE 格式输出
            yield f'data: {chunk}\n\n'

    return StreamingHttpResponse(event_stream(), content_type='text/event-stream')


def _profile_update_summary(profile_signal):
    """把对话中抽取到的画像信号转成给前端展示的"画像已更新"摘要（哪些维度更新了）。
    用于让"画像随学随新"在聊天时可见，而不是后台静默更新。"""
    if not isinstance(profile_signal, dict) or not profile_signal:
        return None
    labels = {
        'knowledge_profile': '知识掌握', 'cognitive_style': '认知风格', 'learning_goals': '学习目标',
        'misconceptions': '易错点', 'learning_preferences': '学习偏好', 'engagement': '参与度',
    }
    dims = [labels[k] for k in labels if profile_signal.get(k)]
    return {'updated': True, 'dimensions': dims} if dims else None


@require_POST
def api_conversation_send(request):
    """开始或继续一个 Conversation 并返回 Assistant 的同步回复（并持久化消息）。"""
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)

    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        data = request.POST.dict()

    text = data.get('text') or data.get('message') or ''
    course_id = data.get('course_id')
    material_id = data.get('material_id')
    current_page = data.get('current_page')
    mode = data.get('mode', 'chat')  # 'chat' 或 'learning'
    recent_topics = str(data.get('recent_topics') or '').strip()[:200]
    kickoff = bool(data.get('kickoff'))  # 费曼互教开场：让"小艾"先开口向学生提问，本轮没有真正的学生发言

    if not text:
        return JsonResponse({'error': '缺少 text 参数'}, status=400)
    
    # 导入 USER-LLM R1 模块
    try:
        from .services.user_llm_r1_reasoning import USERLLM_R1
        user_llm_r1_available = True
    except Exception as e:
        logger.error(f"Failed to import USERLLM_R1: {e}")
        user_llm_r1_available = False

    convo_id = data.get('conversation_id')
    if convo_id:
        try:
            convo = Conversation.objects.get(pk=int(convo_id), user=request.user)
        except Exception:
            convo = Conversation.objects.create(user=request.user, title=(text[:120] if text else '对话'))
    else:
        convo = Conversation.objects.create(user=request.user, title=(text[:120] if text else '对话'))

    # 保存用户消息（流式失败回退到本接口时，用户消息已由流式端点创建，跳过以免重复）
    skip_user_message = bool(data.get('skip_user_message')) or kickoff  # 开场轮不落学生消息
    if skip_user_message:
        msg = None
    else:
        try:
            msg = Message.objects.create(conversation=convo, role='student', content=text, content_type='text')
        except Exception:
            msg = None

    material_context = _build_material_context_block(text, request.user, course_id=course_id, material_id=material_id, current_page=current_page)
    learning_plan_context = _load_latest_learning_plan_context(request.user, material_context.get('course'))

    # 费曼互教模式：根据当前主题，从记忆流中检索"小艾"对这位同学的历史印象
    peer_topic = ''
    peer_memories = []
    if mode == 'peer_teaching':
        peer_topic = (
            getattr(material_context.get('material'), 'title', '')
            or getattr(material_context.get('course'), 'title', '')
            or recent_topics
            or text[:50]
        )
        try:
            from .agents import PeerLearnerAgent
            peer_profile, _ = StudentProfile.objects.get_or_create(user=request.user)
            peer_memories = PeerLearnerAgent(request.user, client=profile_builder.client).select_relevant_memories(
                peer_profile.peer_memory_stream or [], peer_topic, k=3
            )
        except Exception:
            peer_memories = []

    # 提取对话画像信号并记录事件（chat 与 learning 模式都执行）
    llm_r1_result = None
    if mode == 'learning' and user_llm_r1_available:
        try:
            llm_r1_result = USERLLM_R1.process_interaction(
                request.user,
                text,
                {
                    'conversation_id': convo.id,
                    'course_id': course_id,
                    'material_id': material_id,
                    'current_page': current_page
                }
            )
            logger.info(f"USER-LLM R1 inference completed: success={llm_r1_result.get('success')}, confidence={llm_r1_result.get('confidence')}")
        except Exception as e:
            logger.error(f"USER-LLM R1 failed: {e}")
            llm_r1_result = None

    # 传统画像提取作为备选
    profile_signal = _extract_profile_from_chat_text(text)

    # 如果 USER-LLM R1 成功，合并推理结果
    if llm_r1_result and llm_r1_result.get('success'):
        profile_delta = llm_r1_result.get('profile_delta', {})
        if profile_delta:
            # 合并到 profile_signal。R1 的 profile_delta 字段类型不稳定（learning_preferences
            # 有时是 dict、有时是标签 list），不做类型校验直接 .update()/.extend() 会抛
            # "dictionary update sequence element..." 而让整条对话 500。逐字段按类型安全合并。
            _pref = profile_delta.get('learning_preferences')
            _pref_tgt = profile_signal.setdefault('learning_preferences', {})
            if isinstance(_pref_tgt, dict):
                if isinstance(_pref, dict):
                    _pref_tgt.update(_pref)
                elif isinstance(_pref, list):
                    # 偏好以标签列表返回时，并入内容格式偏好，保留信息而不是丢弃
                    _existing = _pref_tgt.get('content_formats') or []
                    _pref_tgt['content_formats'] = list(dict.fromkeys(
                        [str(x) for x in _existing] + [str(x) for x in _pref]
                    ))

            _goals = profile_delta.get('learning_goals')
            if isinstance(_goals, list):
                _goals_tgt = profile_signal.setdefault('learning_goals', [])
                if isinstance(_goals_tgt, list):
                    _goals_tgt.extend(_goals)

            if profile_delta.get('cognitive_style'):
                profile_signal['cognitive_style'] = profile_delta['cognitive_style']

    try:
        record_profile_event(
            request.user,
            'course_ai_message' if material_context.get('course') else 'conversation_message',
            {
                'text': text[:800],
                'conversation_id': convo.id,
                'profile_delta': profile_signal,
                'course_title': getattr(material_context.get('course'), 'title', ''),
                'material_title': getattr(material_context.get('material'), 'title', ''),
                'llm_r1_used': user_llm_r1_available,
                'llm_r1_confidence': llm_r1_result.get('confidence') if llm_r1_result else None,
            },
            source_app='agent_system',
            course_id=getattr(material_context.get('course'), 'id', None),
            material_id=getattr(material_context.get('material'), 'id', None),
            confidence=llm_r1_result.get('confidence', 0.7) if (llm_r1_result and llm_r1_result.get('success')) else (0.7 if profile_signal else 0.35),
        )
    except Exception:
        pass

    # 发送画像自动更新信号（对话类型推断，仅学习模式）
    if mode == 'learning':
        try:
            from agent_system.services.profile_signal_collector import ProfileSignalCollector, ProfileSignalType

            # 推断问题类型
            question_type = 'general'
            if any(kw in text for kw in ['为什么', '原理', '怎么', '如何', '为什么']):
                question_type = 'explanation'
            elif any(kw in text for kw in ['练习', '做题', '测试', '题']):
                question_type = 'practice'
            elif any(kw in text for kw in ['不懂', '困惑', '不清楚', '不会']):
                question_type = 'confusion'

            ProfileSignalCollector.emit(
                user=request.user,
                signal_type=ProfileSignalType.CHAT_QUESTION_TYPE,
                trigger_source='tutor_chat',
                data={
                    'question_type': question_type,
                    'text': text[:500],
                    'conversation_id': convo.id,
                },
                course_id=getattr(material_context.get('course'), 'id', None),
                material_id=getattr(material_context.get('material'), 'id', None),
            )

            # 如果检测到困惑信号
            if question_type == 'confusion':
                ProfileSignalCollector.emit(
                    user=request.user,
                    signal_type=ProfileSignalType.CHAT_CONFUSION_SIGNAL,
                    trigger_source='tutor_chat',
                    data={
                        'topic': getattr(material_context.get('material'), 'title', '') or getattr(material_context.get('course'), 'title', ''),
                        'question_text': text[:500],
                        'conversation_id': convo.id,
                    },
                    course_id=getattr(material_context.get('course'), 'id', None),
                    material_id=getattr(material_context.get('material'), 'id', None),
                )
        except Exception:
            pass

    # 构建 prompt：先把当前轮对话写入画像，再注入上下文
    # 对话模式不使用画像信息，学习模式使用画像
    try:
        if mode == 'learning':
            profile = _build_conversation_profile(convo, latest_text=text)
            profile_summary = json.dumps({
                'knowledge_profile': profile.knowledge_profile,
                'cognitive_style': profile.cognitive_style,
                'learning_goals': profile.learning_goals,
                'learning_preferences': profile.learning_preferences,
            }, ensure_ascii=False)
        else:
            profile = None
            profile_summary = ''
    except Exception:
        profile = None
        profile_summary = ''

    # 类比迁移教学：从知识画像中挑选学生已掌握的概念，供讲解新概念时类比
    # （Analogical Scaffolding, Yasunaga et al. 2023 ICLR）
    analogy_seed = ''
    if mode == 'learning' and profile is not None:
        try:
            analogy_seed = build_analogy_seed(profile.knowledge_profile or {})
        except Exception:
            analogy_seed = ''

    # 自我解释提示（Self-Explanation Effect, Chi et al.）：如果上一轮导师邀请
    # 学生用自己的话解释讲解内容，这一轮的学生消息就是那段自我解释，需要评估
    # 并据此给出反馈、更新知识画像。
    self_explanation_feedback = ''
    if mode == 'learning':
        try:
            last_assistant = convo.messages.filter(role='assistant').order_by('-created_at').first()
            if last_assistant and isinstance(last_assistant.metadata, dict) and last_assistant.metadata.get('awaiting_self_explanation'):
                from .agents import SelfExplanationAgent

                explanation_given = last_assistant.metadata.get('explanation_text') or last_assistant.content
                se_topic = (
                    getattr(material_context.get('material'), 'title', '')
                    or getattr(material_context.get('course'), 'title', '')
                    or text[:50]
                )
                evaluator = SelfExplanationAgent(request.user, client=profile_builder.client)
                se_result = evaluator.evaluate(se_topic, explanation_given, text)

                concept = se_result.get('concept') or se_topic
                self_explanation_feedback = (
                    f"学生刚刚尝试用自己的话解释了上面的内容（关于\"{concept}\"），"
                    f"评估反馈：{se_result.get('feedback', '')}。"
                    "请先简短回应这段自我解释（肯定讲对的地方，纠正遗漏或误解），再继续教学。"
                )

                profile_delta = {}
                if se_result.get('misconceptions'):
                    profile_delta['misconceptions'] = se_result['misconceptions']
                if concept:
                    profile_delta['knowledge_profile'] = {
                        concept: {
                            'mastery_score': se_result.get('quality_score', 70),
                            'last_score': se_result.get('quality_score', 70),
                            'source': 'self_explanation',
                            'updated_at': timezone.now().isoformat(),
                        }
                    }
                if profile_delta:
                    record_profile_event(
                        request.user,
                        'self_explanation_evaluation',
                        {
                            'conversation_id': convo.id,
                            'eval': se_result,
                            'profile_delta': profile_delta,
                        },
                        source_app='agent_system',
                        confidence=0.6,
                        apply_now=True,
                    )
        except Exception:
            logger.exception('自我解释评估失败')
            self_explanation_feedback = ''

    # 取最近 6 条消息作为显式上下文，并用更早消息构建长期记忆
    history = []
    history_messages = []
    try:
        history_messages = list(convo.messages.order_by('-created_at')[:14][::-1])
        for m in history_messages[-6:]:
            role = 'Student' if m.role == 'student' else 'Assistant'
            history.append(f"{role}: {m.content}")
    except Exception:
        history = [text]
        history_messages = []

    guidance_summary = ''
    try:
        if mode == 'learning':
            guidance_summary = _build_guidance_summary(profile)
        elif mode == 'ta':
            guidance_summary = '我是这门课的助教，看课件时有任何不懂的地方，随时问我。'
        else:
            guidance_summary = '我是你的AI助手，有什么问题想问我吗？'
    except Exception:
        guidance_summary = '我是你的AI助手，有什么问题想问我吗？' if mode in ('chat', 'ta') else '我会先收集你的目标、基础和偏好，再逐步带你学习。'

    known_summary = ''
    try:
        if mode == 'learning':
            known_summary = _known_profile_summary(profile)
        else:
            known_summary = ''
    except Exception:
        known_summary = '' if mode == 'chat' else '暂无稳定画像信息'

    system_prompt = _build_conversation_system_prompt(
        text,
        guidance_summary=guidance_summary,
        known_summary=known_summary,
        material_context=material_context,
        tutor_mode=mode,
        memories=peer_memories,
        analogy_seed=analogy_seed,
        recent_topics=recent_topics,
    )

    prompt = f"{system_prompt}\n"
    if mode == 'learning' and profile_summary:
        prompt += f"学生画像: {profile_summary}\n"
    if self_explanation_feedback:
        prompt += self_explanation_feedback + "\n"

    # 费曼互教开场：由"小艾"主动开口，先自我介绍再就本次主题向学生提出第一个具体问题
    if kickoff and mode == 'peer_teaching':
        _kick_topic = recent_topics or text or '这个主题'
        prompt += (
            f"【开场要求】现在互教刚刚开始，还没有任何对话历史。请你作为小艾，"
            f"先用一句话打招呼、说你在学《{_kick_topic}》时有个地方没太搞懂，"
            f"然后就这个主题向同学提出**第一个具体的问题**，请他讲给你听。"
            f"只问一个问题，不要自己回答，也不要一次问很多。\n"
        )

    # 注入AKT知识追踪信息
    if mode == 'learning':
        try:
            kt_summary = get_kt_summary(str(request.user.id))
            if kt_summary and kt_summary.get('concepts'):
                prompt += f"知识掌握度追踪:\n"
                prompt += f"- 已掌握知识点: {kt_summary['mastered_concepts']}个\n"
                prompt += f"- 学习中知识点: {kt_summary['learning_concepts']}个\n"
                prompt += f"- 新知识点: {kt_summary['new_concepts']}个\n"
                prompt += f"- 平均掌握度: {kt_summary['average_mastery']}\n"
                
                # 添加薄弱知识点
                weak_concepts = [c for c in kt_summary['concepts'] if c['mastery_probability'] < 0.5]
                if weak_concepts:
                    prompt += "- 薄弱知识点: " + ", ".join([c['name'] for c in weak_concepts[:5]]) + "\n"
                
                # 添加学习建议
                recommendations = kt_summary.get('recommendations', [])
                if recommendations:
                    prompt += "- 建议重点复习: " + ", ".join([r['name'] for r in recommendations[:3]]) + "\n"
                prompt += "\n"
        except Exception as e:
            logger.error(f"注入知识追踪信息失败: {e}")
    
    long_term_memory = _build_long_term_memory(convo, older_messages=history_messages[:-6], learning_plan_context=learning_plan_context)
    if long_term_memory:
        prompt += "长期对话记忆:\n" + long_term_memory + "\n"
    if material_context.get('text'):
        prompt += "课程资料上下文:\n" + material_context['text'] + "\n"
    if learning_plan_context:
        prompt += "最近学习路径上下文:\n" + _format_learning_plan_context(learning_plan_context) + "\n"
    prompt += "对话历史:\n" + "\n".join(history) + "\nAssistant:"

    client = profile_builder.client
    try:
        reply = client.generate_text(prompt)
    except Exception as e:
        reply = f"生成失败：{e}"

    # 自我解释提示（Self-Explanation Effect）：导师给出讲解类回复后，
    # 邀请学生用自己的话复述，下一轮据此评估并反馈。
    explanation_text = reply
    awaiting_self_explanation = False
    if mode == 'learning':
        try:
            if _detect_response_mode(text) == 'direct_explanation':
                reply = reply + SELF_EXPLANATION_PROMPT
                awaiting_self_explanation = True
        except Exception:
            pass

    # 费曼互教终点：小艾判断学生已把知识点讲清楚时会用标记收尾；加下限(至少讲够2轮)防止刚开口就收尾，
    # 加上限(讲到8轮)兜底避免无限追问。
    peer_complete = False
    if mode == 'peer_teaching':
        _markers = ['[本次互教结束]', '【本次互教结束】', '[互教结束]', '【互教结束】']
        try:
            _student_turns = convo.messages.filter(role='student').count()
        except Exception:
            _student_turns = 0
        _has_marker = any(mk in reply for mk in _markers)
        if _has_marker:
            # 标记文本无论是否真的结束都不该显示给用户，先去掉
            for mk in _markers:
                reply = reply.replace(mk, '')
            reply = reply.strip()
        if (_has_marker and _student_turns >= 2) or _student_turns >= 8:
            peer_complete = True

    stage = _infer_learning_stage(profile, len(history) + 1) if profile else {'index': 1, 'key': 'diagnosis', 'label': '目标诊断'}
    current_question = _infer_current_question(profile, stage, text, reply)
    guided_actions = _infer_guided_actions(text, reply)
    response_options = []
    if _should_offer_response_options(current_question, reply):
        response_options = _options_for_question(current_question.get('key'), profile, stage, text, reply)
    prompt_payload = {
        'question': current_question,
        'options': response_options,
    }

    _update_conversation_memory(convo, text, reply, learning_plan_context=learning_plan_context)

    # 保存助手消息
    try:
        Message.objects.create(
            conversation=convo,
            role='assistant',
            content=reply,
            content_type='text',
            metadata={
                'source': 'xinghuo' if getattr(client, 'api_key', None) else 'placeholder',
                'persona': mode,  # chat/ta/learning/peer_teaching —— 标记该回复出自哪个角色
                'prompt_payload': prompt_payload,
                'course_context': {
                    'course_id': getattr(material_context.get('course'), 'id', None),
                    'course_title': getattr(material_context.get('course'), 'title', None),
                    'material_id': getattr(material_context.get('material'), 'id', None),
                    'material_title': getattr(material_context.get('material'), 'title', None),
                    'current_page': material_context.get('current_page'),
                    'references': material_context.get('chunks', []),
                    'course_map': material_context.get('course_map', {}),
                    'learning_plan': learning_plan_context,
                },
                'conversation_memory': convo.context_summary,
                'awaiting_self_explanation': awaiting_self_explanation,
                'peer_complete': peer_complete,
                'explanation_text': explanation_text,
            }
        )
    except Exception:
        pass

    try:
        convo.save()
    except Exception:
        pass

    # 费曼互教模式：每3轮学生发言，让"小艾"评估一次讲解质量并更新学生画像
    if mode == 'peer_teaching':
        try:
            student_turns = convo.messages.filter(role='student').count()
            if student_turns > 0 and student_turns % 3 == 0:
                from .agents import PeerLearnerAgent

                conversation_text = "\n".join(history + [f"Assistant: {reply}"])
                topic = peer_topic or (
                    getattr(material_context.get('material'), 'title', '')
                    or getattr(material_context.get('course'), 'title', '')
                    or text[:50]
                )
                evaluator = PeerLearnerAgent(request.user, client=client)
                eval_result = evaluator.evaluate_session(topic, conversation_text, {})

                profile_delta = {}
                if eval_result.get('cognitive_style_observation'):
                    profile_delta['cognitive_style'] = eval_result['cognitive_style_observation']
                if eval_result.get('misconceptions_detected'):
                    profile_delta['misconceptions'] = eval_result['misconceptions_detected']

                if profile_delta:
                    record_profile_event(
                        request.user,
                        'peer_teaching_evaluation',
                        {
                            'conversation_id': convo.id,
                            'eval': eval_result,
                            'profile_delta': profile_delta,
                        },
                        source_app='agent_system',
                        confidence=0.6,
                        apply_now=True,
                    )

                # 写入"小艾"的记忆流：把本次评估中值得记住的观察追加进
                # StudentProfile.peer_memory_stream（生成式智能体记忆流）
                memory_observations = eval_result.get('memory_observations') or []
                if memory_observations:
                    peer_profile, _ = StudentProfile.objects.get_or_create(user=request.user)
                    memory_stream = list(peer_profile.peer_memory_stream or [])
                    now_iso = timezone.now().isoformat()
                    for obs in memory_observations:
                        memory_stream.append({
                            'id': str(uuid.uuid4()),
                            'type': 'observation',
                            'content': obs.get('content', ''),
                            'importance': obs.get('importance', 5),
                            'topic': topic,
                            'created_at': now_iso,
                            'last_accessed_at': now_iso,
                        })
                    reflection = evaluator.generate_reflection(memory_stream, topic)
                    if reflection:
                        reflection.update({
                            'id': str(uuid.uuid4()),
                            'created_at': now_iso,
                            'last_accessed_at': now_iso,
                        })
                        memory_stream.append(reflection)
                    # 限制记忆流长度，避免无限增长
                    peer_profile.peer_memory_stream = memory_stream[-50:]
                    peer_profile.save(update_fields=['peer_memory_stream'])
        except Exception:
            logger.exception('费曼互教画像更新失败')

    # AKT知识追踪：分析AI回复中的知识点并更新掌握度
    kt_summary = None
    if mode == 'learning':
        try:
            tracer = get_tracer(str(request.user.id))
            
            # 从本轮真实对话中提取知识点（LLM 抽取，学科无关；失败回退关键词表）
            knowledge_concepts = _extract_knowledge_concepts_from_reply(
                reply, material_context, client=client, student_question=text)
            
            # 根据学生问题和AI回复推断掌握度
            for concept_id, concept_info in knowledge_concepts.items():
                # 判断学生是否理解（基于问题类型和回复内容）
                is_understood = _infer_student_understanding(text, reply, concept_info)
                
                # 记录交互
                record_kt_interaction(
                    user_id=str(request.user.id),
                    concept_id=concept_id,
                    is_correct=is_understood,
                    difficulty=concept_info.get('difficulty', 0.5),
                    hint_used=concept_info.get('hint_used', False),
                )
            
            # 获取掌握度摘要
            kt_summary = get_kt_summary(str(request.user.id))
        except Exception as e:
            logger.error(f"知识追踪失败: {e}")
            kt_summary = None

    return JsonResponse({
        'ok': True,
        'conversation_id': convo.id,
        'conversation': _serialize_conversation_summary(convo),
        'assistant': reply,
        'guided_actions': guided_actions,
        'response_options': response_options,
        'current_question': current_question,
        'prompt_payload': prompt_payload,
        'coach_tip': guidance_summary,
        'conversation_memory': convo.context_summary,
        'knowledge_tracing': kt_summary,  # 添加知识追踪结果
        'peer_complete': peer_complete,  # 费曼互教是否已收尾（小艾判断学生已讲清楚）
        'profile_update': _profile_update_summary(profile_signal),  # "画像已更新"提示（随学随新可见）
        'course_context': {
            'course_id': getattr(material_context.get('course'), 'id', None),
            'course_title': getattr(material_context.get('course'), 'title', None),
            'material_id': getattr(material_context.get('material'), 'id', None),
            'material_title': getattr(material_context.get('material'), 'title', None),
            'current_page': material_context.get('current_page'),
            'references': material_context.get('chunks', []),
            'course_map': material_context.get('course_map', {}),
            'learning_plan': learning_plan_context,
        },
    })


@require_GET
def api_conversation_stream(request):
    """SSE 流式输出对话回复；参数：conversation_id 或 prompt。"""
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)

    convo_id = request.GET.get('conversation_id')
    prompt_param = (request.GET.get('prompt') or '').strip()
    course_id = request.GET.get('course_id')
    material_id = request.GET.get('material_id')
    current_page = request.GET.get('current_page')
    mode = request.GET.get('mode', 'learning')
    convo = None
    latest_text = ''
    if convo_id:
        try:
            convo = Conversation.objects.get(pk=int(convo_id), user=request.user)
        except Exception:
            return JsonResponse({'error': 'conversation not found'}, status=404)
    if prompt_param:
        latest_text = prompt_param
        if convo is None:
            convo = Conversation.objects.create(user=request.user, title=(prompt_param[:120] if prompt_param else '对话'))
        try:
            Message.objects.create(conversation=convo, role='student', content=prompt_param, content_type='text')
        except Exception:
            pass

    material_context = _build_material_context_block(latest_text or prompt_param, request.user, course_id=course_id, material_id=material_id, current_page=current_page)
    if latest_text or prompt_param:
        profile_signal = _extract_profile_from_chat_text(latest_text or prompt_param)
        try:
            record_profile_event(
                request.user,
                'course_ai_message' if material_context.get('course') else 'conversation_message',
                {
                    'text': (latest_text or prompt_param)[:800],
                    'conversation_id': getattr(convo, 'id', None),
                    'profile_delta': profile_signal,
                    'course_title': getattr(material_context.get('course'), 'title', ''),
                    'material_title': getattr(material_context.get('material'), 'title', ''),
                },
                source_app='agent_system.stream',
                course_id=getattr(material_context.get('course'), 'id', None),
                material_id=getattr(material_context.get('material'), 'id', None),
                confidence=0.7 if profile_signal else 0.35,
            )
        except Exception:
            pass

        # 发送画像自动更新信号（对话类型推断）
        try:
            from agent_system.services.profile_signal_collector import ProfileSignalCollector, ProfileSignalType
            from agent_system.services.dialog_profile_builder import get_profile_summary_for_user
            
            text_for_analysis = latest_text or prompt_param
            
            # 分析消息，更新画像
            question_type = 'general'
            if any(kw in text_for_analysis for kw in ['为什么', '原理', '怎么', '如何', '为什么']):
                question_type = 'explanation'
            elif any(kw in text_for_analysis for kw in ['练习', '做题', '测试', '题']):
                question_type = 'practice'
            elif any(kw in text_for_analysis for kw in ['不懂', '困惑', '不清楚', '不会']):
                question_type = 'confusion'
            
            ProfileSignalCollector.emit(
                user=request.user,
                signal_type=ProfileSignalType.CHAT_QUESTION_TYPE,
                trigger_source='tutor_chat_stream',
                data={
                    'question_type': question_type,
                    'text': text_for_analysis[:500],
                    'conversation_id': getattr(convo, 'id', None),
                },
                course_id=getattr(material_context.get('course'), 'id', None),
                material_id=getattr(material_context.get('material'), 'id', None),
            )
            
            # 如果检测到困惑信号，额外发送困惑信号
            if question_type == 'confusion':
                ProfileSignalCollector.emit(
                    user=request.user,
                    signal_type=ProfileSignalType.CHAT_CONFUSION_SIGNAL,
                    trigger_source='tutor_chat_stream',
                    data={
                        'topic': getattr(material_context.get('material'), 'title', '') or getattr(material_context.get('course'), 'title', ''),
                        'question_text': text_for_analysis[:500],
                        'conversation_id': getattr(convo, 'id', None),
                    },
                    course_id=getattr(material_context.get('course'), 'id', None),
                    material_id=getattr(material_context.get('material'), 'id', None),
                )
            
            # 获取当前画像摘要（用于日志记录）
            profile_summary = get_profile_summary_for_user(request.user)
            logger.debug(f"User {request.user.id} profile updated from chat: {profile_summary}")
            
        except Exception as e:
            logger.exception('Failed to process dialog profile update')

    if convo is not None:
        profile = _build_conversation_profile(convo, latest_text=latest_text)
        profile_summary = json.dumps({
            'knowledge_profile': profile.knowledge_profile,
            'cognitive_style': profile.cognitive_style,
            'learning_goals': profile.learning_goals,
            'learning_preferences': profile.learning_preferences,
        }, ensure_ascii=False)
        history = []
        history_messages = list(convo.messages.order_by('-created_at')[:14][::-1])
        for m in history_messages[-6:]:
            role = 'Student' if m.role == 'student' else 'Assistant'
            history.append(f"{role}: {m.content}")

        guidance_summary = ''
        try:
            guidance_summary = _build_guidance_summary(profile)
        except Exception:
            guidance_summary = '我会先收集你的目标、基础和偏好，再逐步带你学习。'

        known_summary = ''
        try:
            known_summary = _known_profile_summary(profile)
        except Exception:
            known_summary = '暂无稳定画像信息'

        system_prompt = _build_conversation_system_prompt(
            latest_text or prompt_param,
            guidance_summary=guidance_summary,
            known_summary=known_summary,
            material_context=material_context,
            tutor_mode=mode,
        )
        # 只有 learning 模式才注入学生画像；chat/ta 走流式时不应带学习画像（与非流式行为一致）
        prompt = f"{system_prompt}\n"
        if mode == 'learning':
            prompt += f"学生画像: {profile_summary}\n"
        long_term_memory = _build_long_term_memory(convo, older_messages=history_messages[:-6])
        if long_term_memory:
            prompt += "长期对话记忆:\n" + long_term_memory + "\n"
        if material_context.get('text'):
            prompt += "课程资料上下文:\n" + material_context['text'] + "\n"
        prompt += "对话历史:\n" + "\n".join(history) + "\nAssistant:"
    elif prompt_param:
        prompt = prompt_param
    else:
        return JsonResponse({'error': 'missing prompt or conversation_id'}, status=400)

    client = profile_builder.client

    def event_stream():
        parts = []
        # 流式无法像 generate_text 那样内部重试，这里在端点层重试一次：本次没产出就退避再来
        for _attempt in range(2):
            got_any = False
            try:
                for chunk in client.stream_generate(prompt):
                    if not chunk:
                        continue
                    got_any = True
                    parts.append(chunk)
                    # SSE 多行安全：内容里的换行必须每行都加 data: 前缀，否则会截断
                    for _ln in str(chunk).split('\n'):
                        yield f'data: {_ln}\n'
                    yield '\n'
            except Exception as exc:
                logger.warning('对话流式生成异常：%s', exc)
            if got_any:
                break
            if _attempt == 0:
                import time as _t
                _t.sleep(1.0)
        # 完成后持久化为一条助手消息，并补齐这一轮的引导状态
        full = ''.join(parts)
        if not full.strip():
            # 流式没产出任何内容（接口不可用）→ 通知前端回退非流式接口，不落库假消息
            # 用自定义事件名 failed，避免和 EventSource 内建的 error(连接错误) 事件混淆
            yield 'event: failed\ndata: {"error": "stream_unavailable"}\n\n'
            return
        if convo is not None:
            try:
                stage = _infer_learning_stage(profile, len(history) + 1) if profile else {'index': 1, 'key': 'diagnosis', 'label': '目标诊断'}
                current_question = _infer_current_question(profile, stage, latest_text, full)
                response_options = []
                if _should_offer_response_options(current_question, full):
                    response_options = _options_for_question(current_question.get('key'), profile, stage, latest_text, full)
                prompt_payload = {
                    'question': current_question,
                    'options': response_options,
                }
                _update_conversation_memory(convo, latest_text, full)
                Message.objects.create(
                    conversation=convo,
                    role='assistant',
                    content=full,
                    content_type='text',
                    metadata={
                        'streamed': True,
                        'persona': mode,  # chat/ta/learning —— 标记该回复出自哪个模式，供历史记录跳回对应模式
                        'source': 'xinghuo' if getattr(client, 'api_key', None) else 'placeholder',
                        'prompt_payload': prompt_payload,
                        'course_context': {
                            'course_id': getattr(material_context.get('course'), 'id', None),
                            'course_title': getattr(material_context.get('course'), 'title', None),
                            'material_id': getattr(material_context.get('material'), 'id', None),
                            'material_title': getattr(material_context.get('material'), 'title', None),
                            'current_page': material_context.get('current_page'),
                            'references': material_context.get('chunks', []),
                            'course_map': material_context.get('course_map', {}),
                        },
                        'conversation_memory': convo.context_summary,
                    }
                )
                convo.save()
                done_payload = {
                    'conversation_id': convo.id,
                    'conversation': _serialize_conversation_summary(convo),
                    'prompt_payload': prompt_payload,
                    'response_options': response_options,
                    'current_question': current_question,
                    'coach_tip': guidance_summary,
                    'conversation_memory': convo.context_summary,
                    'profile_update': _profile_update_summary(profile_signal if (latest_text or prompt_param) else None),
                    'course_context': {
                        'course_id': getattr(material_context.get('course'), 'id', None),
                        'course_title': getattr(material_context.get('course'), 'title', None),
                        'material_id': getattr(material_context.get('material'), 'id', None),
                        'material_title': getattr(material_context.get('material'), 'title', None),
                        'current_page': material_context.get('current_page'),
                        'references': material_context.get('chunks', []),
                        'course_map': material_context.get('course_map', {}),
                    },
                }
                yield 'event: done\n'
                yield 'data: ' + json.dumps(done_payload, ensure_ascii=False) + '\n\n'
            except Exception:
                pass

    return StreamingHttpResponse(event_stream(), content_type='text/event-stream')


@require_GET
def api_conversation_history(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)

    convo_id = request.GET.get('conversation_id')
    conversations = Conversation.objects.filter(user=request.user).order_by('-updated_at')[:12]

    convo_list = []
    for convo in conversations:
        convo_list.append(_serialize_conversation_summary(convo))

    selected = None
    if convo_id:
        try:
            selected = Conversation.objects.get(pk=int(convo_id), user=request.user)
        except Exception:
            selected = None

    messages = []
    stage = {'index': 1, 'key': 'diagnosis', 'label': '目标诊断'}
    coach_tip = '系统会先了解你的目标、基础和偏好，再逐步推进学习。'
    current_question = {
        'key': 'goal',
        'label': '学习目标',
        'question': '你这阶段最希望达成的学习结果是什么？',
        'hint': '先明确方向，我才能决定是带你走考试路线、项目路线，还是打基础路线。',
    }
    response_options = []
    if selected:
        for message in selected.messages.order_by('created_at')[:50]:
            metadata = message.metadata if isinstance(message.metadata, dict) else {}
            messages.append({
                'id': message.id,
                'role': message.role,
                'content': message.content,
                'prompt_payload': metadata.get('prompt_payload'),
                'course_context': metadata.get('course_context'),
                'peer_complete': bool(metadata.get('peer_complete')),
                'created_at': message.created_at.strftime('%H:%M'),
            })
        profile = _build_conversation_profile(selected)
        stage = _infer_learning_stage(profile, len(messages))
        coach_tip = _build_guidance_summary(profile)
        last_user_text = ''
        last_assistant_text = ''
        for message in reversed(messages):
            if not last_assistant_text and message['role'] == 'assistant':
                last_assistant_text = message['content']
            if not last_user_text and message['role'] == 'student':
                last_user_text = message['content']
            if last_user_text and last_assistant_text:
                break
        current_question = _infer_current_question(profile, stage, last_user_text, last_assistant_text)
        response_options = []
        if _should_offer_response_options(current_question, last_assistant_text):
            response_options = _options_for_question(current_question.get('key'), profile, stage, last_user_text, last_assistant_text)

    return JsonResponse({
        'ok': True,
        'conversations': convo_list,
        'selected_conversation_id': selected.id if selected else None,
        'mode': _conversation_mode(selected) if selected else None,
        'messages': messages,
        'learning_stage': stage,
        'coach_tip': coach_tip,
        'response_options': response_options,
        'current_question': current_question,
        'stage_steps': [
            {'index': 1, 'key': 'diagnosis', 'label': '目标诊断'},
            {'index': 2, 'key': 'assessment', 'label': '基础评估'},
            {'index': 3, 'key': 'planning', 'label': '路径规划'},
            {'index': 4, 'key': 'teaching', 'label': '讲解练习'},
            {'index': 5, 'key': 'feedback', 'label': '反馈调整'},
        ],
    })


@require_POST
def api_conversation_delete(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': '请先登录'}, status=403)

    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        data = request.POST.dict()

    convo_id = data.get('conversation_id') or data.get('id')
    if not convo_id:
        return JsonResponse({'error': '缺少 conversation_id 参数'}, status=400)

    try:
        convo = Conversation.objects.get(pk=int(convo_id), user=request.user)
    except Exception:
        return JsonResponse({'error': '对话不存在'}, status=404)

    deleted_id = convo.id
    convo.delete()
    return JsonResponse({'ok': True, 'deleted_conversation_id': deleted_id})
