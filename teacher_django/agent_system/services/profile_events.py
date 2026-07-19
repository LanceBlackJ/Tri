from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from agent_system.models import ProfileEvent, StudentProfile


def _clean_text_list(values, limit=8):
    cleaned = []
    for value in values or []:
        text = str(value or '').strip()
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _bounded_score(value):
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def infer_profile_delta_from_text(text):
    text = str(text or '').strip()
    if not text:
        return {}

    lowered = text.lower()
    delta = {}
    goals = []
    for keyword in ['考试', '考研', '刷题', '项目', '竞赛', '找工作', '面试', '入门', '系统学习', '复习']:
        if keyword in text and keyword not in goals:
            goals.append(keyword)
    match = re.search(r'我(?:想|希望|准备|计划)([^。！？\n]{2,30})', text)
    if match:
        goal = match.group(1).strip('，。；;、 ')
        if goal and goal not in goals:
            goals.append(goal)
    if goals:
        delta['learning_goals'] = goals

    style_map = {
        '视频': '视觉型',
        '图': '视觉型',
        '动画': '视觉型',
        '听': '听觉型',
        '讲解': '听觉型',
        '代码': '动手型',
        '实践': '动手型',
        '项目': '动手型',
        '案例': '案例驱动型',
    }
    for keyword, style in style_map.items():
        if keyword in text:
            delta['cognitive_style'] = style
            delta.setdefault('learning_preferences', {})['preferred_mode'] = keyword
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
    for topic in ['Python', '机器学习', '深度学习', '线性代数', '概率论', '数据结构', '算法']:
        if topic.lower() in lowered:
            knowledge[topic] = knowledge.get('overall', '初级')
    # 'overall' 只是给上面各 topic 定级用的临时默认值，本身不是知识点，
    # 各消费方(复习队列/agents/展示)都会跳过它——干脆不写进画像，保持 knowledge_profile 干净
    knowledge.pop('overall', None)
    if knowledge:
        delta['knowledge_profile'] = knowledge

    hours_match = re.search(r'每周(?:能|可以)?(?:学|投入)?(\d{1,2})\s*小时', text)
    if hours_match:
        delta.setdefault('engagement', {})['weekly_hours'] = int(hours_match.group(1))
    return delta


def _item_text(it):
    """从一个条目取可比较文本：另一条写入路径存的是 dict(带 text/label 等)，这里存的是 str，
    统一抽成文本用于去重，避免两种形状混存导致去重失效、重复累积。"""
    if isinstance(it, dict):
        return str(it.get('text') or it.get('label') or it.get('misconception')
                   or it.get('goal') or it.get('title') or '').strip()
    return str(it or '').strip()


def _merge_unique(existing, additions, limit=20):
    merged = list(existing or [])
    seen = {t for t in (_item_text(x) for x in merged) if t}
    for item in additions or []:
        text = _item_text(item)
        if text and text not in seen:
            merged.append(text)
            seen.add(text)
        if len(merged) >= limit:
            break
    return merged[:limit]


def _build_quiz_delta(event):
    payload = event.payload if isinstance(event.payload, dict) else {}
    score = _bounded_score(payload.get('score'))
    knowledge_tags = _clean_text_list(payload.get('knowledge_tags') or payload.get('weak_areas') or [])
    review_recommendations = payload.get('review_recommendations') if isinstance(payload.get('review_recommendations'), list) else []
    wrong_tags = []
    for item in review_recommendations:
        if isinstance(item, dict):
            tag = item.get('knowledge_tag') or item.get('source_heading')
            if tag:
                wrong_tags.append(tag)
    # 只有 review_recommendations 里明确点名的才算"答错"；否则不把所有知识点都当错的
    wrong_tags = _clean_text_list(wrong_tags)
    wrong_set = set(wrong_tags)

    now_iso = timezone.now().isoformat()
    knowledge_profile = {}
    for tag in knowledge_tags:
        is_wrong = tag in wrong_set
        # 答错的知识点掌握度明显低于答对的，而不是给每个知识点写同一个整卷分
        tag_mastery = round(score * 0.55, 1) if is_wrong else round(score, 1)
        knowledge_profile[tag] = {
            'mastery_score': tag_mastery,
            'last_score': round(score, 1),
            'correct': not is_wrong,
            'source': 'material_quiz',
            'course_id': payload.get('course_id') or event.course_id,
            'material_id': payload.get('material_id') or event.material_id,
            'updated_at': now_iso,
        }

    delta = {
        'knowledge_profile': knowledge_profile,
        'engagement': {
            'last_quiz_score': round(score, 1),
            'last_quiz_at': now_iso,
        },
    }
    if score < 80 and wrong_tags:
        delta['misconceptions'] = wrong_tags
    return delta


def _build_chat_delta(event):
    payload = event.payload if isinstance(event.payload, dict) else {}
    delta = payload.get('profile_delta') if isinstance(payload.get('profile_delta'), dict) else {}
    normalized = dict(delta)
    engagement = normalized.get('engagement') if isinstance(normalized.get('engagement'), dict) else {}
    engagement.update({
        'last_chat_at': timezone.now().isoformat(),
        'last_chat_source': payload.get('conversation_id') or '',
    })
    normalized['engagement'] = engagement
    return normalized


