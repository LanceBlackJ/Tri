"""
USER-LLM R1: 完整实现
基于论文的动态画像推理引擎
集成：向量检索 + 多模态处理 + 迭代推理 + CoT验证
"""
import logging
from typing import Dict
from datetime import datetime

from django.contrib.auth import get_user_model

from ..models import StudentProfile, ProfileEvent, ReasoningChain, Message
from .xinghuo_client import XinghuoClient
from .user_llm_r1_vector import VectorStore, SemanticRetrieval, EventAggregator
from .user_llm_r1_multimodal import MultimodalProcessor
from .user_llm_r1_iterative import IterativeReasoning, ReasoningChainBuilder

logger = logging.getLogger(__name__)
User = get_user_model()


class USERLLM_R1_Full:
    """
    USER-LLM R1 完整实现
    论文核心功能：
    1. User Encoder - 用户编码器
    2. RAG Engine - RAG检索引擎（向量检索）
    3. VLM Init - 多模态处理（冷启动）
    4. CoT Reasoning - 链式思维推理
    5. Profile Validator - 画像验证器
    6. Iterative Optimization - 迭代优化
    """
    
    def __init__(self):
        self.vector_store = VectorStore()
        self.semantic_retrieval = SemanticRetrieval()
        self.multimodal_processor = MultimodalProcessor()
        self.iterative_reasoning = IterativeReasoning()
        self.client = XinghuoClient()
    
    def process_interaction(
        self,
        user,
        query: str,
        context: Dict = None,
        multimodal_input: Dict = None
    ) -> Dict:
        """
        处理用户交互的完整流程
        
        Args:
            user: 用户对象
            query: 用户查询文本
            context: 额外上下文（conversation_id, course_id等）
            multimodal_input: 多模态输入 {'image': base64_data, ...}
        """
        context = context or {}
        multimodal_input = multimodal_input or {}
        
        start_time = datetime.now()
        
        try:
            # ========== Step 1: 用户编码 ==========
            user_encoding = self._encode_user(user)
            
            # ========== Step 2: 多模态处理 ==========
            multimodal_result = {}
            if multimodal_input:
                multimodal_result = self._process_multimodal(user, query, multimodal_input, context)
            
            # ========== Step 3: 向量语义检索 ==========
            retrieval_result = self._semantic_retrieve(user, query, context)
            
            # ========== Step 4: 构建推理上下文 ==========
            inference_context = self._build_inference_context(
                user, query, context, retrieval_result, multimodal_result
            )
            
            # ========== Step 5: 获取当前画像 ==========
            current_profile = self._get_current_profile(user)
            
            # ========== Step 6: 迭代推理 ==========
            iterative_result = self.iterative_reasoning.run_iterative_reasoning(
                user=user,
                query=query,
                initial_context=inference_context,
                current_profile=current_profile
            )
            
            # ========== Step 7: CoT验证 ==========
            validated_delta = self._validate_with_cot(
                user, query, iterative_result['final_delta'], current_profile
            )
            
            # ========== Step 8: 更新画像 ==========
            update_success = self._update_profile(
                user, validated_delta, iterative_result.get('best_result', {})
            )
            
            # ========== Step 9: 保存推理链 ==========
            reasoning_chain = self._save_reasoning_chain(
                user=user,
                query=query,
                context=context,
                iterative_result=iterative_result,
                validated_delta=validated_delta,
                multimodal_result=multimodal_result
            )
            
            # 计算总耗时
            elapsed_time = (datetime.now() - start_time).total_seconds()
            
            return {
                'success': update_success,
                'profile_delta': validated_delta,
                'confidence': iterative_result.get('best_result', {}).get('confidence', 0.0),
                'iterations': iterative_result.get('total_iterations', 0),
                'converged': iterative_result.get('converged', False),
                'reasoning_chain': reasoning_chain,
                'user_encoding': user_encoding,
                'multimodal_analysis': multimodal_result,
                'retrieval_info': {
                    'similar_events': len(retrieval_result.get('similar_events', [])),
                    'total_events': retrieval_result.get('total_events', 0)
                },
                'elapsed_time': elapsed_time,
                'is_cold_start': user_encoding.get('is_cold_start', True)
            }
            
        except Exception as e:
            logger.error(f"USER-LLM R1 processing failed: {str(e)}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
                'profile_delta': {},
                'confidence': 0.0
            }
    
    def _encode_user(self, user) -> Dict:
        """Step 1: 用户编码"""
        profile = StudentProfile.objects.filter(user=user).first()
        
        if not profile:
            return {
                'is_cold_start': True,
                'cold_start_progress': 0.0,
                'profile_version': 0,
                'encoded_features': self._get_cold_start_encoding()
            }
        
        return {
            'is_cold_start': profile.is_cold_start,
            'cold_start_progress': profile.cold_start_progress,
            'profile_version': profile.profile_version,
            'last_inference_time': profile.last_inference_time.isoformat() if profile.last_inference_time else None,
            'encoded_features': {
                'knowledge_vector': profile.knowledge_profile,
                'cognitive_style': profile.cognitive_style or 'unknown',
                'learning_goals': profile.learning_goals or [],
                'preferences': profile.learning_preferences or {},
                'engagement_score': profile.engagement.get('score', 0) if profile.engagement else 0
            }
        }
    
    def _get_cold_start_encoding(self) -> Dict:
        """获取冷启动编码"""
        return {
            'knowledge_vector': {},
            'cognitive_style': 'unknown',
            'learning_goals': [],
            'preferences': {},
            'engagement_score': 0.0
        }
    
    def _process_multimodal(
        self,
        user,
        query: str,
        multimodal_input: Dict,
        context: Dict
    ) -> Dict:
        """Step 2: 多模态处理"""
        result = {
            'has_multimodal': False,
            'image_analysis': None,
            'profile_hints': []
        }
        
        if multimodal_input.get('image'):
            result['has_multimodal'] = True
            result['image_analysis'] = self.multimodal_processor.process_image_input(
                image_data=multimodal_input['image'],
                context=query
            )
            
            # 提取画像线索
            if result['image_analysis'].get('learning_indicators'):
                result['profile_hints'] = result['image_analysis']['learning_indicators']
        
        return result
    
    def _semantic_retrieve(
        self,
        user,
        query: str,
        context: Dict
    ) -> Dict:
        """Step 3: 向量语义检索"""
        conversation_id = context.get('conversation_id')
        course_id = context.get('course_id')
        
        # 使用语义检索
        retrieval_result = self.semantic_retrieval.retrieve_by_context(
            user=user,
            query=query,
            conversation_id=conversation_id,
            course_id=course_id
        )
        
        # 聚合事件
        aggregated = EventAggregator.aggregate_for_inference(
            similar_events=retrieval_result.get('similar_events', []),
            conversation_events=retrieval_result.get('conversation_events', []),
            recent_events=retrieval_result.get('recent_events', [])
        )
        
        # 构建历史上下文
        history_context = EventAggregator.build_inference_context(aggregated)
        
        return {
            'similar_events': retrieval_result.get('similar_events', []),
            'conversation_events': retrieval_result.get('conversation_events', []),
            'recent_events': retrieval_result.get('recent_events', []),
            'aggregated': aggregated,
            'history_context': history_context,
            'total_events': len(aggregated.get('events', []))
        }
    
    def _build_inference_context(
        self,
        user,
        query: str,
        context: Dict,
        retrieval_result: Dict,
        multimodal_result: Dict
    ) -> Dict:
        """Step 4: 构建推理上下文"""
        
        # 构建对话上下文
        conversation_context = ""
        if context.get('conversation_id'):
            messages = Message.objects.filter(
                conversation_id=context['conversation_id']
            ).order_by('-created_at')[:5]
            
            msg_parts = []
            for msg in reversed(messages):
                role = '用户' if msg.role == 'student' else 'AI'
                msg_parts.append(f"{role}: {msg.content[:100]}")
            
            if msg_parts:
                conversation_context = '\n'.join(msg_parts)
        
        return {
            'query': query,
            'history_context': retrieval_result.get('history_context', ''),
            'conversation_context': conversation_context,
            'multimodal_context': multimodal_result,
            'course_id': context.get('course_id'),
            'material_id': context.get('material_id'),
            'conversation_id': context.get('conversation_id'),
            'history_events_count': retrieval_result.get('total_events', 0)
        }
    
    def _get_current_profile(self, user) -> Dict:
        """Step 5: 获取当前用户画像"""
        profile = StudentProfile.objects.filter(user=user).first()
        
        if not profile:
            return {}
        
        return {
            'cognitive_style': profile.cognitive_style,
            'learning_goals': profile.learning_goals or [],
            'learning_preferences': profile.learning_preferences or {},
            'knowledge_profile': profile.knowledge_profile or {},
            'misconceptions': profile.misconceptions or [],
            'engagement': profile.engagement or {}
        }
    
    def _validate_with_cot(
        self,
        user,
        query: str,
        delta: Dict,
        current_profile: Dict
    ) -> Dict:
        """Step 7: CoT验证"""
        # 意图分析
        intent = self._analyze_intent(query)
        
        # 一致性检查
        validation = self._validate_consistency(delta, current_profile)
        
        validated = delta.copy()
        validated['_validation'] = validation
        
        # 如果验证失败，清除部分delta
        if not validation.get('is_valid', True):
            # 只保留高置信度的部分
            if validation.get('issues'):
                logger.warning(f"Validation issues: {validation['issues']}")
        
        return validated
    
    def _analyze_intent(self, query: str) -> Dict:
        """分析用户意图"""
        intent_keywords = {
            'question': ['什么', '为什么', '怎么', '如何', '哪个', '谁', '哪里'],
            'statement': ['我觉得', '我认为', '我想', '我喜欢', '我希望'],
            'request': ['帮我', '请', '能否', '可以', '需要'],
            'confusion': ['不懂', '不会', '困惑', '不明白', '不清楚']
        }
        
        scores = {}
        for intent, keywords in intent_keywords.items():
            scores[intent] = sum(1 for kw in keywords if kw in query)
        
        primary = max(scores, key=scores.get) if scores else 'statement'
        confidence = min(1.0, scores[primary] / 3.0 + 0.5) if scores.get(primary) else 0.5
        
        return {
            'intent': primary,
            'confidence': confidence,
            'scores': scores
        }
    
    def _validate_consistency(
        self,
        delta: Dict,
        current_profile: Dict
    ) -> Dict:
        """验证画像一致性"""
        issues = []
        is_valid = True
        
        # 检查认知风格变化
        if 'cognitive_style' in delta:
            old = current_profile.get('cognitive_style')
            new = delta['cognitive_style']
            if old and new and old != new:
                # 需要多次证据
                issues.append({
                    'dimension': 'cognitive_style',
                    'message': f'认知风格从 {old} 变为 {new} 需要更多证据支持'
                })
        
        # 检查学习目标冲突
        if 'learning_goals' in delta:
            new_goals = set(delta['learning_goals'])
            old_goals = set(current_profile.get('learning_goals', []))
            
            # 检查是否有矛盾的目标
            for goal in new_goals:
                if ('不' in goal or '不要' in goal) and goal.replace('不', '').replace('不要', '') in old_goals:
                    issues.append({
                        'dimension': 'learning_goals',
                        'message': f'目标 {goal} 与现有目标冲突'
                    })
                    is_valid = False
        
        # 检查偏好一致性
        if 'learning_preferences' in delta:
            old_prefs = current_profile.get('learning_preferences', {})
            new_prefs = delta['learning_preferences']
            
            for key in ['preferred_format', 'preferred_mode']:
                if old_prefs.get(key) and new_prefs.get(key):
                    if old_prefs[key] != new_prefs[key]:
                        issues.append({
                            'dimension': 'learning_preferences',
                            'message': f'偏好 {key} 发生变化: {old_prefs[key]} -> {new_prefs[key]}'
                        })
        
        return {
            'is_valid': is_valid,
            'issues': issues
        }
    
    def _update_profile(
        self,
        user,
        delta: Dict,
        inference_result: Dict
    ) -> bool:
        """Step 8: 更新画像"""
        try:
            profile, _ = StudentProfile.objects.get_or_create(user=user)
            
            # 移除验证元数据
            delta_to_save = {k: v for k, v in delta.items() if not k.startswith('_')}
            
            if not delta_to_save:
                return False
            
            # 更新画像
            confidence = inference_result.get('confidence', 0.5)
            profile.update_from_dict(delta_to_save, save=False)
            
            # 更新USER-LLM R1字段
            profile.profile_version += 1
            profile.last_inference_time = datetime.now()
            profile.inference_confidence = confidence
            
            # 更新冷启动状态
            if profile.is_cold_start:
                profile.cold_start_progress = min(1.0, profile.cold_start_progress + 0.1)
                if profile.cold_start_progress >= 0.8:
                    profile.is_cold_start = False
            
            # 更新最近推理记录
            recent = list(profile.recent_inferences or [])
            recent.append({
                'timestamp': datetime.now().isoformat(),
                'confidence': confidence,
                'delta_keys': list(delta_to_save.keys())
            })
            profile.recent_inferences = recent[-10:]
            
            profile.save()
            
            # 同时创建ProfileEvent记录
            self._create_profile_event(user, delta_to_save, confidence)
            
            return True
            
        except Exception as e:
            logger.error(f"Profile update failed: {str(e)}")
            return False
    
    def _create_profile_event(
        self,
        user,
        delta: Dict,
        confidence: float
    ) -> None:
        """创建画像事件记录"""
        try:
            event_types = []
            
            if delta.get('cognitive_style'):
                event_types.append('cognitive_style_change')
            if delta.get('learning_goals'):
                event_types.append('learning_goal_update')
            if delta.get('learning_preferences'):
                event_types.append('preference_update')
            if delta.get('knowledge_profile'):
                event_types.append('knowledge_update')
            
            for event_type in event_types:
                ProfileEvent.objects.create(
                    user=user,
                    event_type=event_type,
                    source_app='user_llm_r1',
                    confidence=confidence,
                    payload=delta,
                    profile_delta=delta
                )
        except Exception as e:
            logger.warning(f"Failed to create profile event: {e}")
    
    def _save_reasoning_chain(
        self,
        user,
        query: str,
        context: Dict,
        iterative_result: Dict,
        validated_delta: Dict,
        multimodal_result: Dict
    ) -> Dict:
        """Step 9: 保存推理链"""
        try:
            # 构建完整的推理链
            chain_data = ReasoningChainBuilder.build_chain(
                query=query,
                context=context,
                iterations=iterative_result.get('iterations', []),
                final_delta=validated_delta,
                confidence=iterative_result.get('best_result', {}).get('confidence', 0.0)
            )
            
            # 创建记录
            profile = StudentProfile.objects.filter(user=user).first()
            
            chain = ReasoningChain.objects.create(
                user=user,
                query=query,
                conversation_id=context.get('conversation_id'),
                reasoning_steps=chain_data.get('steps', []) + iterative_result.get('iterations', []),
                profile_delta=validated_delta,
                confidence=iterative_result.get('best_result', {}).get('confidence', 0.0),
                validation_result={
                    'iterations': iterative_result.get('total_iterations', 0),
                    'converged': iterative_result.get('converged', False),
                    'multimodal_used': multimodal_result.get('has_multimodal', False)
                },
                profile_version=profile.profile_version if profile else 1
            )
            
            return {
                'chain_id': chain.id,
                'steps_count': len(chain_data.get('steps', []))
            }
            
        except Exception as e:
            logger.error(f"Failed to save reasoning chain: {str(e)}")
            return {'chain_id': None, 'steps_count': 0}


# 向后兼容：单例实例。调用方按实例用（USERLLM_R1.process_interaction(user, ...)），
# 之前误绑成类本身，导致 user 被当作 self、self._encode_user 报 AttributeError。
USERLLM_R1 = USERLLM_R1_Full()
