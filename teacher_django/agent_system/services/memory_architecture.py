"""
记忆架构模块 - 基于论文 arXiv:2505.19803v2 和 arXiv:2503.11733v2

论文核心实现：
- 情景记忆 (Episodic Memory)
- 短期记忆 (Short-term Memory)
- 长期记忆 (Long-term Memory)
- 工作记忆 (Working Memory)
- LangChain风格的记忆管理
"""

import logging
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict

logger = logging.getLogger(__name__)


class MemoryType(Enum):
    """
    记忆类型
    """
    EPISODIC = "episodic"           # 情景记忆：特定事件和经历
    SEMANTIC = "semantic"           # 语义记忆：概念和知识
    PROCEDURAL = "procedural"       # 程序记忆：技能和操作
    WORKING = "working"            # 工作记忆：当前任务信息
    SHORT_TERM = "short_term"      # 短期记忆：会话内信息
    LONG_TERM = "long_term"        # 长期记忆：持久化信息


class MemoryImportance(Enum):
    """
    记忆重要性等级
    """
    CRITICAL = 5   # 关键：影响学习成果的重大事件
    HIGH = 4      # 高：重要的学习里程碑
    MEDIUM = 3    # 中：一般学习交互
    LOW = 2       # 低：常规信息
    MINIMAL = 1   # 最小：可忽略的细节


@dataclass
class MemoryItem:
    """
    记忆项
    
    记忆的基本单元，包含记忆内容和元数据
    """
    # 基本信息
    memory_id: str
    user_id: int
    memory_type: MemoryType
    
    # 内容
    content: str
    content_embedding: Optional[List[float]] = None
    
    # 元数据
    timestamp: datetime = field(default_factory=datetime.now)
    importance: MemoryImportance = MemoryImportance.MEDIUM
    
    # 关联信息
    context: Dict = field(default_factory=dict)  # 上下文信息
    emotions: List[str] = field(default_factory=list)  # 关联情感
    concepts: List[str] = field(default_factory=list)  # 关联概念
    
    # 访问信息
    access_count: int = 0
    last_accessed: datetime = field(default_factory=datetime.now)
    
    # 衰减信息
    decay_factor: float = 1.0  # 衰减因子，逐渐降低
    created_at: datetime = field(default_factory=datetime.now)
    
    # 关联
    related_memories: List[str] = field(default_factory=list)  # 相关记忆ID
    
    def to_dict(self) -> Dict:
        return {
            'memory_id': self.memory_id,
            'user_id': self.user_id,
            'memory_type': self.memory_type.value,
            'content': self.content,
            'timestamp': self.timestamp.isoformat(),
            'importance': self.importance.value,
            'context': self.context,
            'emotions': self.emotions,
            'concepts': self.concepts,
            'access_count': self.access_count,
            'last_accessed': self.last_accessed.isoformat(),
            'decay_factor': self.decay_factor,
            'created_at': self.created_at.isoformat(),
            'related_memories': self.related_memories
        }
    
    def update_access(self):
        """更新访问信息"""
        self.access_count += 1
        self.last_accessed = datetime.now()
        # 访问可以稍微恢复衰减
        self.decay_factor = min(self.decay_factor + 0.1, 1.0)
    
    def calculate_relevance(
        self,
        query: str,
        time_weight: float = 0.3,
        importance_weight: float = 0.3,
        access_weight: float = 0.2,
        content_weight: float = 0.2
    ) -> float:
        """
        计算记忆与查询的相关性分数
        
        综合考虑时间衰减、重要性、访问频率、内容匹配
        
        Args:
            query: 查询字符串
            time_weight: 时间权重
            importance_weight: 重要性权重
            access_weight: 访问权重
            content_weight: 内容权重
            
        Returns:
            相关性分数 (0.0 - 1.0)
        """
        # 时间衰减分数
        time_diff = (datetime.now() - self.timestamp).total_seconds()
        days = time_diff / 86400  # 转换为天
        time_score = max(0.1, 1.0 - days * 0.05)  # 每天衰减5%
        
        # 重要性分数
        importance_score = self.importance.value / 5.0
        
        # 访问频率分数
        access_score = min(self.access_count / 10, 1.0)  # 最多10次访问
        
        # 内容匹配分数
        content_score = 0.5  # 默认
        if query:
            query_lower = query.lower()
            content_lower = self.content.lower()
            if query_lower in content_lower:
                content_score = 1.0
            elif any(word in content_lower for word in query_lower.split()):
                content_score = 0.7
        
        # 综合分数
        total_score = (
            time_weight * time_score +
            importance_weight * importance_score +
            access_weight * access_score +
            content_weight * content_score
        ) * self.decay_factor
        
        return min(total_score, 1.0)


