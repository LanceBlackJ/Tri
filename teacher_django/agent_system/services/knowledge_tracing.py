"""
知识追踪模块 - 基于论文 arXiv:2503.11733v2

论文核心实现：
- Deep Knowledge Tracing (DKT)
- Knowledge State Estimation
- Learning Rate Estimation
- Concept Mastery Tracking
- Adaptive Learning Path Generation
"""

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict

logger = logging.getLogger(__name__)


class KnowledgeState(Enum):
    """
    知识掌握状态
    """
    NOT_STARTED = "not_started"      # 未开始
    ACQUIRING = "acquiring"          # 获取中
    PRACTICING = "practicing"        # 练习中
    MASTERED = "mastered"            # 已掌握
    FORGOTTEN = "forgotten"         # 已遗忘
    DECAYED = "decayed"             # 衰减中


@dataclass
class ConceptInfo:
    """
    知识点信息
    """
    concept_id: str
    name: str
    description: str = ""
    prerequisites: List[str] = field(default_factory=list)  # 前置知识点
    related_concepts: List[str] = field(default_factory=list)  # 相关知识点
    
    # 难度等级
    difficulty: float = 0.5  # 0.0 - 1.0
    
    # 关联课程/章节
    course_id: Optional[int] = None
    chapter: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return {
            'concept_id': self.concept_id,
            'name': self.name,
            'description': self.description,
            'prerequisites': self.prerequisites,
            'related_concepts': self.related_concepts,
            'difficulty': self.difficulty,
            'course_id': self.course_id,
            'chapter': self.chapter
        }


