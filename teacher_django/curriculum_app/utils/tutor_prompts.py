import json

ROADMAP_SCHEMA = {
    "type": "object",
    "required": ["title", "modules"],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "modules": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "estimated_hours": {"type": "number"},
                    "lessons": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["title"],
                            "properties": {
                                "title": {"type": "string"},
                                "objectives": {"type": "string"},
                                "resources": {"type": "array", "items": {"type": "string"}}
                            }
                        }
                    }
                }
            }
        }
    }
}

COURSEWARE_SCHEMA = {
    "type": "object",
    "required": ["title", "slides"],
    "properties": {
        "title": {"type": "string"},
        "slides": {"type": "array"}
    }
}


ROADMAP_PROMPT = (
    "请严格输出 JSON，遵守以下 schema（不要包含额外文本）：\n"
    + json.dumps(ROADMAP_SCHEMA, ensure_ascii=False)
    + "\n\n用户请求：{query}\n\n输出示例必须包含 title 与 modules 列表，每个 module 包含 name 与 lessons 列表。"
)


COURSEWARE_PROMPT = (
    "请严格输出 JSON，遵守以下 schema（不要包含额外文本）：\n"
    + json.dumps(COURSEWARE_SCHEMA, ensure_ascii=False)
    + "\n\n用户请求：{query}\n\n输出示例必须包含 title 与 slides（slides 为字符串数组或 HTML 数组）。"
)


def build_roadmap_prompt(query: str) -> str:
    return ROADMAP_PROMPT.format(query=query)


def build_courseware_prompt(query: str) -> str:
    return COURSEWARE_PROMPT.format(query=query)