@dataclass
class ConversationContext:
    """
    对话上下文
    
    存储当前会话的相关信息，用于工作记忆
    """
    session_id: str
    user_id: int
    start_time: datetime = field(default_factory=datetime.now)
    
    # 对话内容
    messages: List[Dict] = field(default_factory=list)
    
    # 当前任务
    current_task: Optional[str] = None
    task_progress: float = 0.0
    
    # 当前主题
    current_topic: Optional[str] = None
    topic_history: List[str] = field(default_factory=list)
    
    # 当前情感状态
    current_emotion: Optional[str] = None
    emotion_history: List[str] = field(default_factory=list)
    
    # 参与度
    engagement_level: float = 0.5
    
    # 待处理事项
    pending_questions: List[str] = field(default_factory=list)
    resolved_items: List[str] = field(default_factory=list)
    
    def add_message(self, role: str, content: str):
        """添加消息"""
        self.messages.append({
            'role': role,
            'content': content,
            'timestamp': datetime.now().isoformat()
        })
    
    def get_recent_messages(self, count: int = 10) -> List[Dict]:
        """获取最近的消息"""
        return self.messages[-count:]
    
    def get_context_summary(self) -> str:
        """获取上下文摘要"""
        if not self.messages:
            return "暂无对话历史"
        
        recent = self.get_recent_messages(5)
        summary = f"当前会话（{len(self.messages)}条消息）"
        
        if self.current_topic:
            summary += f"，主题：{self.current_topic}"
        
        if self.current_task:
            summary += f"，任务：{self.current_task}（{int(self.task_progress * 100)}%）"
        
        return summary
    
    def to_dict(self) -> Dict:
        return {
            'session_id': self.session_id,
            'user_id': self.user_id,
            'start_time': self.start_time.isoformat(),
            'messages': self.messages,
            'current_task': self.current_task,
            'task_progress': self.task_progress,
            'current_topic': self.current_topic,
            'topic_history': self.topic_history,
            'current_emotion': self.current_emotion,
            'emotion_history': self.emotion_history,
            'engagement_level': self.engagement_level,
            'pending_questions': self.pending_questions,
            'resolved_items': self.resolved_items
        }


