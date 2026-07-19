"""课程规划师代理（Planner Agent）

基于COGENT框架和Instructional Agents论文，负责生成课程大纲和学习路径。
"""
import json
from typing import Dict, List

from django.utils import timezone

from .services.xinghuo_client import XinghuoClient
from .services.safety import check_text, check_with_xinghuo, censor_text
from .curriculum_standards import CurriculumStandards


class PlannerAgent:
    """课程规划师代理：负责生成课程大纲和学习路径"""
    
    def __init__(self, user, grade_level: str = 'college'):
        self.user = user
        self.grade_level = grade_level
        self.client = XinghuoClient()
        self.standards_db = CurriculumStandards()
    
    def _safe_text(self, text: str) -> tuple:
        """安全检查文本"""
        try:
            meta = check_with_xinghuo(text)
        except Exception:
            meta = check_text(text)
        
        if not isinstance(meta, dict):
            meta = {'safe': True, 'labels': []}
        
        if not meta.get('safe', True):
            text = censor_text(text)
        
        return text, meta
    
    def generate_outline(self, topic: str, description: str = '', duration: int = 45) -> Dict:
        """生成课程大纲
        
        Args:
            topic: 课程主题
            description: 课程描述
            duration: 课时时长（分钟）
        
        Returns:
            课程大纲字典
        """
        standards = self.standards_db.query_standards(topic, self.grade_level)
        concepts = standards.get('concepts', [])
        objectives = standards.get('learning_objectives', [])
        prerequisites = standards.get('prerequisites', [])
        
        # 根据课时确定章节数量
        chapter_count = self._estimate_chapter_count(duration)
        
        prompt = self._build_outline_prompt(
            topic, description, concepts, objectives, prerequisites, chapter_count, duration
        )
        
        response = self.client.generate_text(prompt, max_tokens=2048)
        response, _ = self._safe_text(response)
        
        # 解析大纲
        outline = self._parse_outline_response(response, topic, standards)
        
        # 添加元数据
        outline['metadata'] = {
            'topic': topic,
            'grade_level': self.grade_level,
            'duration': duration,
            'chapter_count': len(outline.get('chapters', [])),
            'generated_at': str(timezone.now()),
            'standards_aligned': True,
        }
        
        return outline
    
    def _build_outline_prompt(self, topic: str, description: str, concepts: List[Dict], 
                             objectives: List[str], prerequisites: List[str],
                             chapter_count: int, duration: int) -> str:
        """构建大纲生成提示词"""
        concept_labels = [c.get('label', '') for c in concepts]
        concept_descriptions = [c.get('description', '') for c in concepts]
        
        # 只有当确实匹配到具体知识点时才作为参考给出；匹配不到就完全交给大模型按主题生成，
        # 避免把无关/通用概念硬塞进去导致大纲跑偏。
        if concept_labels:
            reference_block = (
                "【可参考的知识点（仅供参考，若与主题不符请忽略）】\n"
                f"- {', '.join(concept_labels)}\n"
            )
        else:
            reference_block = ''

        return f"""
你是一位经验丰富的课程规划专家，请为以下课程主题设计一份详细的教学大纲。

【基本信息】
- 主题：{topic}
- 课程描述：{description or '暂无'}
- 目标年级：{self.grade_level}
- 课时时长：{duration}分钟
- 章节数量：{chapter_count}章

{reference_block}
【最重要的要求】
- 大纲的章节标题、教学目标、核心概念都必须紧扣「{topic}」这一主题**本身的真实知识体系**。
- 严禁套用“理解理论基础与前沿发展 / 进行理论推导和证明 / 设计实验或项目 / 批判性评估研究成果 / 与其他领域融合”这类放之四海皆准的空话，也不要把与「{topic}」无关的学科内容（如无关的编程、机器学习等）塞进来。
- 各章标题要具体、专业、能看出这门课到底讲什么（例如讲“C++”应出现“指针与内存管理/类与对象/STL容器/模板”等，而不是“核心概念与关键方法”这种通用词）。

【设计要求】
1. 章节结构要符合“由浅入深、循序渐进”的逻辑
2. 每章内容要能在约{duration//max(1, chapter_count)}分钟内讲完
3. 每章要包含：明确的教学目标、该章真正的核心概念、具体案例或应用、课堂练习或互动
4. 使用“惊奇式学习设计”：每章开头设计一个能激发好奇心、且与本章内容直接相关的问题或场景

【输出格式】
请以JSON格式输出，包含以下字段：
{{
  "title": "课程标题",
  "description": "课程简介",
  "objectives": ["目标1", "目标2", ...],
  "chapters": [
    {{
      "number": 1,
      "title": "章节标题",
      "duration": "时长（分钟）",
      "teaching_goal": "本章教学目标",
      "core_concepts": ["核心概念1", "核心概念2"],
      "key_points": ["重点1", "重点2"],
      "curiosity_hook": "激发好奇心的开场问题或场景",
      "activities": ["活动1", "活动2"],
      "exercises": ["练习1"]
    }}
  ],
  "assessment": "评估方式"
}}

请直接输出JSON，不要额外说明。
"""
    
    def _estimate_chapter_count(self, duration: int) -> int:
        """估算章节数量"""
        if duration <= 30:
            return 2
        elif duration <= 60:
            return 3
        elif duration <= 90:
            return 4
        elif duration <= 120:
            return 5
        else:
            return min(6, max(3, duration // 30))
    
    def _parse_outline_response(self, response: str, topic: str, standards: Dict) -> Dict:
        """解析大纲响应"""
        # 尝试提取JSON
        try:
            outline = json.loads(response)
        except Exception:
            # 尝试从文本中提取JSON
            outline = None  # 先置空：否则当响应里没有 {...}(如占位/纯文本)时下面 if not outline 会 UnboundLocalError
            import re
            match = re.search(r'\{[\s\S]*\}', response)
            if match:
                try:
                    outline = json.loads(match.group(0))
                except Exception:
                    outline = None

            if not outline:
                # 返回默认结构
                outline = self._create_default_outline(topic, standards)

        # 确保结果是 dict 且必要字段存在（大模型可能返回 list/字符串等）
        if not isinstance(outline, dict):
            outline = self._create_default_outline(topic, standards)
        if 'chapters' not in outline:
            outline['chapters'] = []
        
        if 'objectives' not in outline:
            outline['objectives'] = standards.get('learning_objectives', [])
        
        return outline
    
    def _create_default_outline(self, topic: str, standards: Dict) -> Dict:
        """创建默认大纲"""
        concepts = standards.get('concepts', [])
        
        chapters = []
        for i, concept in enumerate(concepts[:5], 1):
            chapters.append({
                'number': i,
                'title': concept.get('label', f'第{i}章'),
                'duration': '15分钟',
                'teaching_goal': f'理解{concept.get("label", "核心概念")}',
                'core_concepts': concept.get('sub_concepts', [])[:3],
                'key_points': [concept.get('description', '')],
                'curiosity_hook': f'为什么{concept.get("label", "这个概念")}如此重要？',
                'activities': ['课堂讨论', '案例分析'],
                'exercises': ['思考题'],
            })
        
        return {
            'title': f'{topic}课程大纲',
            'description': f'系统学习{topic}的完整课程',
            'objectives': standards.get('learning_objectives', []),
            'chapters': chapters,
            'assessment': '课堂练习 + 章节测验',
        }
    
    def refine_outline(self, outline: Dict, feedback: str) -> Dict:
        """根据反馈优化大纲"""
        outline_text = json.dumps(outline, ensure_ascii=False)
        
        prompt = f"""
你是一位课程设计专家，请根据以下反馈优化课程大纲。

【原始大纲】
{outline_text}

【用户反馈】
{feedback}

【优化要求】
1. 仔细分析反馈意见
2. 针对性地调整大纲结构、内容或活动设计
3. 保持课程的整体逻辑和完整性
4. 直接输出优化后的JSON大纲

【输出】
优化后的课程大纲（JSON格式）
"""
        
        response = self.client.generate_text(prompt, max_tokens=2048)
        response, _ = self._safe_text(response)
        
        return self._parse_outline_response(response, outline.get('title', ''), {})
    
    def add_curiosity_hook(self, chapter: Dict, topic: str) -> Dict:
        """为章节添加"惊奇式学习设计"开场
        
        这是COGENT框架的核心特性之一，通过激发学生好奇心来提升学习效果。
        """
        prompt = f"""
你是一位教学设计专家，请为以下课程章节设计一个能激发学生好奇心的开场问题或场景。

【课程主题】{topic}
【章节标题】{chapter.get('title', '')}
【章节内容】{chapter.get('teaching_goal', '')}
【核心概念】{', '.join(chapter.get('core_concepts', []))}

【设计要求】
1. 设计一个与现实生活相关的问题或场景
2. 问题要有趣、能引发思考、让学生想知道答案
3. 问题要能自然引出本章的核心知识点
4. 避免直接告诉学生答案

【输出格式】
请以JSON格式输出：
{{
  "hook_type": "问题/场景/故事/数据",
  "hook_content": "具体的开场内容",
  "why_interesting": "为什么这个设计能激发好奇心"
}}

请直接输出JSON，不要额外说明。
"""
        
        response = self.client.generate_text(prompt, max_tokens=1024)
        response, _ = self._safe_text(response)
        
        try:
            hook = json.loads(response)
            chapter['curiosity_hook'] = hook.get('hook_content', chapter.get('curiosity_hook', ''))
            chapter['hook_type'] = hook.get('hook_type', '')
        except Exception:
            pass
        
        return chapter
    
    def generate_learning_path(self, topic: str, student_level: str = 'beginner') -> Dict:
        """生成个性化学习路径"""
        standards = self.standards_db.query_standards(topic, self.grade_level)
        concepts = standards.get('concepts', [])
        prerequisites = standards.get('prerequisites', [])
        
        prompt = f"""
请为课程"{topic}"设计一条个性化学习路径。

【基本信息】
- 主题：{topic}
- 学生水平：{student_level}
- 目标年级：{self.grade_level}

【知识点体系】
- 核心概念：{[c.get('label', '') for c in concepts]}
- 前置知识：{prerequisites}

【设计要求】
1. 根据学生水平调整内容深度和节奏
2. 遵循循序渐进的原则
3. 每个阶段要有明确的学习目标
4. 提供多种学习资源推荐
5. 包含阶段性测试点

【输出格式】
JSON格式：
{{
  "path_title": "学习路径标题",
  "total_duration": "总时长",
  "levels": [
    {{
      "level": "入门",
      "duration": "时长",
      "objectives": ["目标"],
      "topics": ["知识点"],
      "resources": ["推荐资源"],
      "checkpoint": "阶段测试"
    }}
  ]
}}
"""
        
        response = self.client.generate_text(prompt, max_tokens=2048)
        response, _ = self._safe_text(response)
        
        try:
            return json.loads(response)
        except Exception:
            return {'path_title': f'{topic}学习路径', 'levels': []}