@dataclass
class KnowledgeStateRecord:
    """
    知识状态记录
    
    记录用户对某个知识点的掌握状态
    """
    user_id: int
    concept_id: str
    
    # 状态
    state: KnowledgeState = KnowledgeState.NOT_STARTED
    mastery_level: float = 0.0  # 0.0 - 1.0
    
    # 估计参数
    estimated_mastery: float = 0.0  # 基于DKT估计
    learning_rate: float = 0.0
    
    # 时间信息
    first_exposure: Optional[datetime] = None
    last_practice: Optional[datetime] = None
    next_review: Optional[datetime] = None
    
    # 练习统计
    practice_count: int = 0
    correct_count: int = 0
    total_attempts: int = 0
    
    # 历史记录
    practice_history: List[Dict] = field(default_factory=list)
    
    # 遗忘曲线
    decay_rate: float = 0.1  # 遗忘率
    
    def get_accuracy(self) -> float:
        """计算正确率"""
        if self.total_attempts == 0:
            return 0.0
        return self.correct_count / self.total_attempts
    
    def update_after_attempt(self, correct: bool, timestamp: datetime = None):
        """
        更新练习记录
        
        Args:
            correct: 是否正确
            timestamp: 时间戳
        """
        timestamp = timestamp or datetime.now()
        
        self.total_attempts += 1
        if correct:
            self.correct_count += 1
        
        # 更新练习历史
        self.practice_history.append({
            'timestamp': timestamp.isoformat(),
            'correct': correct,
            'mastery_before': self.mastery_level
        })
        
        # 更新状态
        self.last_practice = timestamp
        self.practice_count += 1
        
        # 更新掌握度
        self._update_mastery()
        
        # 估计下次复习时间
        self._estimate_next_review()
    
    def _update_mastery(self):
        """更新掌握度"""
        # 简单的掌握度计算
        if self.total_attempts == 0:
            return
        
        accuracy = self.get_accuracy()
        
        # 基于正确率和练习次数
        base_mastery = accuracy
        
        # 考虑练习次数的加成
        if self.practice_count >= 10:
            practice_bonus = 0.1
        elif self.practice_count >= 5:
            practice_bonus = 0.05
        else:
            practice_bonus = 0.0
        
        # 近期正确率的权重更高
        if len(self.practice_history) >= 3:
            recent = self.practice_history[-3:]
            recent_accuracy = sum(1 for p in recent if p['correct']) / len(recent)
            base_mastery = base_mastery * 0.5 + recent_accuracy * 0.5
        
        self.mastery_level = min(base_mastery + practice_bonus, 1.0)
        
        # 更新状态
        if self.mastery_level >= 0.9:
            self.state = KnowledgeState.MASTERED
        elif self.mastery_level >= 0.6:
            self.state = KnowledgeState.PRACTICING
        elif self.mastery_level >= 0.3:
            self.state = KnowledgeState.ACQUIRING
        elif self.total_attempts > 0:
            self.state = KnowledgeState.ACQUIRING
        else:
            self.state = KnowledgeState.NOT_STARTED
    
    def _estimate_next_review(self):
        """估计下次复习时间（基于遗忘曲线）"""
        if self.state == KnowledgeState.MASTERED:
            # 掌握良好的知识点，复习间隔较长
            self.next_review = datetime.now() + timedelta(days=7)
        elif self.state == KnowledgeState.PRACTICING:
            self.next_review = datetime.now() + timedelta(days=3)
        elif self.state == KnowledgeState.ACQUIRING:
            self.next_review = datetime.now() + timedelta(days=1)
        else:
            self.next_review = datetime.now()
    
    def apply_forgetting(self, days_elapsed: float):
        """
        应用遗忘
        
        基于艾宾浩斯遗忘曲线
        
        Args:
            days_elapsed: 经过的天数
        """
        # 遗忘曲线公式: R = e^(-t/S)
        # S 是记忆强度参数
        S = 1.0 / self.decay_rate if self.decay_rate > 0 else 10
        
        retention = math.exp(-days_elapsed / S)
        self.mastery_level *= retention
        
        # 检查是否需要重新学习
        if self.mastery_level < 0.3 and self.state == KnowledgeState.MASTERED:
            self.state = KnowledgeState.FORGOTTEN
    
    def to_dict(self) -> Dict:
        return {
            'user_id': self.user_id,
            'concept_id': self.concept_id,
            'state': self.state.value,
            'mastery_level': self.mastery_level,
            'estimated_mastery': self.estimated_mastery,
            'learning_rate': self.learning_rate,
            'first_exposure': self.first_exposure.isoformat() if self.first_exposure else None,
            'last_practice': self.last_practice.isoformat() if self.last_practice else None,
            'next_review': self.next_review.isoformat() if self.next_review else None,
            'practice_count': self.practice_count,
            'correct_count': self.correct_count,
            'total_attempts': self.total_attempts,
            'accuracy': self.get_accuracy(),
            'decay_rate': self.decay_rate
        }


