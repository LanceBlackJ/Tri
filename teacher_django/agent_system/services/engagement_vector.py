"""
Engagement Vector 参与度评估模块 - 基于论文 arXiv:2505.19803v2

论文核心实现：
- Cognitive Engagement (认知参与度)
- Emotional Engagement (情感参与度)
- Behavioral Engagement (行为参与度)
- 三维度加权融合计算总参与度
- 参与度归一化和时间序列分析
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import statistics

logger = logging.getLogger(__name__)


class EngagementLevel(Enum):
    """
    参与度等级划分
    """
    VERY_LOW = "very_low"      # 0.0 - 0.2
    LOW = "low"                # 0.2 - 0.4
    MEDIUM = "medium"          # 0.4 - 0.6
    HIGH = "high"              # 0.6 - 0.8
    VERY_HIGH = "very_high"    # 0.8 - 1.0


@dataclass
class EngagementMetrics:
    """
    参与度指标数据
    """
    # 时间戳
    timestamp: datetime = field(default_factory=datetime.now)
    
    # 认知参与度指标
    cognitive_metrics: Dict[str, float] = field(default_factory=dict)
    cognitive_score: float = 0.0
    
    # 情感参与度指标
    emotional_metrics: Dict[str, float] = field(default_factory=dict)
    emotional_score: float = 0.0
    
    # 行为参与度指标
    behavioral_metrics: Dict[str, float] = field(default_factory=dict)
    behavioral_score: float = 0.0
    
    # 综合参与度
    total_score: float = 0.0
    
    # 置信度
    confidence: float = 0.0
    
    def to_dict(self) -> Dict:
        return {
            'timestamp': self.timestamp.isoformat(),
            'cognitive_metrics': self.cognitive_metrics,
            'cognitive_score': self.cognitive_score,
            'emotional_metrics': self.emotional_metrics,
            'emotional_score': self.emotional_score,
            'behavioral_metrics': self.behavioral_metrics,
            'behavioral_score': self.behavioral_score,
            'total_score': self.total_score,
            'confidence': self.confidence
        }


@dataclass
class EngagementVector:
    """
    参与度向量
    
    表示用户在某一时刻的完整参与度状态
    
    结构:
    E = (Ec, Ee, Eb)
    - Ec: Cognitive Engagement (认知参与度)
    - Ee: Emotional Engagement (情感参与度)
    - Eb: Behavioral Engagement (行为参与度)
    """
    user_id: int
    timestamp: datetime = field(default_factory=datetime.now)
    
    # 三维度参与度
    cognitive: float = 0.0      # 0.0 - 1.0
    emotional: float = 0.0     # 0.0 - 1.0
    behavioral: float = 0.0    # 0.0 - 1.0
    
    # 加权总参与度
    total: float = 0.0
    
    # 各维度权重
    weights: Dict[str, float] = field(default_factory=lambda: {
        'cognitive': 0.4,
        'emotional': 0.35,
        'behavioral': 0.25
    })
    
    # 参与度等级
    level: EngagementLevel = EngagementLevel.MEDIUM
    
    # 原始指标数据
    raw_metrics: Dict = field(default_factory=dict)
    
    def calculate_total(self) -> float:
        """
        计算加权总参与度
        
        E = w1 × Ec + w2 × Ee + w3 × Eb
        
        Returns:
            加权总参与度
        """
        self.total = (
            self.weights['cognitive'] * self.cognitive +
            self.weights['emotional'] * self.emotional +
            self.weights['behavioral'] * self.behavioral
        )
        return self.total
    
    def determine_level(self) -> EngagementLevel:
        """
        确定参与度等级
        
        Returns:
            EngagementLevel
        """
        if self.total < 0.2:
            self.level = EngagementLevel.VERY_LOW
        elif self.total < 0.4:
            self.level = EngagementLevel.LOW
        elif self.total < 0.6:
            self.level = EngagementLevel.MEDIUM
        elif self.total < 0.8:
            self.level = EngagementLevel.HIGH
        else:
            self.level = EngagementLevel.VERY_HIGH
        return self.level
    
    def to_dict(self) -> Dict:
        return {
            'user_id': self.user_id,
            'timestamp': self.timestamp.isoformat(),
            'cognitive': self.cognitive,
            'emotional': self.emotional,
            'behavioral': self.behavioral,
            'total': self.total,
            'weights': self.weights,
            'level': self.level.value,
            'raw_metrics': self.raw_metrics
        }


class CognitiveEngagementCalculator:
    """
    认知参与度计算器
    
    基于论文中的认知参与度指标：
    1. 问题解决深度
    2. 信息加工水平
    3. 学习策略使用
    4. 注意力集中度
    """
    
    # 认知参与度指标权重
    INDICATOR_WEIGHTS = {
        'problem_solving_depth': 0.25,
        'information_processing': 0.25,
        'learning_strategy': 0.25,
        'attention_focus': 0.25
    }
    
    def __init__(self):
        # 问题解决深度指标
        self.depth_indicators = {
            'question_complexity': {
                'factual': 0.2,      # 事实性问题
                'comprehension': 0.4,  # 理解性问题
                'application': 0.6,    # 应用性问题
                'analysis': 0.8,       # 分析性问题
                'synthesis': 1.0,      # 综合性问题
                'evaluation': 1.0       # 评价性问题
            },
            'solution_attempts': {
                'none': 0.0,
                'one': 0.3,
                'multiple': 0.7,
                'persistent': 1.0
            },
            'explanation_quality': {
                'none': 0.0,
                'superficial': 0.3,
                'partial': 0.6,
                'comprehensive': 1.0
            }
        }
        
        # 信息加工水平指标
        self.processing_indicators = {
            'content_interaction': {
                'reading': 0.2,
                'highlighting': 0.4,
                'note_taking': 0.6,
                'summarizing': 0.8,
                'teaching': 1.0
            },
            'depth_of_processing': {
                'surface': 0.2,
                'moderate': 0.5,
                'deep': 0.8,
                'elaborate': 1.0
            }
        }
        
        # 学习策略使用指标
        self.strategy_indicators = {
            'self_regulation': {
                'none': 0.0,
                'monitoring': 0.3,
                'planning': 0.5,
                'evaluating': 0.7,
                'complete': 1.0
            },
            'strategy_diversity': {
                'single': 0.2,
                'few': 0.5,
                'varied': 0.8,
                'adaptive': 1.0
            }
        }
    
    def calculate(
        self,
        user_responses: List[Dict],
        interaction_data: Dict,
        time_on_task: float,
        context: Optional[Dict] = None
    ) -> Tuple[float, Dict]:
        """
        计算认知参与度
        
        Args:
            user_responses: 用户响应列表
            interaction_data: 交互数据
            time_on_task: 任务耗时（分钟）
            context: 上下文信息
            
        Returns:
            (认知参与度分数, 详细指标)
        """
        context = context or {}
        
        metrics = {}
        
        # 1. 问题解决深度
        problem_solving_score = self._calculate_problem_solving_depth(
            user_responses, interaction_data
        )
        metrics['problem_solving_depth'] = problem_solving_score
        
        # 2. 信息加工水平
        processing_score = self._calculate_information_processing(
            user_responses, interaction_data
        )
        metrics['information_processing'] = processing_score
        
        # 3. 学习策略使用
        strategy_score = self._calculate_learning_strategy(
            user_responses, context
        )
        metrics['learning_strategy'] = strategy_score
        
        # 4. 注意力集中度
        attention_score = self._calculate_attention_focus(
            time_on_task, interaction_data
        )
        metrics['attention_focus'] = attention_score
        
        # 计算加权总分
        total_score = sum(
            self.INDICATOR_WEIGHTS[key] * score
            for key, score in metrics.items()
        )
        
        return total_score, metrics
    
    def _calculate_problem_solving_depth(
        self,
        user_responses: List[Dict],
        interaction_data: Dict
    ) -> float:
        """
        计算问题解决深度
        """
        if not user_responses:
            return 0.3  # 默认中等
        
        scores = []
        
        for response in user_responses:
            # 处理字符串类型的响应
            if isinstance(response, str):
                # 字符串响应，基于长度和内容评估
                if len(response) > 100:
                    response_score = 0.7  # 详细回答
                elif len(response) > 20:
                    response_score = 0.5  # 中等回答
                else:
                    response_score = 0.3  # 简短回答
                scores.append(response_score)
                continue
            
            # 问题类型分数
            question_type = response.get('question_type', 'factual')
            type_score = self.depth_indicators['question_complexity'].get(
                question_type, 0.2
            )
            
            # 解答尝试分数
            attempts = response.get('attempts', 'none')
            attempt_score = self.depth_indicators['solution_attempts'].get(
                attempts, 0.0
            )
            
            # 解释质量分数
            explanation = response.get('explanation_quality', 'none')
            explain_score = self.depth_indicators['explanation_quality'].get(
                explanation, 0.0
            )
            
            # 综合分数
            response_score = (type_score * 0.4 + attempt_score * 0.3 + explain_score * 0.3)
            scores.append(response_score)
        
        return statistics.mean(scores) if scores else 0.3
    
    def _calculate_information_processing(
        self,
        user_responses: List[Dict],
        interaction_data: Dict
    ) -> float:
        """
        计算信息加工水平
        """
        if not interaction_data:
            return 0.4  # 默认中等
        
        # 内容交互深度
        content_type = interaction_data.get('content_type', 'reading')
        interaction_score = self.processing_indicators['content_interaction'].get(
            content_type, 0.2
        )
        
        # 处理深度
        processing_depth = interaction_data.get('processing_depth', 'moderate')
        depth_score = self.processing_indicators['depth_of_processing'].get(
            processing_depth, 0.5
        )
        
        return (interaction_score * 0.5 + depth_score * 0.5)
    
    def _calculate_learning_strategy(
        self,
        user_responses: List[Dict],
        context: Dict
    ) -> float:
        """
        计算学习策略使用
        """
        # 自我调节能力
        self_regulation = context.get('self_regulation', 'monitoring')
        regulation_score = self.strategy_indicators['self_regulation'].get(
            self_regulation, 0.3
        )
        
        # 策略多样性
        strategy_count = len(context.get('strategies_used', []))
        if strategy_count <= 1:
            diversity_score = 0.2
        elif strategy_count == 2:
            diversity_score = 0.5
        elif strategy_count == 3:
            diversity_score = 0.8
        else:
            diversity_score = 1.0
        
        return (regulation_score * 0.6 + diversity_score * 0.4)
    
    def _calculate_attention_focus(
        self,
        time_on_task: float,
        interaction_data: Dict
    ) -> float:
        """
        计算注意力集中度
        """
        # 基于时间的注意力评估
        if time_on_task < 2:
            time_score = 0.3
        elif time_on_task < 5:
            time_score = 0.6
        elif time_on_task < 15:
            time_score = 0.9
        else:
            # 超过15分钟，注意力可能下降
            time_score = max(0.7, 1.0 - (time_on_task - 15) * 0.02)
        
        # 交互中断评估
        interruptions = interaction_data.get('interruptions', 0)
        interruption_penalty = min(interruptions * 0.1, 0.3)
        
        return max(0.0, time_score - interruption_penalty)


class EmotionalEngagementCalculator:
    """
    情感参与度计算器
    
    基于论文中的情感参与度指标：
    1. 情感状态
    2. 情感强度
    3. 情感稳定性
    4. 积极情感比例
    """
    
    # 情感参与度指标权重
    INDICATOR_WEIGHTS = {
        'emotional_state': 0.25,
        'emotional_intensity': 0.25,
        'emotional_stability': 0.25,
        'positive_ratio': 0.25
    }
    
    # 情感状态对应的参与度分数
    EMOTION_SCORES = {
        'engaged': 0.8,
        'curious': 0.9,
        'confused': 0.5,
        'frustrated': 0.3,
        'anxious': 0.4,
        'bored': 0.2,
        'excited': 1.0,
        'confident': 0.9,
        'uncertain': 0.5,
        'satisfied': 0.9,
        'angry': 0.2,
        'overwhelmed': 0.3,
        'motivated': 0.9,
        'tired': 0.3
    }
    
    def __init__(self):
        pass
    
    def calculate(
        self,
        emotion_history: List[Dict],
        current_emotion: Optional[Dict] = None,
        context: Optional[Dict] = None
    ) -> Tuple[float, Dict]:
        """
        计算情感参与度
        
        Args:
            emotion_history: 情感历史记录
            current_emotion: 当前情感
            context: 上下文信息
            
        Returns:
            (情感参与度分数, 详细指标)
        """
        context = context or {}
        metrics = {}
        
        # 1. 当前情感状态分数
        state_score = self._calculate_emotional_state(current_emotion)
        metrics['emotional_state'] = state_score
        
        # 2. 情感强度
        intensity_score = self._calculate_emotional_intensity(
            emotion_history, current_emotion
        )
        metrics['emotional_intensity'] = intensity_score
        
        # 3. 情感稳定性
        stability_score = self._calculate_emotional_stability(emotion_history)
        metrics['emotional_stability'] = stability_score
        
        # 4. 积极情感比例
        positive_ratio = self._calculate_positive_ratio(emotion_history)
        metrics['positive_ratio'] = positive_ratio
        
        # 计算加权总分
        total_score = sum(
            self.INDICATOR_WEIGHTS[key] * score
            for key, score in metrics.items()
        )
        
        return total_score, metrics
    
    def _calculate_emotional_state(self, current_emotion: Optional[Dict]) -> float:
        """
        计算当前情感状态分数
        """
        if not current_emotion:
            return 0.5  # 默认中等
        
        emotion_state = current_emotion.get('state', 'engaged')
        return self.EMOTION_SCORES.get(emotion_state, 0.5)
    
    def _calculate_emotional_intensity(
        self,
        emotion_history: List[Dict],
        current_emotion: Optional[Dict] = None
    ) -> float:
        """
        计算情感强度
        
        适度的情感强度表示积极参与，过高或过低都不理想
        """
        if not emotion_history and not current_emotion:
            return 0.5
        
        intensities = [
            e.get('intensity', 0.5) 
            for e in emotion_history[-10:]  # 最近10条
        ]
        
        if current_emotion:
            intensities.append(current_emotion.get('intensity', 0.5))
        
        if not intensities:
            return 0.5
        
        avg_intensity = sum(intensities) / len(intensities)
        
        # 适度的情感强度（0.4-0.7）最理想
        if 0.4 <= avg_intensity <= 0.7:
            return avg_intensity
        elif avg_intensity < 0.4:
            # 情感过弱，增强
            return 0.4 + avg_intensity
        else:
            # 情感过强，适当降低
            return max(0.4, 1.0 - (avg_intensity - 0.7) * 0.5)
    
    def _calculate_emotional_stability(self, emotion_history: List[Dict]) -> float:
        """
        计算情感稳定性
        
        情感波动过大表示不稳定，适中波动是正常的
        """
        if len(emotion_history) < 2:
            return 0.6  # 数据不足，默认中等
        
        # 计算情感状态转换次数
        states = [e.get('state') for e in emotion_history[-10:]]
        transitions = sum(
            1 for i in range(1, len(states)) 
            if states[i] != states[i-1]
        )
        
        # 适当的情感变化是健康的
        # 完全没有变化可能表示无聊，太多变化可能表示不稳定
        if transitions == 0:
            return 0.4  # 完全没有变化，可能无聊
        elif transitions <= 3:
            return 0.8 + transitions * 0.05  # 适度变化，理想
        elif transitions <= 6:
            return 1.0 - (transitions - 3) * 0.1  # 变化较多
        else:
            return max(0.3, 0.7 - (transitions - 6) * 0.1)  # 变化过多
    
    def _calculate_positive_ratio(self, emotion_history: List[Dict]) -> float:
        """
        计算积极情感比例
        """
        positive_states = {'excited', 'confident', 'satisfied', 'engaged', 'curious', 'motivated'}
        negative_states = {'frustrated', 'anxious', 'bored', 'angry', 'overwhelmed', 'tired'}
        
        if not emotion_history:
            return 0.5  # 默认中等
        
        total = len(emotion_history)
        positive_count = sum(
            1 for e in emotion_history 
            if e.get('state') in positive_states
        )
        negative_count = sum(
            1 for e in emotion_history 
            if e.get('state') in negative_states
        )
        
        if positive_count + negative_count == 0:
            return 0.5
        
        return positive_count / (positive_count + negative_count)


class BehavioralEngagementCalculator:
    """
    行为参与度计算器
    
    基于论文中的行为参与度指标：
    1. 交互频率
    2. 任务完成率
    3. 自愿性参与
    4. 社交互动
    """
    
    # 行为参与度指标权重
    INDICATOR_WEIGHTS = {
        'interaction_frequency': 0.25,
        'task_completion': 0.30,
        'voluntary_participation': 0.25,
        'social_interaction': 0.20
    }
    
    def __init__(self):
        pass
    
    def calculate(
        self,
        interaction_data: Dict,
        task_data: Dict,
        context: Optional[Dict] = None
    ) -> Tuple[float, Dict]:
        """
        计算行为参与度
        
        Args:
            interaction_data: 交互数据
            task_data: 任务数据
            context: 上下文信息
            
        Returns:
            (行为参与度分数, 详细指标)
        """
        context = context or {}
        metrics = {}
        
        # 1. 交互频率
        frequency_score = self._calculate_interaction_frequency(interaction_data)
        metrics['interaction_frequency'] = frequency_score
        
        # 2. 任务完成率
        completion_score = self._calculate_task_completion(task_data)
        metrics['task_completion'] = completion_score
        
        # 3. 自愿性参与
        voluntary_score = self._calculate_voluntary_participation(
            interaction_data, context
        )
        metrics['voluntary_participation'] = voluntary_score
        
        # 4. 社交互动
        social_score = self._calculate_social_interaction(interaction_data)
        metrics['social_interaction'] = social_score
        
        # 计算加权总分
        total_score = sum(
            self.INDICATOR_WEIGHTS[key] * score
            for key, score in metrics.items()
        )
        
        return total_score, metrics
    
    def _calculate_interaction_frequency(self, interaction_data: Dict) -> float:
        """
        计算交互频率
        
        评估用户在一定时间内的交互次数
        """
        interactions_per_minute = interaction_data.get('interactions_per_minute', 0)
        avg_response_time = interaction_data.get('avg_response_time', 60)  # 秒
        
        # 基于交互频率的分数
        if interactions_per_minute <= 0:
            freq_score = 0.2
        elif interactions_per_minute < 0.1:
            freq_score = 0.4
        elif interactions_per_minute < 0.3:
            freq_score = 0.7
        elif interactions_per_minute < 0.5:
            freq_score = 0.9
        else:
            freq_score = 1.0
        
        # 基于响应时间的分数
        if avg_response_time < 10:
            time_score = 0.8  # 响应太快，可能没有深思熟虑
        elif avg_response_time < 30:
            time_score = 1.0  # 理想响应时间
        elif avg_response_time < 60:
            time_score = 0.8
        elif avg_response_time < 120:
            time_score = 0.6
        else:
            time_score = 0.4  # 响应过慢
        
        return (freq_score * 0.6 + time_score * 0.4)
    
    def _calculate_task_completion(self, task_data: Dict) -> float:
        """
        计算任务完成率
        """
        tasks_completed = task_data.get('tasks_completed', 0)
        tasks_total = task_data.get('tasks_total', 1)
        tasks_correct = task_data.get('tasks_correct', 0)
        tasks_attempted = task_data.get('tasks_attempted', 0)
        
        if tasks_total == 0:
            return 0.5  # 默认中等
        
        # 完成率
        completion_rate = tasks_completed / tasks_total
        
        # 正确率（如果尝试了）
        if tasks_attempted > 0:
            accuracy_rate = tasks_correct / tasks_attempted
        else:
            accuracy_rate = 0.5
        
        # 参与尝试率
        participation_rate = min(tasks_attempted / tasks_total, 1.0)
        
        # 综合分数
        return (
            completion_rate * 0.4 +
            accuracy_rate * 0.3 +
            participation_rate * 0.3
        )
    
    def _calculate_voluntary_participation(
        self,
        interaction_data: Dict,
        context: Dict
    ) -> float:
        """
        计算自愿性参与
        
        评估用户是否主动参与学习活动
        """
        # 自愿提问次数
        voluntary_questions = interaction_data.get('voluntary_questions', 0)
        
        # 自愿探索行为
        voluntary_exploration = interaction_data.get('voluntary_exploration', 0)
        
        # 主动寻求帮助
        help_seeking = interaction_data.get('help_seeking', 0)
        
        # 按时完成任务
        on_time_completion = context.get('on_time_completion', 0.5)
        
        # 额外练习
        extra_practice = context.get('extra_practice', 0)
        
        # 计算自愿性分数
        question_score = min(voluntary_questions / 5, 1.0)  # 最多5个问题
        exploration_score = min(voluntary_exploration / 3, 1.0)
        help_score = min(help_seeking / 3, 1.0)
        
        # 适度的求助是好的，过多或过少都不好
        if help_seeking == 0:
            help_adjusted = 0.5
        elif help_seeking <= 2:
            help_adjusted = 0.8
        else:
            help_adjusted = max(0.3, 1.0 - (help_seeking - 2) * 0.15)
        
        return (
            question_score * 0.3 +
            exploration_score * 0.2 +
            help_adjusted * 0.2 +
            on_time_completion * 0.2 +
            min(extra_practice / 2, 1.0) * 0.1
        )
    
    def _calculate_social_interaction(self, interaction_data: Dict) -> float:
        """
        计算社交互动
        
        评估用户与系统/其他人的互动质量
        """
        # 与AI助手的互动深度
        ai_interaction_depth = interaction_data.get('ai_interaction_depth', 'moderate')
        depth_scores = {
            'none': 0.0,
            'surface': 0.3,
            'moderate': 0.6,
            'deep': 0.9,
            'collaborative': 1.0
        }
        depth_score = depth_scores.get(ai_interaction_depth, 0.5)
        
        # 讨论参与
        discussion_participation = interaction_data.get('discussion_participation', 0)
        discussion_score = min(discussion_participation / 5, 1.0)
        
        # 知识分享
        knowledge_sharing = interaction_data.get('knowledge_sharing', False)
        sharing_score = 1.0 if knowledge_sharing else 0.3
        
        return (
            depth_score * 0.5 +
            discussion_score * 0.3 +
            sharing_score * 0.2
        )


class EngagementVectorEngine:
    """
    Engagement Vector 引擎 - 论文核心实现
    
    整合三个维度的参与度计算，生成完整的参与度向量
    """
    
    # 默认权重配置
    DEFAULT_WEIGHTS = {
        'cognitive': 0.40,   # 认知维度权重
        'emotional': 0.35,  # 情感维度权重
        'behavioral': 0.25  # 行为维度权重
    }
    
    def __init__(self, custom_weights: Optional[Dict] = None):
        """
        初始化参与度向量引擎
        
        Args:
            custom_weights: 自定义权重
        """
        self.cognitive_calculator = CognitiveEngagementCalculator()
        self.emotional_calculator = EmotionalEngagementCalculator()
        self.behavioral_calculator = BehavioralEngagementCalculator()
        
        if custom_weights:
            self.weights = custom_weights
        else:
            self.weights = self.DEFAULT_WEIGHTS.copy()
    
    def calculate_engagement_vector(
        self,
        user_id: int,
        interaction_data: Dict,
        task_data: Dict,
        emotion_history: List[Dict],
        current_emotion: Optional[Dict] = None,
        context: Optional[Dict] = None
    ) -> EngagementVector:
        """
        计算完整的参与度向量
        
        Args:
            user_id: 用户ID
            interaction_data: 交互数据
            task_data: 任务数据
            emotion_history: 情感历史
            current_emotion: 当前情感
            context: 上下文信息
            
        Returns:
            EngagementVector: 完整的参与度向量
        """
        context = context or {}
        time_on_task = context.get('time_on_task', 5)  # 默认5分钟
        user_responses = context.get('user_responses', [])
        
        # 1. 计算认知参与度
        cognitive_score, cognitive_metrics = self.cognitive_calculator.calculate(
            user_responses=user_responses,
            interaction_data=interaction_data,
            time_on_task=time_on_task,
            context=context
        )
        
        # 2. 计算情感参与度
        emotional_score, emotional_metrics = self.emotional_calculator.calculate(
            emotion_history=emotion_history,
            current_emotion=current_emotion,
            context=context
        )
        
        # 3. 计算行为参与度
        behavioral_score, behavioral_metrics = self.behavioral_calculator.calculate(
            interaction_data=interaction_data,
            task_data=task_data,
            context=context
        )
        
        # 4. 创建参与度向量
        vector = EngagementVector(
            user_id=user_id,
            timestamp=datetime.now(),
            cognitive=cognitive_score,
            emotional=emotional_score,
            behavioral=behavioral_score,
            weights=self.weights.copy()
        )
        
        # 5. 计算加权总参与度
        vector.calculate_total()
        
        # 6. 确定参与度等级
        vector.determine_level()
        
        # 7. 保存原始指标
        vector.raw_metrics = {
            'cognitive_metrics': cognitive_metrics,
            'emotional_metrics': emotional_metrics,
            'behavioral_metrics': behavioral_metrics
        }
        
        return vector
    
    def normalize_engagement_metrics(
        self,
        raw_metrics: Dict,
        normalization_params: Optional[Dict] = None
    ) -> Dict:
        """
        归一化参与度指标
        
        用于将不同量纲的指标归一化到[0, 1]区间
        
        Args:
            raw_metrics: 原始指标数据
            normalization_params: 归一化参数
            
        Returns:
            归一化后的指标
        """
        # 使用min-max归一化
        normalized = {}
        
        for dimension, metrics in raw_metrics.items():
            if isinstance(metrics, dict):
                normalized[dimension] = {}
                for key, value in metrics.items():
                    # 默认映射到[0, 1]
                    normalized[dimension][key] = min(max(value, 0.0), 1.0)
            else:
                normalized[dimension] = min(max(metrics, 0.0), 1.0)
        
        return normalized
    
    def calculate_weighted_fusion(
        self,
        cognitive: float,
        emotional: float,
        behavioral: float,
        weights: Optional[Dict] = None
    ) -> float:
        """
        计算加权融合参与度
        
        E = w1 × Ec + w2 × Ee + w3 × Eb
        
        Args:
            cognitive: 认知参与度
            emotional: 情感参与度
            behavioral: 行为参与度
            weights: 自定义权重
            
        Returns:
            加权融合后的总参与度
        """
        if weights is None:
            weights = self.weights
        
        return (
            weights['cognitive'] * cognitive +
            weights['emotional'] * emotional +
            weights['behavioral'] * behavioral
        )
    
    def analyze_engagement_trend(
        self,
        engagement_history: List[EngagementVector]
    ) -> Dict:
        """
        分析参与度趋势
        
        Args:
            engagement_history: 参与度历史记录
            
        Returns:
            趋势分析结果
        """
        if len(engagement_history) < 2:
            return {
                'trend': 'insufficient_data',
                'change_rate': 0.0,
                'stability': 0.0
            }
        
        # 计算变化率
        recent = engagement_history[-3:]
        older = engagement_history[:3]
        
        recent_avg = sum(e.total for e in recent) / len(recent)
        older_avg = sum(e.total for e in older) / len(older) if older else recent_avg
        
        if older_avg > 0:
            change_rate = (recent_avg - older_avg) / older_avg
        else:
            change_rate = 0.0
        
        # 计算稳定性（标准差）
        all_scores = [e.total for e in engagement_history[-10:]]
        stability = 1.0 - min(statistics.stdev(all_scores) if len(all_scores) > 1 else 0, 1.0)
        
        # 判断趋势
        if change_rate > 0.1:
            trend = 'improving'
        elif change_rate < -0.1:
            trend = 'declining'
        else:
            trend = 'stable'
        
        return {
            'trend': trend,
            'change_rate': change_rate,
            'stability': stability,
            'recent_avg': recent_avg,
            'older_avg': older_avg
        }
    
    def generate_engagement_report(
        self,
        user_id: int,
        current_vector: EngagementVector,
        historical_data: List[EngagementVector]
    ) -> Dict:
        """
        生成参与度报告
        
        Args:
            user_id: 用户ID
            current_vector: 当前参与度向量
            historical_data: 历史参与度数据
            
        Returns:
            参与度报告
        """
        # 趋势分析
        trend_analysis = self.analyze_engagement_trend(historical_data + [current_vector])
        
        # 维度分析
        dimensions = {
            'cognitive': {
                'score': current_vector.cognitive,
                'interpretation': self._interpret_dimension('cognitive', current_vector.cognitive),
                'metrics': current_vector.raw_metrics.get('cognitive_metrics', {})
            },
            'emotional': {
                'score': current_vector.emotional,
                'interpretation': self._interpret_dimension('emotional', current_vector.emotional),
                'metrics': current_vector.raw_metrics.get('emotional_metrics', {})
            },
            'behavioral': {
                'score': current_vector.behavioral,
                'interpretation': self._interpret_dimension('behavioral', current_vector.behavioral),
                'metrics': current_vector.raw_metrics.get('behavioral_metrics', {})
            }
        }
        
        # 建议
        suggestions = self._generate_suggestions(dimensions, trend_analysis)
        
        return {
            'user_id': user_id,
            'timestamp': current_vector.timestamp.isoformat(),
            'overall_engagement': {
                'total_score': current_vector.total,
                'level': current_vector.level.value,
                'weights': current_vector.weights
            },
            'dimensions': dimensions,
            'trend_analysis': trend_analysis,
            'suggestions': suggestions
        }
    
    def _interpret_dimension(self, dimension: str, score: float) -> str:
        """
        解释维度分数
        
        Args:
            dimension: 维度名称
            score: 分数
            
        Returns:
            解释文本
        """
        interpretations = {
            'cognitive': {
                (0.0, 0.3): '认知参与度较低，需要更多思考和探索',
                (0.3, 0.5): '认知参与度中等，可以尝试更深入的学习',
                (0.5, 0.7): '认知参与度良好，正在积极思考',
                (0.7, 1.0): '认知参与度很高，展现了深度思考能力'
            },
            'emotional': {
                (0.0, 0.3): '情感参与度较低，可能缺乏学习动力',
                (0.3, 0.5): '情感参与度中等，可以增加趣味性',
                (0.5, 0.7): '情感参与度良好，学习态度积极',
                (0.7, 1.0): '情感参与度很高，学习热情高涨'
            },
            'behavioral': {
                (0.0, 0.3): '行为参与度较低，需要提高学习活跃度',
                (0.3, 0.5): '行为参与度中等，可以更主动参与',
                (0.5, 0.7): '行为参与度良好，学习行为积极',
                (0.7, 1.0): '行为参与度很高，学习行为非常主动'
            }
        }
        
        for (low, high), text in interpretations.get(dimension, {}).items():
            if low <= score < high:
                return text
        return '未知状态'
    
    def _generate_suggestions(
        self,
        dimensions: Dict,
        trend_analysis: Dict
    ) -> List[str]:
        """
        生成改进建议
        
        Args:
            dimensions: 维度分析结果
            trend_analysis: 趋势分析结果
            
        Returns:
            建议列表
        """
        suggestions = []
        
        # 基于维度分数的建议
        for dim_name, dim_data in dimensions.items():
            score = dim_data['score']
            
            if score < 0.4:
                if dim_name == 'cognitive':
                    suggestions.append('建议：尝试回答更深层次的问题，挑战自己的思维')
                elif dim_name == 'emotional':
                    suggestions.append('建议：尝试用有趣的方式学习，找到学习的乐趣')
                elif dim_name == 'behavioral':
                    suggestions.append('建议：更主动地参与学习活动，多做练习')
        
        # 基于趋势的建议
        trend = trend_analysis.get('trend', 'stable')
        if trend == 'declining':
            suggestions.append('注意：你的参与度有下降趋势，建议调整学习方法')
        elif trend == 'improving':
            suggestions.append('很好：你的参与度在提升，保持当前的学习状态')
        
        return suggestions


# 全局参与度引擎实例
_engagement_engine: Optional[EngagementVectorEngine] = None


def get_engagement_engine(custom_weights: Optional[Dict] = None) -> EngagementVectorEngine:
    """
    获取全局参与度引擎实例
    
    Args:
        custom_weights: 自定义权重
        
    Returns:
        EngagementVectorEngine实例
    """
    global _engagement_engine
    if _engagement_engine is None:
        _engagement_engine = EngagementVectorEngine(custom_weights)
    return _engagement_engine


def calculate_user_engagement(
    user_id: int,
    interaction_data: Dict,
    task_data: Dict,
    emotion_history: List[Dict],
    current_emotion: Optional[Dict] = None,
    context: Optional[Dict] = None,
    custom_weights: Optional[Dict] = None
) -> Dict:
    """
    计算用户参与度的便捷函数
    
    Args:
        user_id: 用户ID
        interaction_data: 交互数据
        task_data: 任务数据
        emotion_history: 情感历史
        current_emotion: 当前情感
        context: 上下文信息
        custom_weights: 自定义权重
        
    Returns:
        参与度分析结果
    """
    engine = get_engagement_engine(custom_weights)
    
    # 计算参与度向量
    vector = engine.calculate_engagement_vector(
        user_id=user_id,
        interaction_data=interaction_data,
        task_data=task_data,
        emotion_history=emotion_history,
        current_emotion=current_emotion,
        context=context
    )
    
    return vector.to_dict()
