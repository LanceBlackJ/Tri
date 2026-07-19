"""
USER-LLM R1 完整实现：向量存储与语义检索服务
基于文本嵌入的语义相似度检索
"""
import json
import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime

from django.contrib.auth import get_user_model

from ..models import ProfileEvent
from .embeddings import compute_embedding, cosine_similarity

logger = logging.getLogger(__name__)
User = get_user_model()


class VectorStore:
    """向量存储服务 - 管理事件嵌入向量"""
    
    # 嵌入维度
    EMBEDDING_DIM = 256
    
    @staticmethod
    def get_or_create_event_embedding(event: ProfileEvent) -> List[float]:
        """获取或创建事件的嵌入向量"""
        from ..models import EventEmbedding
        
        try:
            embedding_record = EventEmbedding.objects.get(profile_event=event)
            return embedding_record.embedding_vector
        except EventEmbedding.DoesNotExist:
            # 构建事件文本
            event_text = VectorStore._build_event_text(event)
            
            # 计算嵌入向量
            embedding = compute_embedding(event_text, dim=VectorStore.EMBEDDING_DIM)
            
            # 保存嵌入记录
            try:
                embedding_record = EventEmbedding.objects.create(
                    profile_event=event,
                    embedding_vector=embedding,
                    embedding_model='sha256_hash'
                )
            except Exception as e:
                logger.warning(f"Failed to save event embedding: {e}")
            
            return embedding
    
    @staticmethod
    def _build_event_text(event: ProfileEvent) -> str:
        """构建事件的文本表示"""
        parts = [
            f"事件类型: {event.event_type}",
            f"来源: {event.source_app}",
        ]
        
        if event.payload:
            payload_text = json.dumps(event.payload, ensure_ascii=False)
            parts.append(f"内容: {payload_text}")
        
        return " | ".join(parts)
    
    @staticmethod
    def batch_compute_embeddings(events: List[ProfileEvent]) -> Dict[int, List[float]]:
        """批量计算事件的嵌入向量"""
        result = {}
        for event in events:
            try:
                embedding = VectorStore.get_or_create_event_embedding(event)
                result[event.id] = embedding
            except Exception as e:
                logger.warning(f"Failed to compute embedding for event {event.id}: {e}")
                result[event.id] = [0.0] * VectorStore.EMBEDDING_DIM
        return result


class SemanticRetrieval:
    """语义检索服务 - 基于向量相似度检索相关事件"""
    
    # 检索参数
    DEFAULT_TOP_K = 10
    SIMILARITY_THRESHOLD = 0.3
    TIME_WINDOW_DAYS = 30
    
    @staticmethod
    def retrieve_similar_events(
        user,
        query: str,
        event_type_filter: Optional[List[str]] = None,
        top_k: int = DEFAULT_TOP_K
    ) -> List[Tuple[ProfileEvent, float]]:
        """
        基于语义相似度检索相关事件
        
        Returns:
            List of (event, similarity_score) tuples, sorted by score descending
        """
        # 1. 计算查询嵌入
        query_embedding = compute_embedding(query, dim=VectorStore.EMBEDDING_DIM)
        
        # 2. 获取时间窗口内的事件
        time_cutoff = datetime.now()
        from datetime import timedelta
        time_cutoff = time_cutoff - timedelta(days=SemanticRetrieval.TIME_WINDOW_DAYS)
        
        queryset = ProfileEvent.objects.filter(
            user=user,
            created_at__gte=time_cutoff
        )
        
        if event_type_filter:
            queryset = queryset.filter(event_type__in=event_type_filter)
        
        events = list(queryset.order_by('-created_at'))
        
        if not events:
            return []
        
        # 3. 批量获取嵌入向量
        event_embeddings = VectorStore.batch_compute_embeddings(events)
        
        # 4. 计算相似度并排序
        scored_events = []
        for event in events:
            embedding = event_embeddings.get(event.id)
            if not embedding:
                continue
            
            similarity = cosine_similarity(query_embedding, embedding)
            
            if similarity >= SemanticRetrieval.SIMILARITY_THRESHOLD:
                scored_events.append((event, similarity))
        
        # 5. 按相似度排序
        scored_events.sort(key=lambda x: x[1], reverse=True)
        
        return scored_events[:top_k]
    
    @staticmethod
    def retrieve_by_context(
        user,
        query: str,
        conversation_id: Optional[int] = None,
        course_id: Optional[int] = None
    ) -> Dict:
        """根据上下文检索相关事件"""
        result = {
            'similar_events': [],
            'conversation_events': [],
            'course_events': [],
            'recent_events': []
        }
        
        # 1. 语义相似度检索
        similar = SemanticRetrieval.retrieve_similar_events(user, query, top_k=10)
        result['similar_events'] = [
            {'event': e, 'score': s} for e, s in similar
        ]
        
        # 2. 获取同一对话的事件
        if conversation_id:
            conv_events = ProfileEvent.objects.filter(
                user=user,
                payload__conversation_id=conversation_id
            ).order_by('-created_at')[:5]
            result['conversation_events'] = list(conv_events)
        
        # 3. 获取同一课程的事件
        if course_id:
            course_events = ProfileEvent.objects.filter(
                user=user,
                course_id=course_id
            ).order_by('-created_at')[:5]
            result['course_events'] = list(course_events)
        
        # 4. 获取最近的事件
        recent = ProfileEvent.objects.filter(user=user).order_by('-created_at')[:10]
        result['recent_events'] = list(recent)
        
        return result