class DeepKnowledgeTracing:
    """
    深度知识追踪 (DKT)
    
    基于论文的DKT算法，使用LSTM-like结构追踪知识状态
    """
    
    def __init__(self, hidden_dim: int = 100):
        """
        初始化DKT
        
        Args:
            hidden_dim: 隐藏层维度
        """
        self.hidden_dim = hidden_dim
        
        # 知识状态表示 {user_id: {concept_id: knowledge_vector}}
        self.knowledge_states: Dict[int, Dict[str, List[float]]] = defaultdict(dict)
        
        # LSTM隐藏状态
        self.lstm_hidden: Dict[int, Tuple[List[float], List[float]]] = {}
    
    def get_knowledge_state_vector(
        self,
        user_id: int,
        concept_id: str
    ) -> List[float]:
        """
        获取知识状态向量
        
        Args:
            user_id: 用户ID
            concept_id: 知识点ID
            
        Returns:
            知识状态向量
        """
        return self.knowledge_states.get(user_id, {}).get(concept_id, [0.0] * self.hidden_dim)
    
    def update_knowledge_state(
        self,
        user_id: int,
        concept_id: str,
        interaction_data: Dict
    ) -> List[float]:
        """
        更新知识状态
        
        Args:
            user_id: 用户ID
            concept_id: 知识点ID
            interaction_data: 交互数据，包含：
                - correct: 是否正确
                - response_time: 响应时间
                - hints_used: 使用的提示数
                - attempt_number: 尝试次数
                
        Returns:
            更新后的知识状态向量
        """
        # 获取当前状态
        current_state = self.get_knowledge_state_vector(user_id, concept_id)
        
        # 构建输入向量
        correct = interaction_data.get('correct', False)
        response_time = interaction_data.get('response_time', 30)
        hints_used = interaction_data.get('hints_used', 0)
        
        # 创建交互特征
        input_features = [
            1.0 if correct else 0.0,
            min(response_time / 120, 1.0),  # 标准化响应时间
            min(hints_used / 3, 1.0)  # 标准化提示使用
        ]
        
        # 简化的LSTM更新（实际应该用真实的LSTM）
        new_state = self._lstm_cell(current_state, input_features)
        
        # 更新存储
        self.knowledge_states[user_id][concept_id] = new_state
        
        return new_state
    
    def _lstm_cell(
        self,
        hidden_state: List[float],
        input_features: List[float]
    ) -> List[float]:
        """
        简化的LSTM单元
        
        Args:
            hidden_state: 隐藏状态
            input_features: 输入特征
            
        Returns:
            新的隐藏状态
        """
        # 简化的门控机制
        learning_rate = 0.1
        
        # 输入门
        input_gate = [min(f + 0.5, 1.0) for f in input_features]
        
        # 更新隐藏状态
        new_hidden = []
        for i, h in enumerate(hidden_state):
            if i < len(input_features):
                update = input_features[i] * learning_rate * (1 - h)
                new_h = h + input_gate[i] * update
            else:
                new_h = h * (1 - learning_rate * 0.1)
            new_hidden.append(max(0.0, min(1.0, new_h)))
        
        return new_hidden
    
    def predict_mastery(
        self,
        user_id: int,
        concept_id: str
    ) -> float:
        """
        预测掌握度
        
        Args:
            user_id: 用户ID
            concept_id: 知识点ID
            
        Returns:
            预测的掌握度 (0.0 - 1.0)
        """
        state_vector = self.get_knowledge_state_vector(user_id, concept_id)
        
        # 计算平均激活度
        if not state_vector:
            return 0.0
        
        return sum(state_vector) / len(state_vector)
    
    def get_learning_dynamics(
        self,
        user_id: int,
        concept_id: str
    ) -> Dict:
        """
        获取学习动态
        
        Args:
            user_id: 用户ID
            concept_id: 知识点ID
            
        Returns:
            学习动态信息
        """
        state_vector = self.get_knowledge_state_vector(user_id, concept_id)
        
        # 分析状态向量的变化
        if not state_vector:
            return {
                'mastery': 0.0,
                'stability': 0.0,
                'learning_speed': 0.0
            }
        
        mastery = sum(state_vector) / len(state_vector)
        
        # 稳定性（标准差）
        variance = sum((x - mastery) ** 2 for x in state_vector) / len(state_vector)
        stability = 1.0 - min(math.sqrt(variance), 1.0)
        
        # 学习速度（假设状态向量随时间增加）
        learning_speed = mastery / max(sum(1 for x in state_vector if x > 0.5), 1)
        
        return {
            'mastery': mastery,
            'stability': stability,
            'learning_speed': learning_speed,
            'state_vector_dim': len(state_vector)
        }


