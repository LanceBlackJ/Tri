"""学生画像构建器：基于自然语言输入调用讯飞星火生成结构化画像（JSON）。

注意：讯飞生成的内容需要严格解析与安全检查；该模块提供解析、降级与合并到 StudentProfile 的功能。
"""
from __future__ import annotations

import json
import re
import logging
from typing import Optional

from .xinghuo_client import XinghuoClient
from .safety import check_text, censor_text, check_with_xinghuo
from django.conf import settings

logger = logging.getLogger(__name__)


class ProfileBuilder:
    def __init__(self, client: Optional[XinghuoClient] = None):
        self.client = client or XinghuoClient()

    def build_from_text(self, text: str) -> dict:
        """调用大模型从自然语言文本中提取画像，返回字典（可能部分字段缺失）。"""
        prompt = (
            "请从下面的学生自述或对话中抽取学生画像，并只返回一个 JSON 对象，包含以下字段：\n"
            "- knowledge_profile: 一个对象，键为知识点或主题，值为掌握程度（例如 '初级','中级','高级' 或 0-100）\n"
            "- cognitive_style: 字符串，认知风格（视觉型/听觉型/动手型/混合等）\n"
            "- learning_goals: 数组，学习目标的简短条目\n"
            "- misconceptions: 数组，学生的易错点或误区\n"
            "- engagement: 对象，包含 'score' (0-100) 和 'notes' 字段\n"
            "- preferences: 对象，学习偏好（例如偏好视频/文本/实践）\n"
            "请确保输出合法的 JSON，不要额外说明。\n\n"
            f"文本:\n{text}\n\nJSON:" 
        )
        resp = self.client.generate_text(prompt)
        safe = None
        # 优先使用讯飞合规接口（若开启）进行检查
        try:
            if getattr(settings, 'XINGHUO_SAFETY_ENABLED', False):
                safe = check_with_xinghuo(resp)
        except Exception:
            safe = None
        if not safe:
            safe = check_text(resp)
        if not safe.get('safe', True):
            logger.warning('ProfileBuilder: 生成结果包含敏感词: %s', safe.get('found') or safe.get('labels'))
            resp = censor_text(resp)
        parsed = self._extract_json(resp)
        if parsed is None:
            logger.warning('ProfileBuilder: 无法解析模型返回为 JSON，降级保存原始文本')
            return {'raw_text': text}
        # 基本类型规范化
        norm = {}
        if 'knowledge_profile' in parsed and isinstance(parsed['knowledge_profile'], dict):
            norm['knowledge_profile'] = parsed['knowledge_profile']
        if 'cognitive_style' in parsed:
            norm['cognitive_style'] = str(parsed['cognitive_style'])
        if 'learning_goals' in parsed and isinstance(parsed['learning_goals'], list):
            norm['learning_goals'] = parsed['learning_goals']
        if 'misconceptions' in parsed and isinstance(parsed['misconceptions'], list):
            norm['misconceptions'] = parsed['misconceptions']
        if 'engagement' in parsed and isinstance(parsed['engagement'], dict):
            norm['engagement'] = parsed['engagement']
        if 'preferences' in parsed and isinstance(parsed['preferences'], dict):
            norm['preferences'] = parsed['preferences']
        return norm

    def _extract_json(self, text: str) -> Optional[dict]:
        # 尝试直接解析
        try:
            return json.loads(text)
        except Exception:
            pass
        # 提取第一个 JSON 对象块
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            s = m.group(0)
            try:
                return json.loads(s)
            except Exception:
                # 尝试用单引号替换为双引号
                try:
                    s2 = s.replace("'", '"')
                    return json.loads(s2)
                except Exception:
                    return None
        return None
