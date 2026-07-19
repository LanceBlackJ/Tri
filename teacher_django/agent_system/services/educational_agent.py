"""
教育智能体集成模块 - 基于三篇论文的完整实现

整合以下技术：
1. USER-LLM R1 (arXiv:XXXX) - 动态画像推理
2. 情感智能+记忆架构 (arXiv:2505.19803v2) - 情感识别、Engagement Vector
3. LLM Agents for Education (arXiv:2503.11733v2) - 知识追踪、错题检测

核心功能：
- 多模态情感响应
- 自适应学习路径
- 知识追踪与预测
- 个性化反馈生成
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class LearningMode(Enum):
    """
    学习模式
    """
    TUTORING = "tutoring"           # 辅导模式
    PRACTICE = "practice"           # 练习模式
    EXPLORATION = "exploration"     # 探索模式
    REVIEW = "review"              # 复习模式
    ASSESSMENT = "assessment"       # 评估模式


@dataclass
class UserLearningProfile:
    """
    用户学习画像
    
    整合所有画像信息
    """
    user_id: int
    
    # 基本信息
    knowledge_profile: Dict[str, float] = field(default_factory=dict)  # 知识掌握度
    cognitive_style: str = ""  # 认知风格
    learning_goals: List[str] = field(default_factory=list)  # 学习目标
    misconceptions: List[str] = field(default_factory=list)  # 误解概念
    preferences: Dict = field(default_factory=dict)  # 学习偏好
    
    # 情感状态
    current_emotion: str = "engaged"
    emotion_history: List[Dict] = field(default_factory=list)
    engagement_level: float = 0.5
    
    # 学习状态
    current_mode: LearningMode = LearningMode.TUTORING
    active_concepts: List[str] = field(default_factory=list)
    weak_areas: List[str] = field(default_factory=list)
    
    # 统计
    total_sessions: int = 0
    total_interactions: int = 0
    average_mastery: float = 0.0
    
    def to_dict(self) -> Dict:
        return {
            'user_id': self.user_id,
            'knowledge_profile': self.knowledge_profile,
            'cognitive_style': self.cognitive_style,
            'learning_goals': self.learning_goals,
            'misconceptions': self.misconceptions,
            'preferences': self.preferences,
            'current_emotion': self.current_emotion,
            'engagement_level': self.engagement_level,
            'current_mode': self.current_mode.value,
            'active_concepts': self.active_concepts,
            'weak_areas': self.weak_areas,
            'total_sessions': self.total_sessions,
            'total_interactions': self.total_interactions,
            'average_mastery': self.average_mastery
        }


class EducationalAgent:
    """
    教育智能体 - 论文核心实现
    
    整合情感智能、记忆架构、知识追踪和错题检测
    提供完整的自适应学习体验
    """
    
    def __init__(self, llm_client=None):
        """
        初始化教育智能体
        
        Args:
            llm_client: LLM客户端
        """
        self.llm_client = llm_client
        
        # 导入并初始化各模块
        from .emotion_recognition import (
            get_emotion_engine,
            get_empathy_generator
        )
        from .engagement_vector import get_engagement_engine
        from .memory_architecture import get_memory_architecture
        from .knowledge_tracing import get_knowledge_tracing_engine
        from .error_correction import get_error_correction_engine
        
        # 初始化引擎
        self.emotion_engine = get_emotion_engine(llm_client)
        self.empathy_generator = get_empathy_generator(llm_client)
        self.engagement_engine = get_engagement_engine()
        self.memory_architecture = get_memory_architecture()
        self.knowledge_tracing = get_knowledge_tracing_engine()
        self.error_correction = get_error_correction_engine(llm_client)
        
        # 用户画像缓存
        self.user_profiles: Dict[int, UserLearningProfile] = {}
        
        # 会话信息
        self.active_sessions: Dict[str, Dict] = {}
    
    def process_learning_interaction(
        self,
        user_id: int,
        session_id: str,
        user_input: str,
        context: Optional[Dict] = None,
        interaction_type: str = "chat"
    ) -> Dict:
        """
        处理学习交互
        
        主要方法，整合所有模块的功能
        
        Args:
            user_id: 用户ID
            session_id: 会话ID
            user_input: 用户输入
            context: 上下文信息
            interaction_type: 交互类型 (chat/practice/assessment)
            
        Returns:
            完整的处理结果
        """
        context = context or {}
        
        # 1. 获取用户画像
        profile = self._get_or_create_profile(user_id)
        
        # 2. 情感识别
        emotion_result = self.emotion_engine.recognize_emotion(
            user_id=user_id,
            text=user_input,
            context={
                'session_id': session_id,
                'behavior_data': context.get('behavior_data'),
                **context
            }
        )
        
        # 3. 参与度评估
        engagement_vector = self.engagement_engine.calculate_engagement_vector(
            user_id=user_id,
            interaction_data=context.get('interaction_data', {}),
            task_data=context.get('task_data', {}),
            emotion_history=profile.emotion_history,
            current_emotion=emotion_result.to_dict(),
            context={
                'time_on_task': context.get('time_on_task', 5),
                'user_responses': context.get('user_responses', [])
            }
        )
        
        # 4. 更新用户画像
        profile.current_emotion = emotion_result.state.value
        profile.engagement_level = engagement_vector.total
        profile.emotion_history.append(emotion_result.to_dict())
        
        # 5. 记忆更新
        self.memory_architecture.store_interaction(
            user_id=user_id,
            session_id=session_id,
            interaction_type=interaction_type,
            content=user_input,
            concepts=context.get('concepts', []),
            emotions=[emotion_result.state.value],
            importance=self._determine_importance(emotion_result)
        )
        
        # 6. 知识追踪（如果有练习）
        knowledge_updates = {}
        if context.get('practice_result'):
            practice = context['practice_result']
            concept_id = practice.get('concept_id')
            
            if concept_id:
                record = self.knowledge_tracing.record_interaction(
                    user_id=user_id,
                    concept_id=concept_id,
                    course_id=context.get('course_id', 1),
                    interaction_data={
                        'correct': practice.get('correct', False),
                        'response_time': practice.get('response_time', 30),
                        'hints_used': practice.get('hints_used', 0)
                    }
                )
                knowledge_updates = record.to_dict()
        
        # 7. 生成同理心响应
        empathy_strategy = self.empathy_generator.select_optimal_strategy(
            emotion_result,
            context
        )
        
        # 8. 生成上下文提示（给LLM）
        context_prompt = self.memory_architecture.build_context_prompt(
            user_id=user_id,
            session_id=session_id
        )
        
        # 9. 构建完整响应
        response = {
            'user_input': user_input,
            'emotion_analysis': {
                'detected_emotion': emotion_result.to_dict(),
                'emotion_context': self.emotion_engine.analyze_emotional_context(
                    user_id, context
                ),
                'empathy_strategy': empathy_strategy.value
            },
            'engagement_analysis': engagement_vector.to_dict(),
            'knowledge_updates': knowledge_updates,
            'context_for_llm': {
                'user_profile': profile.to_dict(),
                'memory_context': context_prompt,
                'emotion_aware_instruction': self._build_emotion_instruction(emotion_result)
            },
            'timestamp': datetime.now().isoformat()
        }
        
        return response
    
    def _get_or_create_profile(self, user_id: int) -> UserLearningProfile:
        """获取或创建用户画像"""
        if user_id not in self.user_profiles:
            self.user_profiles[user_id] = UserLearningProfile(user_id=user_id)
        return self.user_profiles[user_id]
    
    def _determine_importance(self, emotion_result):
        """确定交互重要性"""
        from .memory_architecture import MemoryImportance
        
        if emotion_result.intensity > 0.7:
            if emotion_result.state.value in ['frustrated', 'anxious', 'confused']:
                return MemoryImportance.HIGH
            return MemoryImportance.MEDIUM
        return MemoryImportance.LOW
    
    def _build_emotion_instruction(self, emotion_result) -> str:
        """构建情感感知指令"""
        instructions = {
            'confused': "用户似乎对某些内容感到困惑，请使用更清晰、更耐心的解释方式。",
            'frustrated': "用户可能感到沮丧，请给予更多鼓励和支持，使用温和的语气。",
            'excited': "用户对学习内容表现出热情，可以适当增加挑战性内容。",
            'anxious': "用户可能感到焦虑，请使用安抚的语气，提供明确的指导。",
            'bored': "用户可能感到无聊，请尝试使用更有趣的方式来解释。",
            'confident': "用户表现得很自信，可以提供更具挑战性的问题。"
        }
        
        state = emotion_result.state.value
        instruction = instructions.get(state, "")
        
        if instruction:
            return f"[情感感知] {instruction}"
        return ""
    
    def generate_personalized_response(
        self,
        user_id: int,
        base_response: str,
        emotion_analysis: Dict,
        engagement_analysis: Dict
    ) -> str:
        """
        生成个性化响应
        
        在基础响应上添加同理心元素
        
        Args:
            user_id: 用户ID
            base_response: 基础响应
            emotion_analysis: 情感分析结果
            engagement_analysis: 参与度分析结果
            
        Returns:
            个性化响应
        """
        emotion_data = emotion_analysis.get('detected_emotion')
        
        if not emotion_data:
            return base_response
        
        # 导入情感数据类
        from .emotion_recognition import EmotionData, EmotionalState, EmpathyStrategy
        
        # 重建情感数据
        emotion = EmotionData(
            emotion_type=emotion_data.get('emotion_type', 'joy'),
            state=EmotionalState(emotion_data.get('state', 'engaged')),
            intensity=emotion_data.get('intensity', 0.5),
            confidence=emotion_data.get('confidence', 0.5)
        )
        
        # 获取同理心响应
        strategy = EmpathyStrategy(emotion_analysis.get('empathy_strategy', 'reactive'))
        
        empathetic_intro = self.empathy_generator.generate_empathetic_response(
            emotion=emotion,
            strategy=strategy
        )
        
        # 根据参与度调整响应长度
        engagement_total = engagement_analysis.get('total', 0.5)
        
        if engagement_total < 0.3:
            # 低参与度：缩短响应，提供更多引导
            if empathetic_intro:
                return f"{empathetic_intro}\n\n{base_response}"
        elif engagement_total > 0.7:
            # 高参与度：可以提供更详细的解释
            pass
        
        # 根据情感状态添加后续建议
        suggestions = []
        
        if emotion.state.value in ['confused', 'frustrated']:
            suggestions.append("\n\n需要我换一种方式解释吗？")
        elif emotion.state.value == 'anxious':
            suggestions.append("\n\n我们慢慢来，不要着急。")
        
        if suggestions:
            return f"{empathetic_intro} {base_response}{suggestions[0]}"
        
        return f"{empathetic_intro} {base_response}" if empathetic_intro else base_response
    
    def process_practice_answer(
        self,
        user_id: int,
        session_id: str,
        question: str,
        user_answer: str,
        expected_answer: str,
        concept_id: str,
        course_id: int,
        context: Optional[Dict] = None
    ) -> Dict:
        """
        处理练习答案
        
        整合错题检测和知识追踪
        
        Args:
            user_id: 用户ID
            session_id: 会话ID
            question: 问题
            user_answer: 用户答案
            expected_answer: 期望答案
            concept_id: 知识点ID
            course_id: 课程ID
            context: 上下文
            
        Returns:
            处理结果
        """
        context = context or {}
        
        # 1. 获取用户画像
        profile = self._get_or_create_profile(user_id)
        
        # 2. 情感识别
        emotion_result = self.emotion_engine.recognize_emotion(
            user_id=user_id,
            text=user_answer,
            context={'session_id': session_id}
        )
        
        # 3. 错题检测与纠正
        feedback = self.error_correction.detect_and_correct(
            user_id=user_id,
            user_answer=user_answer,
            expected_answer=expected_answer,
            question=question,
            context={'concept_id': concept_id},
            user_profile=profile.to_dict()
        )
        
        # 4. 知识追踪更新
        correct = user_answer.strip() == expected_answer.strip()
        record = self.knowledge_tracing.record_interaction(
            user_id=user_id,
            concept_id=concept_id,
            course_id=course_id,
            interaction_data={
                'correct': correct,
                'response_time': context.get('response_time', 30),
                'hints_used': context.get('hints_used', 0)
            }
        )
        
        # 5. 更新画像
        if not correct:
            if concept_id not in profile.weak_areas:
                profile.weak_areas.append(concept_id)
        else:
            if concept_id in profile.weak_areas:
                profile.weak_areas.remove(concept_id)
        
        # 6. 记忆更新
        self.memory_architecture.store_interaction(
            user_id=user_id,
            session_id=session_id,
            interaction_type='practice',
            content=f"练习：{question}",
            concepts=[concept_id],
            emotions=[emotion_result.state.value],
            importance=self._determine_importance(emotion_result)
        )
        
        # 7. 生成响应
        personalized_feedback = self.generate_personalized_response(
            user_id=user_id,
            base_response=feedback.to_user_message(),
            emotion_analysis={
                'detected_emotion': emotion_result.to_dict(),
                'empathy_strategy': 'reactive'
            },
            engagement_analysis={'total': profile.engagement_level}
        )
        
        return {
            'is_correct': correct,
            'feedback': feedback.to_dict(),
            'personalized_message': personalized_feedback,
            'knowledge_update': record.to_dict(),
            'emotion': emotion_result.to_dict(),
            'suggestions': self._generate_learning_suggestions(
                profile, record, feedback
            )
        }
    
    def _generate_learning_suggestions(
        self,
        profile: UserLearningProfile,
        knowledge_record,
        error_feedback
    ) -> List[str]:
        """
        生成学习建议
        """
        suggestions = []
        
        # 基于薄弱知识点
        if profile.weak_areas:
            suggestions.append(f"建议复习以下知识点：{', '.join(profile.weak_areas[:3])}")
        
        # 基于错误反馈
        if error_feedback.related_concepts_to_review:
            suggestions.append(f"相关概念：{', '.join(error_feedback.related_concepts_to_review[:3])}")
        
        # 基于掌握度
        if knowledge_record.mastery_level < 0.6:
            suggestions.append("这个知识点还需要更多练习")
        
        return suggestions
    
    def get_learning_report(
        self,
        user_id: int
    ) -> Dict:
        """
        获取学习报告
        
        Args:
            user_id: 用户ID
            
        Returns:
            完整的学习报告
        """
        # 获取画像
        profile = self._get_or_create_profile(user_id)
        
        # 知识追踪报告
        knowledge_report = self.knowledge_tracing.get_user_knowledge_profile(user_id)
        
        # 错误分析报告
        error_report = self.error_correction.get_error_analysis(user_id)
        
        # 情感趋势
        emotion_trend = self.emotion_engine.get_emotion_trend(user_id)
        
        # 参与度趋势
        engagement_summary = {
            'current_level': profile.engagement_level,
            'emotional_state': profile.current_emotion,
            'total_interactions': profile.total_interactions
        }
        
        # 记忆摘要
        memory_summary = self.memory_architecture.get_learning_history(user_id)
        
        return {
            'user_id': user_id,
            'profile': profile.to_dict(),
            'knowledge_summary': knowledge_report,
            'error_summary': error_report,
            'emotion_trend': emotion_trend,
            'engagement_summary': engagement_summary,
            'memory_summary': memory_summary,
            'recommendations': self._generate_recommendations(profile, knowledge_report, error_report)
        }
    
    def _generate_recommendations(
        self,
        profile: UserLearningProfile,
        knowledge_report: Dict,
        error_report: Dict
    ) -> List[str]:
        """
        生成综合建议
        """
        recommendations = []
        
        # 基于掌握度
        avg_mastery = knowledge_report.get('summary', {}).get('average_mastery', 0)
        if avg_mastery < 0.5:
            recommendations.append("建议花更多时间巩固基础知识")
        elif avg_mastery > 0.8:
            recommendations.append("可以尝试更具挑战性的内容")
        
        # 基于错误模式
        common_errors = error_report.get('most_common_errors', [])
        if common_errors:
            recommendations.append(f"需要重点关注：{common_errors[0]['error_type']}")
        
        # 基于参与度
        if profile.engagement_level < 0.4:
            recommendations.append("尝试不同的学习方式来提高参与度")
        
        # 基于情感状态
        if profile.current_emotion in ['frustrated', 'bored']:
            recommendations.append("当前状态建议调整学习节奏或内容")
        
        return recommendations


# 尝试导入Enum
try:
    from enum import Enum
except ImportError:
    # Python 2.7兼容
    def Enum(name, values):
        class EnumClass(object):
            def __init__(self):
                for n, v in values:
                    setattr(self, n, v)
        return EnumClass()


# 全局教育智能体实例
_educational_agent: Optional[EducationalAgent] = None


def get_educational_agent(llm_client=None) -> EducationalAgent:
    """
    获取全局教育智能体实例
    
    Args:
        llm_client: LLM客户端
        
    Returns:
        EducationalAgent实例
    """
    global _educational_agent
    if _educational_agent is None:
        _educational_agent = EducationalAgent(llm_client)
    return _educational_agent


def process_student_learning(
    user_id: int,
    session_id: str,
    user_input: str,
    context: Optional[Dict] = None
) -> Dict:
    """
    处理学生学习的便捷函数
    """
    agent = get_educational_agent()
    return agent.process_learning_interaction(
        user_id=user_id,
        session_id=session_id,
        user_input=user_input,
        context=context
    )


def grade_and_feedback(
    user_id: int,
    session_id: str,
    question: str,
    user_answer: str,
    expected_answer: str,
    concept_id: str,
    course_id: int
) -> Dict:
    """
    评分和反馈的便捷函数
    """
    agent = get_educational_agent()
    return agent.process_practice_answer(
        user_id=user_id,
        session_id=session_id,
        question=question,
        user_answer=user_answer,
        expected_answer=expected_answer,
        concept_id=concept_id,
        course_id=course_id
    )
