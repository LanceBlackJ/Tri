"""
AKT (Attentive Knowledge Tracing) 知识追踪模块

基于注意力机制的知识追踪模型，用于追踪学生对知识点的掌握程度。
参考论文：Attentive Knowledge Tracing (AKT), AAAI 2020

核心思想：
1. 使用注意力机制计算历史交互对当前知识点的影响
2. 考虑时间衰减效应，最近的交互影响更大
3. 提供可解释的知识掌握度估计
"""

import math
import time
from typing import Dict, List, Optional


class KnowledgeConcept:
    """知识点模型"""
    
    def __init__(self, concept_id: str, name: str = ""):
        self.concept_id = concept_id
        self.name = name or concept_id
        self.mastery_probability = 0.0  # 掌握概率 [0, 1]
        self.interaction_count = 0
        self.correct_count = 0
        self.last_interaction_time = 0
        self.interaction_history: List[Dict] = []
    
    def to_dict(self) -> Dict:
        return {
            'concept_id': self.concept_id,
            'name': self.name,
            'mastery_probability': round(self.mastery_probability, 3),
            'interaction_count': self.interaction_count,
            'correct_count': self.correct_count,
            'accuracy': round(self.correct_count / max(self.interaction_count, 1), 3),
            'last_interaction_time': self.last_interaction_time,
        }