class SpacedRepetition:
    """
    间隔重复算法
    
    基于艾宾浩斯遗忘曲线实现自适应复习调度
    """
    
    # 间隔参数
    INITIAL_INTERVAL = 1  # 初始间隔（天）
    MIN_INTERVAL = 0.5    # 最小间隔（天）
    MAX_INTERVAL = 30    # 最大间隔（天）
    
    # 难度系数
    DIFFICULTY_MULTIPLIERS = {
        'easy': 2.5,
        'medium': 2.0,
        'hard': 1.2,
        'fail': 1.0
    }
    
    def calculate_next_review(
        self,
        current_mastery: float,
        accuracy: float,
        previous_interval: float,
        difficulty: str = 'medium'
    ) -> Tuple[float, float]:
        """
        计算下次复习间隔
        
        基于SM-2算法改进
        
        Args:
            current_mastery: 当前掌握度
            accuracy: 正确率
            previous_interval: 前一次间隔
            difficulty: 难度等级
            
        Returns:
            (新间隔, 新的间隔乘数)
        """
        # 基础间隔
        if previous_interval < self.MIN_INTERVAL:
            new_interval = self.INITIAL_INTERVAL
        else:
            # 根据正确率调整
            if accuracy >= 0.9:
                ease_factor = 1.3
            elif accuracy >= 0.8:
                ease_factor = 1.2
            elif accuracy >= 0.7:
                ease_factor = 1.0
            elif accuracy >= 0.6:
                ease_factor = 0.8
            else:
                ease_factor = 0.5
            
            # 应用难度乘数
            difficulty_mult = self.DIFFICULTY_MULTIPLIERS.get(difficulty, 1.0)
            
            # 计算新间隔
            new_interval = previous_interval * ease_factor * difficulty_mult
        
        # 边界约束
        new_interval = max(self.MIN_INTERVAL, min(new_interval, self.MAX_INTERVAL))
        
        # 根据掌握度调整
        if current_mastery >= 0.9:
            # 掌握良好，增加间隔
            new_interval *= 1.2
        elif current_mastery < 0.5:
            # 掌握不足，减少间隔
            new_interval *= 0.7
        
        # 再次约束
        new_interval = max(self.MIN_INTERVAL, min(new_interval, self.MAX_INTERVAL))
        
        # 计算新的难度评估
        if accuracy >= 0.85:
            new_difficulty = 'easy'
        elif accuracy >= 0.65:
            new_difficulty = 'medium'
        elif accuracy >= 0.45:
            new_difficulty = 'hard'
        else:
            new_difficulty = 'fail'
        
        return new_interval, new_difficulty
    
    def generate_review_schedule(
        self,
        knowledge_records: Dict[str, KnowledgeStateRecord],
        review_limit: int = 20
    ) -> List[Tuple[str, datetime]]:
        """
        生成复习计划
        
        Args:
            knowledge_records: 知识状态记录字典
            review_limit: 复习数量限制
            
        Returns:
            [(concept_id, review_time), ...] 按复习时间排序
        """
        schedule = []
        
        now = datetime.now()
        
        for concept_id, record in knowledge_records.items():
            # 计算需要复习的优先级
            if record.next_review:
                time_until_review = (record.next_review - now).total_seconds() / 3600  # 小时
                
                # 优先级分数
                priority_score = 0
                
                # 越接近复习时间的优先级越高
                if time_until_review <= 0:
                    priority_score += 100
                else:
                    priority_score += max(0, 50 - time_until_review)
                
                # 掌握度越低优先级越高
                priority_score += (1.0 - record.mastery_level) * 30
                
                # 练习次数越少优先级越高
                if record.practice_count < 3:
                    priority_score += 20
                
                schedule.append((
                    concept_id,
                    record.next_review,
                    priority_score
                ))
        
        # 按优先级排序
        schedule.sort(key=lambda x: x[2], reverse=True)
        
        # 取前N个
        return [(c, t) for c, t, _ in schedule[:review_limit]]