class EventAggregator:
    """事件聚合服务 - 将检索到的事件聚合为上下文"""
    
    @staticmethod
    def aggregate_for_inference(
        similar_events: List[Tuple[ProfileEvent, float]],
        conversation_events: List[ProfileEvent],
        recent_events: List[ProfileEvent]
    ) -> Dict:
        """聚合事件用于LLM推理"""
        
        # 1. 高相似度事件优先
        high_priority = [
            {'event': e, 'score': s, 'priority': 'high'}
            for e, s in similar_events if s > 0.6
        ]
        
        # 2. 中等相似度事件
        medium_priority = [
            {'event': e, 'score': s, 'priority': 'medium'}
            for e, s in similar_events if 0.4 <= s <= 0.6
        ]
        
        # 3. 对话上下文事件
        context_events = [
            {'event': e, 'score': 0.5, 'priority': 'context'}
            for e in conversation_events
        ]
        
        # 4. 最近事件作为补充
        recent_context = [
            {'event': e, 'score': 0.3, 'priority': 'recent'}
            for e in recent_events[:5]
        ]
        
        # 5. 合并并限制总数
        all_events = high_priority + medium_priority + context_events + recent_context
        all_events = all_events[:15]  # 最多15个事件
        
        return {
            'events': all_events,
            'total_count': len(all_events),
            'high_priority_count': len(high_priority),
            'medium_priority_count': len(medium_priority)
        }
    
    @staticmethod
    def build_inference_context(aggregated: Dict) -> str:
        """构建用于LLM推理的上下文文本"""
        lines = ["【用户行为历史】"]
        
        priority_labels = {
            'high': '🔴 高相关',
            'medium': '🟡 中相关',
            'context': '💬 对话上下文',
            'recent': '📅 最近活动'
        }
        
        current_priority = None
        for item in aggregated['events']:
            event = item['event']
            priority = item['priority']
            
            # 添加优先级分隔
            if priority != current_priority:
                lines.append(f"\n{priority_labels.get(priority, priority)}:")
                current_priority = priority
            
            # 构建事件描述
            time_str = event.created_at.strftime('%m-%d %H:%M')
            event_desc = EventAggregator._format_event(event)
            score_str = f"[{item['score']:.2f}]"
            
            lines.append(f"  {time_str} {score_str} {event_desc}")
        
        return '\n'.join(lines)
    
    @staticmethod
    def _format_event(event: ProfileEvent) -> str:
        """格式化单个事件为可读文本"""
        parts = [f"类型={event.event_type}"]
        
        if event.payload:
            # 提取关键信息
            payload = event.payload
            if 'text' in payload:
                text = str(payload['text'])[:50]
                parts.append(f"内容={text}...")
            if 'question_type' in payload:
                parts.append(f"问题类型={payload['question_type']}")
            if 'confidence' in payload:
                parts.append(f"置信度={payload['confidence']}")
        
        return ' | '.join(parts)