class AKTKnowledgeTracer:
    """
    AKT知识追踪器
    
    简化版AKT模型，使用注意力机制和贝叶斯更新来追踪知识掌握度。
    """
    
    def __init__(self):
        self.concepts: Dict[str, KnowledgeConcept] = {}
        self.global_forget_rate = 0.01  # 全局遗忘率
        self.time_decay_factor = 0.5  # 时间衰减因子
        
        # BKT风格参数（用于冷启动）
        self.prior_knowledge = 0.1  # 初始掌握概率
        self.learning_rate = 0.3  # 首次尝试学习概率
        self.guess_rate = 0.2  # 猜测正确概率
        self.slip_rate = 0.1  # 粗心错误概率
    
    def add_concept(self, concept_id: str, name: str = "") -> KnowledgeConcept:
        """添加新的知识点"""
        if concept_id not in self.concepts:
            self.concepts[concept_id] = KnowledgeConcept(concept_id, name)
        return self.concepts[concept_id]
    
    def record_interaction(
        self,
        concept_id: str,
        is_correct: bool,
        timestamp: Optional[float] = None,
        difficulty: float = 0.5,
        hint_used: bool = False
    ) -> Dict:
        """
        记录学生与知识点的交互
        
        参数:
            concept_id: 知识点ID
            is_correct: 是否回答正确
            timestamp: 交互时间戳（默认当前时间）
            difficulty: 题目难度 [0, 1]
            hint_used: 是否使用了提示
        
        返回:
            更新后的知识掌握度信息
        """
        timestamp = timestamp or time.time()
        concept = self.add_concept(concept_id)
        
        # 应用时间衰减（遗忘曲线）
        self._apply_time_decay(concept, timestamp)
        
        # 计算注意力权重（基于时间距离和交互质量）
        attention_weight = self._compute_attention_weight(concept, timestamp)
        
        # 使用AKT风格更新掌握概率
        old_mastery = concept.mastery_probability
        concept.mastery = self._update_mastery_akt(
            concept=concept,
            is_correct=is_correct,
            attention_weight=attention_weight,
            difficulty=difficulty,
            hint_used=hint_used
        )
        
        # 更新交互统计
        concept.interaction_count += 1
        if is_correct:
            concept.correct_count += 1
        concept.last_interaction_time = timestamp
        
        # 记录交互历史
        concept.interaction_history.append({
            'timestamp': timestamp,
            'is_correct': is_correct,
            'difficulty': difficulty,
            'hint_used': hint_used,
            'mastery_before': old_mastery,
            'mastery_after': concept.mastery_probability,
            'attention_weight': attention_weight,
        })
        
        return {
            'concept_id': concept_id,
            'mastery_probability': round(concept.mastery_probability, 3),
            'mastery_change': round(concept.mastery_probability - old_mastery, 3),
            'interaction_count': concept.interaction_count,
            'accuracy': round(concept.correct_count / concept.interaction_count, 3),
        }
    
    def _apply_time_decay(self, concept: KnowledgeConcept, current_time: float):
        """应用时间衰减（遗忘曲线）"""
        if concept.last_interaction_time > 0:
            time_diff = current_time - concept.last_interaction_time
            hours_diff = time_diff / 3600.0  # 转换为小时
            
            # Ebbinghaus遗忘曲线简化版
            decay = math.exp(-self.global_forget_rate * hours_diff)
            
            # 掌握度向初始值衰减
            concept.mastery_probability = (
                concept.mastery_probability * decay +
                self.prior_knowledge * (1 - decay)
            )
    
    def _compute_attention_weight(
        self,
        concept: KnowledgeConcept,
        current_time: float
    ) -> float:
        """
        计算注意力权重
        
        基于AKT论文的注意力机制：
        1. 时间距离越近，权重越大
        2. 交互质量越高（无提示、高难度），权重越大
        """
        if not concept.interaction_history:
            return 1.0
        
        # 时间衰减权重
        time_weights = []
        for interaction in concept.interaction_history[-10:]:  # 最近10次交互
            time_diff = current_time - interaction['timestamp']
            hours_diff = time_diff / 3600.0
            
            # 指数时间衰减
            w_time = math.exp(-self.time_decay_factor * hours_diff)
            time_weights.append(w_time)
        
        # 归一化
        total_weight = sum(time_weights)
        if total_weight > 0:
            return time_weights[-1] / total_weight
        return 1.0
    
    def _update_mastery_akt(
        self,
        concept: KnowledgeConcept,
        is_correct: bool,
        attention_weight: float,
        difficulty: float,
        hint_used: bool
    ) -> float:
        """
        AKT风格掌握度更新
        
        结合贝叶斯更新和注意力机制：
        1. 如果回答正确，掌握度增加（考虑难度和注意力）
        2. 如果回答错误，掌握度减少（考虑是否使用提示）
        """
        current_mastery = concept.mastery_probability
        
        # 基础学习率（考虑注意力权重）
        effective_learning_rate = self.learning_rate * (1 + attention_weight)
        
        if is_correct:
            # 回答正确：掌握度增加
            # 难度越高，增加越多
            difficulty_bonus = difficulty * 0.2
            
            # 使用提示会减少掌握度增加
            hint_penalty = 0.3 if hint_used else 0.0
            
            # 掌握度增加量
            increase = (
                effective_learning_rate * (1 - current_mastery) *
                (1 + difficulty_bonus - hint_penalty)
            )
            
            new_mastery = current_mastery + increase
            
        else:
            # 回答错误：掌握度减少
            # 如果使用了提示，减少更多（说明真的不会）
            hint_factor = 1.5 if hint_used else 1.0
            
            # 掌握度减少量
            decrease = (
                self.slip_rate * current_mastery *
                attention_weight * hint_factor
            )
            
            new_mastery = current_mastery - decrease
        
        # 限制在 [0, 1] 范围内
        return max(0.0, min(1.0, new_mastery))
    
    def get_mastery_summary(self) -> Dict:
        """获取所有知识点的掌握度摘要"""
        if not self.concepts:
            return {
                'total_concepts': 0,
                'mastered_concepts': 0,
                'learning_concepts': 0,
                'new_concepts': 0,
                'average_mastery': 0.0,
                'concepts': [],
            }
        
        mastered = 0
        learning = 0
        new_concepts = 0
        total_mastery = 0.0
        
        concept_list = []
        for concept in self.concepts.values():
            mastery = concept.mastery_probability
            total_mastery += mastery
            
            if mastery >= 0.8:
                mastered += 1
                status = 'mastered'
            elif mastery >= 0.3:
                learning += 1
                status = 'learning'
            else:
                new_concepts += 1
                status = 'new'
            
            concept_list.append({
                **concept.to_dict(),
                'status': status,
            })
        
        return {
            'total_concepts': len(self.concepts),
            'mastered_concepts': mastered,
            'learning_concepts': learning,
            'new_concepts': new_concepts,
            'average_mastery': round(total_mastery / len(self.concepts), 3),
            'concepts': sorted(concept_list, key=lambda x: x['mastery_probability']),
        }
    
    def get_recommendations(self) -> List[Dict]:
        """
        基于掌握度生成学习建议
        
        返回需要重点关注的知识点列表
        """
        recommendations = []
        
        for concept in self.concepts.values():
            mastery = concept.mastery_probability
            
            # 优先推荐掌握度在 0.3-0.7 的知识点（正在学习中）
            if 0.3 <= mastery < 0.7:
                priority = 'high'
                action = '重点练习'
            elif mastery < 0.3:
                priority = 'medium'
                action = '重新学习'
            elif mastery < 0.8:
                priority = 'low'
                action = '巩固练习'
            else:
                continue  # 已掌握的知识点不推荐
            
            # 计算建议的练习次数
            recent_accuracy = 0.0
            if concept.interaction_history:
                recent = concept.interaction_history[-5:]
                recent_accuracy = sum(1 for i in recent if i['is_correct']) / len(recent)
            
            recommendations.append({
                'concept_id': concept.concept_id,
                'name': concept.name,
                'mastery_probability': round(mastery, 3),
                'priority': priority,
                'action': action,
                'recent_accuracy': round(recent_accuracy, 3),
                'interaction_count': concept.interaction_count,
            })
        
        # 按优先级排序
        priority_order = {'high': 0, 'medium': 1, 'low': 2}
        recommendations.sort(key=lambda x: priority_order[x['priority']])
        
        return recommendations
    
    def to_dict(self) -> Dict:
        """导出完整状态"""
        return {
            'concepts': {cid: c.to_dict() for cid, c in self.concepts.items()},
            'global_forget_rate': self.global_forget_rate,
            'time_decay_factor': self.time_decay_factor,
            'summary': self.get_mastery_summary(),
            'recommendations': self.get_recommendations(),
        }


# 全局知识追踪器实例（可按用户隔离）
_tracers: Dict[str, AKTKnowledgeTracer] = {}


def get_tracer(user_id: str) -> AKTKnowledgeTracer:
    """获取用户的知识追踪器"""
    if user_id not in _tracers:
        _tracers[user_id] = AKTKnowledgeTracer()
    return _tracers[user_id]


def record_interaction(
    user_id: str,
    concept_id: str,
    is_correct: bool,
    **kwargs
) -> Dict:
    """记录用户交互的便捷函数"""
    tracer = get_tracer(user_id)
    return tracer.record_interaction(concept_id, is_correct, **kwargs)


def get_mastery_summary(user_id: str) -> Dict:
    """获取用户掌握度摘要的便捷函数"""
    tracer = get_tracer(user_id)
    return tracer.get_mastery_summary()