class ShortTermMemory:
    """
    短期记忆管理器
    
    管理当前会话的临时信息，具有自动衰减机制
    """
    
    # 短期记忆配置
    DEFAULT_TTL = 3600  # 默认存活时间（秒）：1小时
    MAX_ITEMS = 100      # 最大记忆项数
    CLEANUP_INTERVAL = 300  # 清理间隔（秒）
    
    def __init__(self, ttl: int = None, max_items: int = None):
        """
        初始化短期记忆管理器
        
        Args:
            ttl: 记忆存活时间（秒）
            max_items: 最大记忆项数
        """
        self.ttl = ttl or self.DEFAULT_TTL
        self.max_items = max_items or self.MAX_ITEMS
        
        # 存储结构：{user_id: {memory_id: MemoryItem}}
        self.memories: Dict[int, Dict[str, MemoryItem]] = defaultdict(dict)
        
        # 最后清理时间
        self.last_cleanup = datetime.now()
    
    def store(
        self,
        user_id: int,
        content: str,
        memory_type: MemoryType = MemoryType.SHORT_TERM,
        context: Optional[Dict] = None,
        importance: MemoryImportance = MemoryImportance.MEDIUM,
        memory_id: Optional[str] = None
    ) -> MemoryItem:
        """
        存储短期记忆
        
        Args:
            user_id: 用户ID
            content: 记忆内容
            memory_type: 记忆类型
            context: 上下文信息
            importance: 重要性
            memory_id: 指定记忆ID
            
        Returns:
            MemoryItem: 创建的记忆项
        """
        # 生成记忆ID
        if not memory_id:
            memory_id = self._generate_memory_id(user_id, content)
        
        # 创建记忆项
        memory = MemoryItem(
            memory_id=memory_id,
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            context=context or {},
            importance=importance
        )
        
        # 存储
        self.memories[user_id][memory_id] = memory
        
        # 检查是否需要清理
        self._maybe_cleanup(user_id)
        
        return memory
    
    def retrieve(
        self,
        user_id: int,
        memory_id: str
    ) -> Optional[MemoryItem]:
        """
        检索特定记忆
        
        Args:
            user_id: 用户ID
            memory_id: 记忆ID
            
        Returns:
            MemoryItem或None
        """
        memory = self.memories.get(user_id, {}).get(memory_id)
        
        if memory:
            # 检查是否过期
            if self._is_expired(memory):
                self.remove(user_id, memory_id)
                return None
            
            # 更新访问
            memory.update_access()
        
        return memory
    
    def search(
        self,
        user_id: int,
        query: str,
        limit: int = 10,
        memory_type: Optional[MemoryType] = None
    ) -> List[MemoryItem]:
        """
        搜索短期记忆
        
        Args:
            user_id: 用户ID
            query: 查询字符串
            limit: 返回数量限制
            memory_type: 记忆类型过滤
            
        Returns:
            相关记忆列表（按相关性排序）
        """
        memories = self.memories.get(user_id, {}).values()
        
        # 过滤
        results = []
        for memory in memories:
            if memory_type and memory.memory_type != memory_type:
                continue
            
            if self._is_expired(memory):
                continue
            
            relevance = memory.calculate_relevance(query)
            if relevance > 0.1:
                results.append((memory, relevance))
        
        # 排序
        results.sort(key=lambda x: x[1], reverse=True)
        
        return [m for m, _ in results[:limit]]
    
    def get_recent(
        self,
        user_id: int,
        count: int = 10,
        memory_type: Optional[MemoryType] = None
    ) -> List[MemoryItem]:
        """
        获取最近的记忆
        
        Args:
            user_id: 用户ID
            count: 返回数量
            memory_type: 记忆类型过滤
            
        Returns:
            最近记忆列表
        """
        memories = self.memories.get(user_id, {}).values()
        
        # 过滤并排序
        results = []
        for memory in memories:
            if memory_type and memory.memory_type != memory_type:
                continue
            if not self._is_expired(memory):
                results.append(memory)
        
        results.sort(key=lambda m: m.timestamp, reverse=True)
        
        return results[:count]
    
    def remove(self, user_id: int, memory_id: str) -> bool:
        """
        删除记忆
        
        Args:
            user_id: 用户ID
            memory_id: 记忆ID
            
        Returns:
            是否成功删除
        """
        if memory_id in self.memories.get(user_id, {}):
            del self.memories[user_id][memory_id]
            return True
        return False
    
    def clear(self, user_id: int):
        """
        清除用户的所有短期记忆
        """
        if user_id in self.memories:
            self.memories[user_id] = {}
    
    def _generate_memory_id(self, user_id: int, content: str) -> str:
        """生成记忆ID"""
        raw = f"{user_id}:{content}:{datetime.now().isoformat()}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]
    
    def _is_expired(self, memory: MemoryItem) -> bool:
        """检查记忆是否过期"""
        age = (datetime.now() - memory.timestamp).total_seconds()
        return age > self.ttl
    
    def _maybe_cleanup(self, user_id: int):
        """必要时清理过期记忆"""
        now = datetime.now()
        
        if (now - self.last_cleanup).total_seconds() < self.CLEANUP_INTERVAL:
            return
        
        # 清理过期记忆
        memories = self.memories.get(user_id, {})
        expired = [mid for mid, m in memories.items() if self._is_expired(m)]
        for mid in expired:
            del memories[mid]
        
        # 如果仍然过多，删除最旧的
        if len(memories) > self.max_items:
            sorted_memories = sorted(
                memories.items(),
                key=lambda x: x[1].timestamp
            )
            excess = len(memories) - self.max_items
            for mid, _ in sorted_memories[:excess]:
                del memories[mid]
        
        self.last_cleanup = now