def _build_activity_delta(event):
    payload = event.payload if isinstance(event.payload, dict) else {}
    delta = payload.get('profile_delta') if isinstance(payload.get('profile_delta'), dict) else {}
    normalized = dict(delta)
    engagement = normalized.get('engagement') if isinstance(normalized.get('engagement'), dict) else {}
    engagement.update({
        'last_activity_type': event.event_type,
        'last_activity_at': timezone.now().isoformat(),
    })
    if event.event_type == 'course_material_viewed':
        engagement['last_course_id'] = event.course_id
        engagement['last_material_id'] = event.material_id
    if event.event_type in {'learning_plan_generated', 'learning_plan_refreshed'}:
        engagement['last_plan_id'] = payload.get('plan_id')
        engagement['last_plan_title'] = payload.get('plan_title') or payload.get('title') or ''
    if event.event_type in {'weak_area_archived', 'weak_area_restored'}:
        tag = str(payload.get('knowledge_tag') or '').strip()
        if tag:
            engagement['last_weak_area_tag'] = tag
            if event.event_type == 'weak_area_archived':
                normalized['misconceptions'] = [tag]
    if event.event_type == 'material_quiz_generated':
        engagement['last_generated_quiz_material_id'] = event.material_id
        engagement['last_generated_quiz_count'] = payload.get('question_count') or 0
    if event.event_type == 'material_quiz_feedback':
        engagement['last_quiz_feedback'] = payload.get('feedback_type') or ''
    if event.event_type == 'outline_quiz_submitted':
        score = _bounded_score(payload.get('score'))
        engagement['last_outline_quiz_score'] = round(score, 1)
        engagement['last_outline_id'] = payload.get('outline_id')
        tags = _clean_text_list(payload.get('knowledge_tags') or [])
        if score < 80 and tags:
            normalized['misconceptions'] = tags
    normalized['engagement'] = engagement
    return normalized


def build_profile_delta(event):
    if event.event_type == 'material_quiz_submitted':
        return _build_quiz_delta(event)
    if event.event_type in {'conversation_message', 'course_ai_message'}:
        return _build_chat_delta(event)
    if event.event_type in {
        'course_material_viewed',
        'learning_plan_generated',
        'learning_plan_refreshed',
        'profile_building_answer',
        'weak_area_archived',
        'weak_area_restored',
        'material_quiz_generated',
        'material_quiz_feedback',
        'course_ai_opened',
        'outline_quiz_submitted',
    }:
        return _build_activity_delta(event)
    payload = event.payload if isinstance(event.payload, dict) else {}
    return payload.get('profile_delta') if isinstance(payload.get('profile_delta'), dict) else {}


def apply_profile_event(event):
    with transaction.atomic():
        locked_event = ProfileEvent.objects.select_for_update().get(pk=event.pk)
        if locked_event.processed_at:
            return locked_event

        profile, _ = StudentProfile.objects.select_for_update().get_or_create(user=locked_event.user)
        delta = build_profile_delta(locked_event)
        knowledge_profile = profile.knowledge_profile if isinstance(profile.knowledge_profile, dict) else {}
        incoming_knowledge = delta.get('knowledge_profile') if isinstance(delta.get('knowledge_profile'), dict) else {}
        knowledge_profile.update(incoming_knowledge)
        profile.knowledge_profile = knowledge_profile
        # 同步维护知识点时间戳，避免事件写入的知识点在复习队列里永远被当作"刚更新过"(days_elapsed=0)
        if incoming_knowledge:
            timestamps = profile.knowledge_timestamps if isinstance(profile.knowledge_timestamps, dict) else {}
            _now_iso = timezone.now().isoformat()
            for _tag in incoming_knowledge.keys():
                timestamps[_tag] = _now_iso
            profile.knowledge_timestamps = timestamps

        if delta.get('cognitive_style'):
            profile.cognitive_style = str(delta.get('cognitive_style')).strip()[:100]

        profile.learning_goals = _merge_unique(profile.learning_goals, delta.get('learning_goals'))
        profile.misconceptions = _merge_unique(profile.misconceptions, delta.get('misconceptions'))

        engagement = profile.engagement if isinstance(profile.engagement, dict) else {}
        incoming_engagement = delta.get('engagement') if isinstance(delta.get('engagement'), dict) else {}
        engagement.update(incoming_engagement)
        engagement['event_count'] = int(engagement.get('event_count') or 0) + 1
        if locked_event.event_type == 'material_quiz_submitted':
            engagement['quiz_event_count'] = int(engagement.get('quiz_event_count') or 0) + 1
        if locked_event.event_type in {'conversation_message', 'course_ai_message'}:
            engagement['chat_event_count'] = int(engagement.get('chat_event_count') or 0) + 1
        profile.engagement = engagement

        preferences = profile.learning_preferences if isinstance(profile.learning_preferences, dict) else {}
        incoming_preferences = delta.get('learning_preferences') or delta.get('preferences')
        if isinstance(incoming_preferences, dict):
            preferences.update(incoming_preferences)
        profile.learning_preferences = preferences
        profile.save()

        locked_event.profile_delta = delta
        locked_event.processed_at = timezone.now()
        locked_event.save(update_fields=['profile_delta', 'processed_at'])
        return locked_event


def record_profile_event(
    user,
    event_type,
    payload=None,
    *,
    source_app='',
    course_id=None,
    material_id=None,
    confidence=1.0,
    dedupe_key='',
    apply_now=True,
):
    if not user or not getattr(user, 'is_authenticated', False):
        return None

    if dedupe_key:
        existing = ProfileEvent.objects.filter(user=user, dedupe_key=dedupe_key).order_by('-created_at').first()
        if existing:
            return existing

    event = ProfileEvent.objects.create(
        user=user,
        event_type=event_type,
        source_app=source_app or '',
        course_id=course_id,
        material_id=material_id,
        confidence=confidence,
        payload=payload if isinstance(payload, dict) else {},
        dedupe_key=dedupe_key or '',
    )
    if apply_now:
        return apply_profile_event(event)
    return event
