from typing import Tuple


def classify_request(query: str) -> Tuple[str, float]:
    """
    简单请求分类器：将用户输入分类为 'roadmap' | 'courseware' | 'clarify'。
    返回 (label, confidence)。

    当前实现为轻量启发式规则，未来可替换为 LLM 分类（few-shot）。
    """
    if not query:
        return 'clarify', 0.0

    q = query.lower()
    roadmap_keys = ['路线', '路线图', '学习计划', 'roadmap', 'road map', 'road-map']
    courseware_keys = ['课件', 'ppt', '幻灯片', 'slides', '课件生成', '生成课件']

    for kw in courseware_keys:
        if kw in q:
            return 'courseware', 0.95

    for kw in roadmap_keys:
        if kw in q:
            return 'roadmap', 0.95

    # 问句或模糊请求倾向于澄清
    if q.strip().endswith('?') or q.strip().startswith(('如何', '怎么', '怎样', '能否')):
        return 'clarify', 0.6

    # 默认回退为 roadmap
    return 'roadmap', 0.5