class ConceptPrerequisiteGraph:
    """
    知识点前置依赖图
    
    管理知识点之间的依赖关系，支持学习路径规划
    """
    
    def __init__(self):
        """初始化依赖图"""
        # 图结构: {concept_id: ConceptInfo}
        self.concepts: Dict[str, ConceptInfo] = {}
        
        # 邻接表: {concept_id: [prerequisite_ids]}
        self.prerequisites: Dict[str, List[str]] = defaultdict(list)
        
        # 反向邻接表: {concept_id: [dependent_ids]}
        self.dependents: Dict[str, List[str]] = defaultdict(list)
    
    def add_concept(
        self,
        concept: ConceptInfo
    ):
        """
        添加知识点
        
        Args:
            concept: 知识点信息
        """
        self.concepts[concept.concept_id] = concept
        
        # 更新依赖关系
        for prereq_id in concept.prerequisites:
            self.prerequisites[concept.concept_id].append(prereq_id)
            self.dependents[prereq_id].append(concept.concept_id)
    
    def get_learning_sequence(
        self,
        target_concept_id: str,
        user_mastery: Dict[str, float]
    ) -> List[str]:
        """
        获取学习序列
        
        基于用户当前掌握情况，规划学习路径
        
        Args:
            target_concept_id: 目标知识点
            user_mastery: 用户掌握度字典
            
        Returns:
            学习序列
        """
        # 检查目标知识点是否存在
        if target_concept_id not in self.concepts:
            return []
        
        # 获取所有前置知识点
        prereqs = self._get_all_prerequisites(target_concept_id)
        
        # 排序：未掌握的优先，难度低的优先
        sequence = []
        for prereq in prereqs:
            mastery = user_mastery.get(prereq, 0.0)
            difficulty = self.concepts[prereq].difficulty
            
            sequence.append((prereq, mastery, difficulty))
        
        # 排序：首先按未掌握程度，然后按难度
        sequence.sort(key=lambda x: (x[1], x[2]))
        
        return [s[0] for s in sequence]
    
    def _get_all_prerequisites(
        self,
        concept_id: str,
        visited: Optional[set] = None
    ) -> List[str]:
        """
        获取所有前置知识点（递归）
        
        Args:
            concept_id: 知识点ID
            visited: 已访问集合
            
        Returns:
            前置知识点列表
        """
        if visited is None:
            visited = set()
        
        if concept_id in visited:
            return []
        
        visited.add(concept_id)
        
        prereqs = []
        for prereq_id in self.prerequisites.get(concept_id, []):
            prereqs.append(prereq_id)
            prereqs.extend(self._get_all_prerequisites(prereq_id, visited))
        
        return list(set(prereqs))
    
    def get_ready_to_learn(
        self,
        user_mastery: Dict[str, float]
    ) -> List[str]:
        """
        获取可以学习的新知识点
        
        Args:
            user_mastery: 用户掌握度字典
            
        Returns:
            可以学习的知识点列表
        """
        ready = []
        
        for concept_id, concept in self.concepts.items():
            # 已经掌握的跳过
            if user_mastery.get(concept_id, 0.0) >= 0.8:
                continue
            
            # 检查前置条件是否满足
            prereqs_met = all(
                user_mastery.get(prereq, 0.0) >= 0.6
                for prereq in concept.prerequisites
            )
            
            if prereqs_met:
                ready.append(concept_id)
        
        return ready