class LongTermMemory:
    """
    长期记忆管理器
    
    管理持久的用户信息和学习历史
    """
    
    def __init__(self):
        # 存储结构：{user_id: [MemoryItem]}
        self.memories: Dict[int, List[MemoryItem]] = defaultdict(list)
        
        # 索引结构
        self._concept_index: Dict[int, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
        self._emotion_index: Dict[int, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
        self._time_index: Dict[int, List[str]] = defaultdict(list)
    
    def store(
        self,
        user_id: int,
        content: str,
        memory_type: MemoryType = MemoryType.LONG_TERM,
        context: Optional[Dict] = None,
        importance: MemoryImportance = MemoryImportance.MEDIUM,
        concepts: Optional[List[str]] = None,
        emotions: Optional[List[str]] = None,
        memory_id: Optional[str] = None
    ) -> MemoryItem:
        """
        存储长期记忆
        
        Args:
            user_id: 用户ID
            content: 记忆内容
            memory_type: 记忆类型
            context: 上下文信息
            importance: 重要性
            concepts: 关联概念
            emotions: 关联情感
            memory_id: 指定记忆ID
            
        Returns:
            MemoryItem: 创建的记忆项
        """
        # 生成记忆ID
        if not memory_id:
            memory_id = self._generate_memory_id(user_id, content)
        
        # 创建记忆项
        memory = MemoryItem(
            memory_id=memory_id,
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            context=context or {},
            importance=importance,
            concepts=concepts or [],
            emotions=emotions or []
        )
        
        # 存储
        self.memories[user_id].append(memory)
        
        # 更新索引
        self._update_indexes(user_id, memory)
        
        return memory
    
    def _update_indexes(self, user_id: int, memory: MemoryItem):
        """更新索引"""
        # 概念索引
        for concept in memory.concepts:
            self._concept_index[user_id][concept.lower()].append(memory.memory_id)
        
        # 情感索引
        for emotion in memory.emotions:
            self._emotion_index[user_id][emotion].append(memory.memory_id)
        
        # 时间索引
        time_key = memory.timestamp.strftime('%Y-%m-%d')
        self._time_index[user_id].append(time_key)
    
    def retrieve(
        self,
        user_id: int,
        memory_id: str
    ) -> Optional[MemoryItem]:
        """
        检索特定记忆
        """
        for memory in self.memories.get(user_id, []):
            if memory.memory_id == memory_id:
                memory.update_access()
                return memory
        return None
    
    def search_by_concept(
        self,
        user_id: int,
        concept: str,
        limit: int = 10
    ) -> List[MemoryItem]:
        """
        按概念搜索记忆
        
        Args:
            user_id: 用户ID
            concept: 概念关键词
            limit: 返回数量
            
        Returns:
            相关记忆列表
        """
        memory_ids = self._concept_index.get(user_id, {}).get(concept.lower(), [])
        
        results = []
        for memory_id in memory_ids:
            memory = self.retrieve(user_id, memory_id)
            if memory:
                results.append(memory)
        
        return results[:limit]
    
    def search_by_emotion(
        self,
        user_id: int,
        emotion: str,
        limit: int = 10
    ) -> List[MemoryItem]:
        """
        按情感搜索记忆
        """
        memory_ids = self._emotion_index.get(user_id, {}).get(emotion, [])
        
        results = []
        for memory_id in memory_ids:
            memory = self.retrieve(user_id, memory_id)
            if memory:
                results.append(memory)
        
        return results[:limit]
    
    def search_by_time_range(
        self,
        user_id: int,
        start_date: datetime,
        end_date: datetime,
        limit: int = 50
    ) -> List[MemoryItem]:
        """
        按时间范围搜索记忆
        """
        memories = self.memories.get(user_id, [])
        
        results = []
        for memory in memories:
            if start_date <= memory.timestamp <= end_date:
                memory.update_access()
                results.append(memory)
        
        results.sort(key=lambda m: m.timestamp, reverse=True)
        return results[:limit]
    
    def search(
        self,
        user_id: int,
        query: str,
        concepts: Optional[List[str]] = None,
        emotions: Optional[List[str]] = None,
        memory_type: Optional[MemoryType] = None,
        limit: int = 20
    ) -> List[Tuple[MemoryItem, float]]:
        """
        综合搜索长期记忆
        
        Args:
            user_id: 用户ID
            query: 查询字符串
            concepts: 概念过滤
            emotions: 情感过滤
            memory_type: 记忆类型过滤
            limit: 返回数量
            
        Returns:
            (记忆, 相关性分数) 列表
        """
        memories = self.memories.get(user_id, [])
        
        results = []
        for memory in memories:
            # 类型过滤
            if memory_type and memory.memory_type != memory_type:
                continue
            
            # 概念过滤
            if concepts:
                if not any(c.lower() in [mc.lower() for mc in memory.concepts] for c in concepts):
                    continue
            
            # 情感过滤
            if emotions:
                if not any(e in memory.emotions for e in emotions):
                    continue
            
            # 计算相关性
            relevance = memory.calculate_relevance(query)
            if relevance > 0.05:
                results.append((memory, relevance))
        
        # 排序
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]
    
    def get_memories_by_type(
        self,
        user_id: int,
        memory_type: MemoryType,
        limit: int = 50
    ) -> List[MemoryItem]:
        """
        按类型获取记忆
        """
        memories = self.memories.get(user_id, [])
        
        results = [m for m in memories if m.memory_type == memory_type]
        results.sort(key=lambda m: m.timestamp, reverse=True)
        
        return results[:limit]
    
    def consolidate_episodic_memory(
        self,
        user_id: int,
        time_window_days: int = 7
    ) -> str:
        """
        整合情景记忆
        
        将一段时间内的情景记忆整合为语义记忆
        
        Args:
            user_id: 用户ID
            time_window_days: 时间窗口（天）
            
        Returns:
            整合后的摘要
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=time_window_days)
        
        # 获取时间范围内的情景记忆
        episodic_memories = self.search_by_time_range(
            user_id, start_date, end_date, limit=100
        )
        episodic_memories = [m for m in episodic_memories if m.memory_type == MemoryType.EPISODIC]
        
        if len(episodic_memories) < 3:
            return ""
        
        # 提取关键概念
        all_concepts = []
        for memory in episodic_memories:
            all_concepts.extend(memory.concepts)
        
        concept_counts = defaultdict(int)
        for concept in all_concepts:
            concept_counts[concept] += 1
        
        # 选择高频概念
        top_concepts = [c for c, _ in sorted(
            concept_counts.items(), key=lambda x: x[1], reverse=True
        )[:5]]
        
        # 提取情感趋势
        all_emotions = []
        for memory in episodic_memories:
            all_emotions.extend(memory.emotions)
        
        emotion_counts = defaultdict(int)
        for emotion in all_emotions:
            emotion_counts[emotion] += 1
        
        # 创建摘要
        summary_parts = []
        
        if top_concepts:
            summary_parts.append(f"学习了：{', '.join(top_concepts)}")
        
        if emotion_counts:
            dominant_emotion = max(emotion_counts, key=emotion_counts.get)
            summary_parts.append(f"情感状态：{dominant_emotion}")
        
        summary_parts.append(f"共{len(episodic_memories)}次学习交互")
        
        summary = "；".join(summary_parts)
        
        # 存储为语义记忆
        self.store(
            user_id=user_id,
            content=summary,
            memory_type=MemoryType.SEMANTIC,
            context={
                'consolidated_from': [m.memory_id for m in episodic_memories],
                'time_window': time_window_days
            },
            importance=MemoryImportance.HIGH,
            concepts=top_concepts
        )
        
        return summary
    
    def _generate_memory_id(self, user_id: int, content: str) -> str:
        """生成记忆ID"""
        raw = f"ltm_{user_id}:{content}:{datetime.now().isoformat()}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]
    
    def get_summary(self, user_id: int) -> Dict:
        """
        获取用户长期记忆摘要
        
        Args:
            user_id: 用户ID
            
        Returns:
            记忆摘要
        """
        memories = self.memories.get(user_id, [])
        
        # 统计
        type_counts = defaultdict(int)
        concept_counts = defaultdict(int)
        emotion_counts = defaultdict(int)
        
        for memory in memories:
            type_counts[memory.memory_type.value] += 1
            for concept in memory.concepts:
                concept_counts[concept] += 1
            for emotion in memory.emotions:
                emotion_counts[emotion] += 1
        
        # 最新记忆
        recent = sorted(memories, key=lambda m: m.timestamp, reverse=True)[:5]
        
        return {
            'total_memories': len(memories),
            'type_distribution': dict(type_counts),
            'top_concepts': dict(sorted(
                concept_counts.items(), key=lambda x: x[1], reverse=True
            )[:10]),
            'emotion_distribution': dict(emotion_counts),
            'recent_memories': [
                {
                    'id': m.memory_id,
                    'content': m.content[:100],
                    'timestamp': m.timestamp.isoformat()
                }
                for m in recent
            ]
        }


class WorkingMemory:
    """
    工作记忆管理器
    
    管理当前任务执行过程中的临时信息
    """
    
    def __init__(self, capacity: int = 7):
        """
        初始化工作记忆
        
        Args:
            capacity: 容量（Miller's Law：7±2）
        """
        self.capacity = capacity
        
        # 存储：{session_id: ConversationContext}
        self.contexts: Dict[str, ConversationContext] = {}
        
        # 任务栈：{session_id: [task_info]}
        self.task_stacks: Dict[str, List[Dict]] = defaultdict(list)
    
    def create_context(
        self,
        session_id: str,
        user_id: int
    ) -> ConversationContext:
        """
        创建对话上下文
        """
        context = ConversationContext(
            session_id=session_id,
            user_id=user_id
        )
        self.contexts[session_id] = context
        return context
    
    def get_context(self, session_id: str) -> Optional[ConversationContext]:
        """获取对话上下文"""
        return self.contexts.get(session_id)
    
    def update_context(
        self,
        session_id: str,
        **kwargs
    ):
        """
        更新对话上下文
        """
        context = self.contexts.get(session_id)
        if context:
            for key, value in kwargs.items():
                if hasattr(context, key):
                    setattr(context, key, value)
    
    def push_task(
        self,
        session_id: str,
        task_name: str,
        task_info: Optional[Dict] = None
    ):
        """
        推入任务
        
        Args:
            session_id: 会话ID
            task_name: 任务名称
            task_info: 任务信息
        """
        task_info = task_info or {}
        task_info['name'] = task_name
        task_info['start_time'] = datetime.now().isoformat()
        
        self.task_stacks[session_id].append(task_info)
        
        # 更新上下文
        context = self.get_context(session_id)
        if context:
            context.current_task = task_name
            context.task_progress = 0.0
    
    def pop_task(self, session_id: str) -> Optional[Dict]:
        """
        弹出任务
        
        Returns:
            任务信息
        """
        tasks = self.task_stacks.get(session_id, [])
        if tasks:
            task = tasks.pop()
            
            # 更新上下文
            context = self.get_context(session_id)
            if context:
                context.current_task = tasks[-1]['name'] if tasks else None
                context.resolved_items.append(task['name'])
            
            return task
        return None
    
    def get_current_task(self, session_id: str) -> Optional[Dict]:
        """获取当前任务"""
        tasks = self.task_stacks.get(session_id, [])
        return tasks[-1] if tasks else None
    
    def update_task_progress(
        self,
        session_id: str,
        progress: float
    ):
        """
        更新任务进度
        
        Args:
            session_id: 会话ID
            progress: 进度 (0.0 - 1.0)
        """
        context = self.get_context(session_id)
        if context:
            context.task_progress = progress
        
        tasks = self.task_stacks.get(session_id, [])
        if tasks:
            tasks[-1]['progress'] = progress
    
    def clear_session(self, session_id: str):
        """
        清除会话数据
        """
        if session_id in self.contexts:
            del self.contexts[session_id]
        if session_id in self.task_stacks:
            del self.task_stacks[session_id]


class MemoryArchitecture:
    """
    记忆架构主控制器 - 论文核心实现
    
    整合短期记忆、长期记忆和工作记忆
    实现完整的记忆管理功能
    """
    
    def __init__(self):
        """初始化记忆架构"""
        self.short_term = ShortTermMemory()
        self.long_term = LongTermMemory()
        self.working = WorkingMemory()
    
    def store_interaction(
        self,
        user_id: int,
        session_id: str,
        interaction_type: str,
        content: str,
        context: Optional[Dict] = None,
        concepts: Optional[List[str]] = None,
        emotions: Optional[List[str]] = None,
        importance: MemoryImportance = MemoryImportance.MEDIUM
    ) -> MemoryItem:
        """
        存储交互记忆
        
        Args:
            user_id: 用户ID
            session_id: 会话ID
            interaction_type: 交互类型
            content: 内容
            context: 上下文
            concepts: 概念
            emotions: 情感
            importance: 重要性
            
        Returns:
            记忆项
        """
        context = context or {}
        context['interaction_type'] = interaction_type
        context['session_id'] = session_id
        
        # 判断记忆类型
        if interaction_type in ['question', 'answer', 'explanation']:
            memory_type = MemoryType.EPISODIC
        elif interaction_type in ['concept_intro', 'knowledge_update']:
            memory_type = MemoryType.SEMANTIC
        elif interaction_type in ['practice', 'exercise']:
            memory_type = MemoryType.PROCEDURAL
        else:
            memory_type = MemoryType.SHORT_TERM
        
        # 根据重要性调整记忆存储策略
        if importance.value >= MemoryImportance.HIGH.value:
            # 高重要性同时存储到长期和短期
            stm_memory = self.short_term.store(
                user_id=user_id,
                content=content,
                memory_type=MemoryType.SHORT_TERM,
                context=context,
                importance=importance
            )
            
            ltm_memory = self.long_term.store(
                user_id=user_id,
                content=content,
                memory_type=memory_type,
                context=context,
                concepts=concepts,
                emotions=emotions,
                importance=importance
            )
            
            return ltm_memory
        else:
            # 一般交互只存储短期
            return self.short_term.store(
                user_id=user_id,
                content=content,
                memory_type=MemoryType.SHORT_TERM,
                context=context,
                importance=importance
            )
    
    def retrieve_context(
        self,
        user_id: int,
        session_id: str,
        query: str,
        include_short_term: bool = True,
        include_long_term: bool = True,
        limit: int = 20
    ) -> Dict:
        """
        检索上下文
        
        Args:
            user_id: 用户ID
            session_id: 会话ID
            query: 查询字符串
            include_short_term: 是否包含短期记忆
            include_long_term: 是否包含长期记忆
            limit: 返回数量
            
        Returns:
            上下文检索结果
        """
        results = {
            'working_context': None,
            'short_term_memories': [],
            'long_term_memories': [],
            'related_concepts': [],
            'emotional_context': {}
        }
        
        # 获取工作记忆
        working_context = self.working.get_context(session_id)
        if working_context:
            results['working_context'] = working_context.to_dict()
        
        # 检索短期记忆
        if include_short_term:
            short_term = self.short_term.search(user_id, query, limit=limit)
            results['short_term_memories'] = [m.to_dict() for m in short_term]
        
        # 检索长期记忆
        if include_long_term:
            long_term_results = self.long_term.search(
                user_id, query, limit=limit
            )
            results['long_term_memories'] = [
                (m.to_dict(), score) for m, score in long_term_results
            ]
            
            # 提取相关概念
            concepts = set()
            for memory, _ in long_term_results:
                concepts.update(memory.concepts)
            results['related_concepts'] = list(concepts)
        
        # 获取情感上下文
        emotion_memories = self.long_term.search(
            user_id, "", emotions=['frustrated', 'confused', 'excited', 'confident'],
            limit=10
        )
        
        emotion_context = defaultdict(int)
        for memory, _ in emotion_memories:
            for emotion in memory.emotions:
                emotion_context[emotion] += 1
        results['emotional_context'] = dict(emotion_context)
        
        return results
    
    def build_context_prompt(
        self,
        user_id: int,
        session_id: str,
        max_short_term: int = 5,
        max_long_term: int = 3
    ) -> str:
        """
        构建上下文提示
        
        用于LLM调用
        
        Args:
            user_id: 用户ID
            session_id: 会话ID
            max_short_term: 最大短期记忆数
            max_long_term: 最大长期记忆数
            
        Returns:
            格式化的上下文字符串
        """
        parts = []
        
        # 工作记忆
        working = self.working.get_context(session_id)
        if working:
            parts.append(f"【当前会话】{working.get_context_summary()}")
        
        # 短期记忆
        short_term = self.short_term.get_recent(user_id, count=max_short_term)
        if short_term:
            stm_parts = []
            for memory in short_term:
                stm_parts.append(f"- {memory.content}")
            parts.append(f"【近期交互】\n" + "\n".join(stm_parts))
        
        # 长期记忆
        long_term = self.long_term.get_memories_by_type(
            user_id, MemoryType.SEMANTIC, limit=max_long_term
        )
        if long_term:
            ltm_parts = []
            for memory in long_term:
                ltm_parts.append(f"- {memory.content}")
            parts.append(f"【学习历史】\n" + "\n".join(ltm_parts))
        
        return "\n\n".join(parts) if parts else ""
    
    def consolidate_memories(
        self,
        user_id: int,
        schedule: str = 'daily'
    ) -> Dict:
        """
        整合记忆
        
        定期调用，将短期记忆整合到长期记忆
        
        Args:
            user_id: 用户ID
            schedule: 调度策略
            
        Returns:
            整合结果
        """
        results = {
            'short_term_count': 0,
            'consolidated': 0,
            'summary': ''
        }
        
        # 获取短期记忆
        short_term = self.short_term.get_recent(
            user_id, count=50, memory_type=MemoryType.EPISODIC
        )
        results['short_term_count'] = len(short_term)
        
        if len(short_term) >= 5:
            # 整合到长期记忆
            for memory in short_term:
                # 提取概念和情感
                concepts = memory.context.get('concepts', [])
                emotions = memory.context.get('emotions', [])
                
                # 存储到长期记忆
                self.long_term.store(
                    user_id=user_id,
                    content=memory.content,
                    memory_type=MemoryType.SEMANTIC,
                    context=memory.context,
                    concepts=concepts,
                    emotions=emotions,
                    importance=memory.importance
                )
                
                results['consolidated'] += 1
            
            # 创建摘要
            summary = self.long_term.consolidate_episodic_memory(user_id, time_window_days=1)
            results['summary'] = summary
        
        # 清理短期记忆
        self.short_term.clear(user_id)
        
        return results
    
    def get_learning_history(
        self,
        user_id: int,
        days: int = 30
    ) -> Dict:
        """
        获取学习历史
        
        Args:
            user_id: 用户ID
            days: 历史天数
            
        Returns:
            学习历史摘要
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        memories = self.long_term.search_by_time_range(
            user_id, start_date, end_date, limit=1000
        )
        
        # 统计
        type_counts = defaultdict(int)
        concept_counts = defaultdict(int)
        emotion_counts = defaultdict(int)
        daily_activity = defaultdict(int)
        
        for memory in memories:
            type_counts[memory.memory_type.value] += 1
            for concept in memory.concepts:
                concept_counts[concept] += 1
            for emotion in memory.emotions:
                emotion_counts[emotion] += 1
            
            day_key = memory.timestamp.strftime('%Y-%m-%d')
            daily_activity[day_key] += 1
        
        # 获取记忆摘要
        memory_summary = self.long_term.get_summary(user_id)
        
        return {
            'period_days': days,
            'total_memories': len(memories),
            'type_distribution': dict(type_counts),
            'top_concepts': dict(sorted(
                concept_counts.items(), key=lambda x: x[1], reverse=True
            )[:20]),
            'emotion_trend': dict(emotion_counts),
            'daily_activity': dict(sorted(daily_activity.items())),
            'summary': memory_summary
        }


# 全局记忆架构实例
_memory_architecture: Optional[MemoryArchitecture] = None


def get_memory_architecture() -> MemoryArchitecture:
    """
    获取全局记忆架构实例
    
    Returns:
        MemoryArchitecture实例
    """
    global _memory_architecture
    if _memory_architecture is None:
        _memory_architecture = MemoryArchitecture()
    return _memory_architecture


def store_learning_interaction(
    user_id: int,
    session_id: str,
    content: str,
    interaction_type: str = 'general',
    concepts: Optional[List[str]] = None,
    emotions: Optional[List[str]] = None,
    importance: MemoryImportance = MemoryImportance.MEDIUM
) -> MemoryItem:
    """
    存储学习交互的便捷函数
    """
    architecture = get_memory_architecture()
    return architecture.store_interaction(
        user_id=user_id,
        session_id=session_id,
        interaction_type=interaction_type,
        content=content,
        concepts=concepts,
        emotions=emotions,
        importance=importance
    )


def retrieve_learning_context(
    user_id: int,
    session_id: str,
    query: str
) -> Dict:
    """
    检索学习上下文的便捷函数
    """
    architecture = get_memory_architecture()
    return architecture.retrieve_context(
        user_id=user_id,
        session_id=session_id,
        query=query
    )
