import hashlib
import json
import logging

from django.utils import timezone

from core.xunfei_spark import spark_client
from .models import ProfileSnapshot

logger = logging.getLogger(__name__)

SNAPSHOT_HISTORY_LIMIT = 10

SUGGESTION_MAP = {
    '知识掌握': '可以挑一个还没掌握牢的知识点，找几道练习题巩固一下。',
    '学习参与度': '尝试更规律地安排学习时间，哪怕每天只学一点也能积累参与度。',
    '学习目标': '给自己设定一个具体的小目标，明确接下来想学会什么。',
    '学习偏好': '尝试探索一种新的学习方式，比如多做练习或多和同学讨论。',
    '概念清晰度': '把容易混淆的概念找出来，重新梳理一下定义和区别。',
    '画像置信度': '多和AI聊聊你的学习情况，能帮助系统更准确地了解你。',
}


def _normalize_knowledge_score(value):
    """将知识掌握度的不同存储格式统一转换为0-100的分数。

    knowledge_profile 的取值可能是：
    - 0~1 的浮点数（掌握度），如 0.5
    - 字符串等级，如 '初级'/'中级'/'高级'
    - 结构化字典，如 {'mastery_score': 85}
    """
    if isinstance(value, dict):
        try:
            score = float(value.get('mastery_score'))
        except (TypeError, ValueError):
            return None
        return max(0.0, min(100.0, score))
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        score = float(value)
        if score <= 1:
            score *= 100
        return max(0.0, min(100.0, score))
    if isinstance(value, str):
        return {'高级': 95.0, '中级': 65.0, '初级': 30.0}.get(value)
    return None