class KnowledgeTracingEngine:
    """
    知识追踪引擎 - 论文核心实现
    
    整合DKT、间隔重复和前置依赖图
    实现完整的知识追踪功能
    """
    
    def __init__(self):
        """初始化知识追踪引擎"""
        self.dkt = DeepKnowledgeTracing()
        self.spaced_repetition = SpacedRepetition()
        self.prerequisite_graph = ConceptPrerequisiteGraph()
        
        # 知识状态记录: {user_id: {concept_id: KnowledgeStateRecord}}
        self.knowledge_records: Dict[int, Dict[str, KnowledgeStateRecord]] = defaultdict(dict)
        
        # 知识点信息: {course_id: {concept_id: ConceptInfo}}
        self.concept_info: Dict[int, Dict[str, ConceptInfo]] = defaultdict(dict)
    
    def register_concept(
        self,
        course_id: int,
        concept: ConceptInfo
    ):
        """
        注册知识点
        
        Args:
            course_id: 课程ID
            concept: 知识点信息
        """
        self.concept_info[course_id][concept.concept_id] = concept
        self.prerequisite_graph.add_concept(concept)
    
    def record_interaction(
        self,
        user_id: int,
        concept_id: str,
        course_id: int,
        interaction_data: Dict
    ) -> KnowledgeStateRecord:
        """
        记录学习交互
        
        Args:
            user_id: 用户ID
            concept_id: 知识点ID
            course_id: 课程ID
            interaction_data: 交互数据，包含：
                - correct: 是否正确
                - response_time: 响应时间
                - hints_used: 使用的提示数
                - attempt_number: 尝试次数
                
        Returns:
            更新后的知识状态记录
        """
        # 获取或创建记录
        if concept_id not in self.knowledge_records[user_id]:
            self.knowledge_records[user_id][concept_id] = KnowledgeStateRecord(
                user_id=user_id,
                concept_id=concept_id,
                first_exposure=datetime.now()
            )
        
        record = self.knowledge_records[user_id][concept_id]
        
        # 更新DKT
        self.dkt.update_knowledge_state(user_id, concept_id, interaction_data)
        
        # 更新记录
        record.update_after_attempt(
            correct=interaction_data.get('correct', False),
            timestamp=interaction_data.get('timestamp', datetime.now())
        )
        
        # 更新估计的掌握度
        record.estimated_mastery = self.dkt.predict_mastery(user_id, concept_id)
        
        # 计算新的复习间隔
        new_interval, _ = self.spaced_repetition.calculate_next_review(
            current_mastery=record.mastery_level,
            accuracy=record.get_accuracy(),
            previous_interval=1.0,  # 简化处理
            difficulty=interaction_data.get('difficulty', 'medium')
        )
        
        return record
    
    def get_knowledge_state(
        self,
        user_id: int,
        concept_id: str
    ) -> Optional[KnowledgeStateRecord]:
        """
        获取知识状态
        
        Args:
            user_id: 用户ID
            concept_id: 知识点ID
            
        Returns:
            知识状态记录
        """
        return self.knowledge_records.get(user_id, {}).get(concept_id)
    
    def get_user_knowledge_profile(
        self,
        user_id: int,
        course_id: Optional[int] = None
    ) -> Dict:
        """
        获取用户知识画像
        
        Args:
            user_id: 用户ID
            course_id: 课程ID（可选）
            
        Returns:
            知识画像
        """
        records = self.knowledge_records.get(user_id, {})
        
        if course_id:
            # 只返回指定课程的知识状态
            course_concepts = self.concept_info.get(course_id, {})
            records = {
                k: v for k, v in records.items()
                if k in course_concepts
            }
        
        # 计算总体统计
        total = len(records)
        mastered = sum(1 for r in records.values() if r.state == KnowledgeState.MASTERED)
        practicing = sum(1 for r in records.values() if r.state == KnowledgeState.PRACTICING)
        acquiring = sum(1 for r in records.values() if r.state == KnowledgeState.ACQUIRING)
        
        # 计算平均掌握度
        if records:
            avg_mastery = sum(r.mastery_level for r in records.values()) / total
        else:
            avg_mastery = 0.0
        
        # 知识点状态分布
        concept_states = {
            concept_id: {
                'state': record.state.value,
                'mastery': record.mastery_level,
                'accuracy': record.get_accuracy(),
                'practice_count': record.practice_count,
                'last_practice': record.last_practice.isoformat() if record.last_practice else None
            }
            for concept_id, record in records.items()
        }
        
        # 需要复习的知识点
        needs_review = [
            {
                'concept_id': concept_id,
                'mastery': record.mastery_level,
                'next_review': record.next_review.isoformat() if record.next_review else None
            }
            for concept_id, record in records.items()
            if record.next_review and record.next_review <= datetime.now()
        ]
        needs_review.sort(key=lambda x: x['mastery'])
        
        # DKF学习动态
        dkt_states = {}
        for concept_id in records:
            dkt_states[concept_id] = self.dkt.get_learning_dynamics(user_id, concept_id)
        
        return {
            'user_id': user_id,
            'course_id': course_id,
            'summary': {
                'total_concepts': total,
                'mastered': mastered,
                'practicing': practicing,
                'acquiring': acquiring,
                'average_mastery': avg_mastery,
                'mastery_rate': mastered / total if total > 0 else 0.0
            },
            'concept_states': concept_states,
            'needs_review': needs_review,
            'dkt_states': dkt_states
        }
    
    def generate_learning_path(
        self,
        user_id: int,
        target_concepts: List[str],
        course_id: int,
        max_concepts: int = 10
    ) -> List[Dict]:
        """
        生成学习路径
        
        Args:
            user_id: 用户ID
            target_concepts: 目标知识点列表
            course_id: 课程ID
            max_concepts: 最大知识点数
            
        Returns:
            学习路径
        """
        # 获取用户当前掌握情况
        user_mastery = {
            concept_id: record.mastery_level
            for concept_id, record in self.knowledge_records[user_id].items()
        }
        
        learning_path = []
        
        for target in target_concepts:
            if len(learning_path) >= max_concepts:
                break
            
            # 获取学习序列
            sequence = self.prerequisite_graph.get_learning_sequence(
                target, user_mastery
            )
            
            # 添加到路径
            for concept_id in sequence:
                if len(learning_path) >= max_concepts:
                    break
                
                # 获取知识点信息
                concept_info = self.concept_info.get(course_id, {}).get(concept_id)
                record = self.knowledge_records[user_id].get(concept_id)
                
                learning_path.append({
                    'concept_id': concept_id,
                    'name': concept_info.name if concept_info else concept_id,
                    'description': concept_info.description if concept_info else '',
                    'difficulty': concept_info.difficulty if concept_info else 0.5,
                    'current_mastery': user_mastery.get(concept_id, 0.0),
                    'estimated_time': self._estimate_learn_time(
                        concept_info.difficulty if concept_info else 0.5,
                        user_mastery.get(concept_id, 0.0)
                    )
                })
                
                # 更新掌握度映射
                user_mastery[concept_id] = 0.5  # 假设学习后会达到0.5
        
        return learning_path
    
    def _estimate_learn_time(
        self,
        difficulty: float,
        current_mastery: float
    ) -> float:
        """
        估计学习时间
        
        Args:
            difficulty: 难度
            current_mastery: 当前掌握度
            
        Returns:
            估计学习时间（分钟）
        """
        # 基础时间
        base_time = 10  # 分钟
        
        # 根据难度调整
        difficulty_factor = 1 + difficulty * 2
        
        # 根据当前掌握度调整
        mastery_factor = 1 - current_mastery * 0.5
        
        return base_time * difficulty_factor * mastery_factor
    
    def get_weak_concepts(
        self,
        user_id: int,
        course_id: int,
        threshold: float = 0.6,
        limit: int = 5
    ) -> List[Dict]:
        """
        获取薄弱知识点
        
        Args:
            user_id: 用户ID
            course_id: 课程ID
            threshold: 阈值
            limit: 数量限制
            
        Returns:
            薄弱知识点列表
        """
        records = self.knowledge_records.get(user_id, {})
        course_concepts = self.concept_info.get(course_id, {})
        
        weak_concepts = []
        
        for concept_id, record in records.items():
            if concept_id not in course_concepts:
                continue
            
            if record.mastery_level < threshold:
                concept_info = course_concepts[concept_id]
                
                weak_concepts.append({
                    'concept_id': concept_id,
                    'name': concept_info.name,
                    'mastery': record.mastery_level,
                    'accuracy': record.get_accuracy(),
                    'practice_count': record.practice_count,
                    'recommendations': self._generate_recommendations(record, concept_info)
                })
        
        # 按掌握度排序
        weak_concepts.sort(key=lambda x: x['mastery'])
        
        return weak_concepts[:limit]
    
    def _generate_recommendations(
        self,
        record: KnowledgeStateRecord,
        concept: ConceptInfo
    ) -> List[str]:
        """
        生成改进建议
        
        Args:
            record: 知识状态记录
            concept: 知识点信息
            
        Returns:
            建议列表
        """
        recommendations = []
        
        if record.practice_count < 3:
            recommendations.append("需要更多练习来巩固基础")
        
        if record.get_accuracy() < 0.6:
            recommendations.append("建议重新学习相关的前置知识点")
        
        if concept.difficulty > 0.7:
            recommendations.append("这个知识点难度较高，可以分步骤学习")
        
        if record.last_practice and (datetime.now() - record.last_practice).days > 7:
            recommendations.append("已经有一段时间没练习了，建议复习一下")
        
        if not recommendations:
            recommendations.append("继续保持当前的学习状态")
        
        return recommendations
    
    def apply_spaced_review(
        self,
        user_id: int
    ) -> List[Tuple[str, datetime]]:
        """
        应用间隔复习
        
        Args:
            user_id: 用户ID
            
        Returns:
            复习计划
        """
        records = self.knowledge_records.get(user_id, {})
        return self.spaced_repetition.generate_review_schedule(records)
    
    def analyze_learning_patterns(
        self,
        user_id: int
    ) -> Dict:
        """
        分析学习模式
        
        Args:
            user_id: 用户ID
            
        Returns:
            学习模式分析
        """
        records = self.knowledge_records.get(user_id, {})
        
        if not records:
            return {
                'pattern': 'insufficient_data',
                'strengths': [],
                'areas_for_improvement': [],
                'learning_speed': 0.0
            }
        
        # 分析掌握度分布
        mastery_levels = [r.mastery_level for r in records.values()]
        
        # 分析练习频率
        practice_counts = [r.practice_count for r in records.values()]
        
        # 分析正确率趋势
        accuracies = [r.get_accuracy() for r in records.values() if r.total_attempts > 0]
        
        # 确定优势领域
        strengths = [
            concept_id for concept_id, record in records.items()
            if record.mastery_level >= 0.8
        ]
        
        # 确定需要改进的领域
        improvements = [
            concept_id for concept_id, record in records.items()
            if record.mastery_level < 0.6
        ]
        
        # 计算学习速度
        learning_speed = 0.0
        if records:
            total_learning_time = sum(
                (record.last_practice - record.first_exposure).total_seconds()
                for record in records.values()
                if record.first_exposure and record.last_practice
            ) / 3600  # 转换为小时
            
            total_mastery_gained = sum(
                record.mastery_level
                for record in records.values()
            )
            
            if total_learning_time > 0:
                learning_speed = total_mastery_gained / total_learning_time
        
        # 确定学习模式
        if len(strengths) > len(improvements):
            pattern = 'strong_performer'
        elif len(improvements) > len(strengths):
            pattern = 'needs_support'
        elif sum(practice_counts) / len(practice_counts) > 10:
            pattern = 'practice_oriented'
        else:
            pattern = 'balanced'
        
        return {
            'pattern': pattern,
            'strengths': strengths[:5],
            'areas_for_improvement': improvements[:5],
            'learning_speed': learning_speed,
            'average_mastery': sum(mastery_levels) / len(mastery_levels),
            'average_practice_count': sum(practice_counts) / len(practice_counts),
            'average_accuracy': sum(accuracies) / len(accuracies) if accuracies else 0.0
        }