def _compute_profile_hash(profile, radar_values=None, knowledge_snapshot=None):
    """对画像内容做哈希，用于判断画像自上次快照后是否发生变化。
    必须纳入雷达值与知识快照——它们已并入做题画像(A)，否则做题带来的知识变化(A 变、B 不变)
    会因哈希不变而永远不生成新快照，成长报告/雷达"上次得分"反映不出做题进步。"""
    content = (profile.profile_data or '') + '|' + (profile.confidence_scores or '')
    content += '|' + json.dumps(radar_values or [], ensure_ascii=False, sort_keys=True)
    content += '|' + json.dumps(knowledge_snapshot or {}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _build_knowledge_snapshot_dict(knowledge_items):
    """将知识点列表转换为 {知识点: 归一化分数} 字典，跳过内部键和无法解析的值。"""
    snapshot = {}
    for k, v in knowledge_items:
        if k == 'overall' or str(k).startswith('__'):
            continue
        score = _normalize_knowledge_score(v)
        if score is not None:
            snapshot[k] = score
    return snapshot


def _get_or_create_snapshot(user, course_id, profile, radar_chart_data, knowledge_items):
    """获取或创建当前画像的快照。

    返回 (latest_snapshot, previous_snapshot_or_None, created)：
    - 若最近一条快照的 profile_hash 与当前画像一致，不创建新快照，
      previous 取倒数第二条（可能为 None）。
    - 否则创建新快照，previous 取创建前的最新一条（可能为 None）。
    """
    knowledge_snapshot = _build_knowledge_snapshot_dict(knowledge_items)
    current_hash = _compute_profile_hash(profile, radar_chart_data.get('values'), knowledge_snapshot)
    recent = list(
        ProfileSnapshot.objects.filter(user=user, course_id=course_id).order_by('-created_at')[:2]
    )

    if recent and recent[0].profile_hash == current_hash:
        previous = recent[1] if len(recent) > 1 else None
        return recent[0], previous, False

    previous = recent[0] if recent else None
    new_snapshot = ProfileSnapshot.objects.create(
        user=user,
        course_id=course_id,
        radar_labels=json.dumps(radar_chart_data['labels'], ensure_ascii=False),
        radar_values=json.dumps(radar_chart_data['values'], ensure_ascii=False),
        knowledge_snapshot=json.dumps(knowledge_snapshot, ensure_ascii=False),
        profile_hash=current_hash,
        ai_narrative='',
    )
    return new_snapshot, previous, True


def _compute_deltas(latest_values, previous_values, labels):
    """逐维度计算最新值与上次值的差异，并归类为 up/down/flat。"""
    deltas = []
    for label, latest, previous in zip(labels, latest_values, previous_values):
        delta = round(latest - previous, 1)
        if delta > 1:
            direction = 'up'
        elif delta < -1:
            direction = 'down'
        else:
            direction = 'flat'
        deltas.append({
            'label': label,
            'latest': latest,
            'previous': previous,
            'delta': delta,
            'delta_abs': abs(delta),
            'direction': direction,
        })
    return deltas


def _build_trend_series(snapshots, labels):
    """将按时间正序排列的快照列表转换为趋势折线图所需的数据结构。"""
    dates = []
    series = [{'label': label, 'data': []} for label in labels]

    for snap in snapshots:
        dates.append(timezone.localtime(snap.created_at).strftime('%Y-%m-%d'))
        try:
            values = json.loads(snap.radar_values)
        except Exception:
            values = [0] * len(labels)
        for idx, item in enumerate(series):
            item['data'].append(values[idx] if idx < len(values) else 0)

    return {'dates': dates, 'series': series}


_DIRECTION_LABELS = {'up': '上升', 'down': '下降', 'flat': '基本持平'}


def _compute_knowledge_deltas(latest_snapshot, previous_snapshot, top_n=2, min_delta=1.0):
    """对比前后两次知识点掌握度快照，返回变化幅度最大的知识点（按|变化|降序，最多top_n个）。"""
    try:
        latest_map = json.loads(latest_snapshot.knowledge_snapshot or '{}')
        previous_map = json.loads(previous_snapshot.knowledge_snapshot or '{}')
    except (TypeError, ValueError):
        return []

    deltas = []
    for tag, latest_score in latest_map.items():
        if tag not in previous_map:
            continue
        previous_score = previous_map[tag]
        delta = round(latest_score - previous_score, 1)
        if abs(delta) < min_delta:
            continue
        deltas.append({'tag': tag, 'previous': previous_score, 'latest': latest_score, 'delta': delta})

    deltas.sort(key=lambda d: abs(d['delta']), reverse=True)
    return deltas[:top_n]


def _truncate_narrative(text, limit=100):
    """将AI生成的文案截断到约limit个字符，优先在句末标点处截断，避免半句话。"""
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    for punct in ('。', '！', '？', '；', '，'):
        idx = truncated.rfind(punct)
        if idx >= limit // 2:
            return truncated[:idx + 1]
    return truncated


def _fallback_narrative(best, worst):
    """AI不可用或调用失败时的兜底诊断文案。"""
    if best['direction'] == 'flat' and worst['direction'] == 'flat':
        return '最近各项指标变化不大，继续保持当前的学习节奏，可以给自己定一个新的小目标。'

    if best['direction'] != 'up' and worst['direction'] != 'up':
        return (
            f"最近的「{worst['label']}」有所下滑，先别担心。"
            f"{SUGGESTION_MAP.get(worst['label'], '')}"
        )

    if worst['direction'] != 'down':
        return (
            f"最近「{best['label']}」进步明显，继续保持！"
            f"{SUGGESTION_MAP.get(best['label'], '')}"
        )

    return (
        f"最近「{best['label']}」进步明显，但「{worst['label']}」有所下滑。"
        f"{SUGGESTION_MAP.get(worst['label'], '')}"
    )


def _generate_growth_narrative(deltas, knowledge_deltas, misconceptions_list, learning_goals_list):
    """基于本次成长数据生成一段AI诊断文字，失败时回退到模板文案。"""
    sorted_deltas = sorted(deltas, key=lambda d: d['delta'])
    worst = sorted_deltas[0]
    best = sorted_deltas[-1]

    if spark_client:
        try:
            delta_lines = '\n'.join(
                f"- {d['label']}: 从{d['previous']}到{d['latest']}"
                f"（变化{d['delta']:+.1f}，{_DIRECTION_LABELS[d['direction']]}）"
                for d in deltas
            )
            has_up = any(d['direction'] == 'up' for d in deltas)
            has_down = any(d['direction'] == 'down' for d in deltas)

            requirements = []
            if has_up:
                requirements.append('必须提到标注为「上升」的维度中变化最大的一个，给予肯定')
            else:
                requirements.append('本次没有维度标注为「上升」，不要声称学生有进步，可客观说明整体保持稳定')

            if has_down:
                requirements.append('必须提到标注为「下降」的维度中变化最大的一个，给出安慰和具体建议')
            else:
                requirements.append('本次没有维度标注为「下降」，不要声称学生有退步或停滞')

            knowledge_block = ''
            if knowledge_deltas:
                knowledge_lines = '\n'.join(
                    f"- {kd['tag']}: 从{kd['previous']}到{kd['latest']}（变化{kd['delta']:+.1f}）"
                    for kd in knowledge_deltas
                )
                knowledge_block = f"\n具体知识点掌握度变化：\n{knowledge_lines}"
                requirements.append('如果提供了具体知识点掌握度变化数据，挑其中变化最大的1个知识点，在反馈中提到它的名称，让建议更具体')

            requirements.append('给出一条具体可执行的学习建议')
            requirements.append('全文不超过100字')
            requirements.append('不要使用markdown格式，输出纯文本')

            requirement_lines = '\n'.join(f'{i + 1}. {text}' for i, text in enumerate(requirements))

            messages = [
                {
                    'role': 'system',
                    'content': (
                        "你是一位关注学生成长的助教，正在根据学生最近一次学习画像的变化撰写简短反馈。\n"
                        "每个维度后面已标注「上升」/「下降」/「基本持平」，请严格依据该标注描述变化，不要凭空判断或编造方向。\n"
                        "要求：\n"
                        f"{requirement_lines}"
                    )
                },
                {
                    'role': 'user',
                    'content': (
                        f"学生六维画像各项变化如下：\n{delta_lines}\n"
                        f"常见误解：{misconceptions_list or '无'}\n"
                        f"学习目标：{learning_goals_list or '无'}"
                        f"{knowledge_block}\n"
                        "请据此写一段成长反馈。"
                    )
                },
            ]
            narrative = spark_client.get_response(messages)
            if narrative and narrative.strip():
                return _truncate_narrative(narrative)
        except Exception:
            logger.exception('生成学习成长AI诊断失败')

    return _fallback_narrative(best, worst)


def build_growth_report(user, course_id, profile, radar_chart_data, knowledge_items,
                         misconceptions_list, learning_goals_list):
    """构建学习成长报告所需的全部上下文数据。"""
    latest, previous, created = _get_or_create_snapshot(
        user, course_id, profile, radar_chart_data, knowledge_items,
    )

    empty_report = {
        'has_growth_history': False,
        'growth_deltas': [],
        'growth_trend': None,
        'growth_narrative': '',
        'latest_snapshot_at': latest.created_at,
        'previous_snapshot_at': None,
        'radar_previous_values': None,
    }

    if previous is None:
        return empty_report

    labels = radar_chart_data['labels']
    latest_values = json.loads(latest.radar_values)
    previous_values = json.loads(previous.radar_values)

    deltas = _compute_deltas(latest_values, previous_values, labels)

    history = list(
        ProfileSnapshot.objects.filter(user=user, course_id=course_id).order_by('created_at')[:SNAPSHOT_HISTORY_LIMIT]
    )
    growth_trend = _build_trend_series(history, labels)

    if created:
        knowledge_deltas = _compute_knowledge_deltas(latest, previous)
        narrative = _generate_growth_narrative(deltas, knowledge_deltas, misconceptions_list, learning_goals_list)
        latest.ai_narrative = narrative
        latest.save(update_fields=['ai_narrative'])
    else:
        narrative = latest.ai_narrative

    return {
        'has_growth_history': True,
        'growth_deltas': deltas,
        'growth_trend': growth_trend,
        'growth_narrative': narrative,
        'latest_snapshot_at': latest.created_at,
        'previous_snapshot_at': previous.created_at,
        'radar_previous_values': previous_values,
    }