# 全局知识追踪引擎实例
_knowledge_tracing_engine: Optional[KnowledgeTracingEngine] = None


def get_knowledge_tracing_engine() -> KnowledgeTracingEngine:
    """
    获取全局知识追踪引擎实例
    
    Returns:
        KnowledgeTracingEngine实例
    """
    global _knowledge_tracing_engine
    if _knowledge_tracing_engine is None:
        _knowledge_tracing_engine = KnowledgeTracingEngine()
    return _knowledge_tracing_engine


def record_learning_interaction(
    user_id: int,
    concept_id: str,
    course_id: int,
    correct: bool,
    response_time: float = 30,
    hints_used: int = 0
) -> Dict:
    """
    记录学习交互的便捷函数
    """
    engine = get_knowledge_tracing_engine()
    record = engine.record_interaction(
        user_id=user_id,
        concept_id=concept_id,
        course_id=course_id,
        interaction_data={
            'correct': correct,
            'response_time': response_time,
            'hints_used': hints_used
        }
    )
    return record.to_dict()


def get_learning_profile(
    user_id: int,
    course_id: int
) -> Dict:
    """
    获取学习画像的便捷函数
    """
    engine = get_knowledge_tracing_engine()
    return engine.get_user_knowledge_profile(user_id, course_id)
