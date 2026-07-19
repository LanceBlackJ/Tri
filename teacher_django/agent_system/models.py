from django.db import models
from django.conf import settings


class StudentProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='student_profile')
    # 存储动态多维画像数据（JSON）
    # 六维画像设计：
    # 1) knowledge_profile: {knowledge_point: level}
    knowledge_profile = models.JSONField(default=dict, blank=True)
    # 2) cognitive_style: 简短描述（视觉型/听觉型/动手型/混合）
    cognitive_style = models.CharField(max_length=100, blank=True)
    # 3) learning_goals: 列表
    learning_goals = models.JSONField(default=list, blank=True)
    # 4) misconceptions: 列表/结构化易错点
    misconceptions = models.JSONField(default=list, blank=True)
    # 5) engagement: 结构化参与度信息，例如 {'score': 0-100, 'notes': '...'}
    engagement = models.JSONField(default=dict, blank=True)
    # 6) preferences: 学习偏好（保留原名 learning_preferences）
    learning_preferences = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # USER-LLM R1 新增字段
    profile_version = models.IntegerField(default=1)
    last_inference_time = models.DateTimeField(null=True, blank=True)
    inference_confidence = models.FloatField(default=0.0)
    recent_inferences = models.JSONField(default=list, blank=True)
    is_cold_start = models.BooleanField(default=True)
    cold_start_progress = models.FloatField(default=0.0)

    # 费曼互教"小艾"的记忆流（生成式智能体记忆流，Park et al. 2023 UIST）
    # 列表元素结构：{id, type, content, importance(1-10), created_at, last_accessed_at, topic}
    peer_memory_stream = models.JSONField(default=list, blank=True)

    # 7) knowledge_timestamps: 每个知识点最近一次BKT更新的时间戳（遗忘衰减计算用）
    #    格式: {tag: iso8601_string}
    knowledge_timestamps = models.JSONField(default=dict, blank=True)

    def update_from_dict(self, data: dict, save: bool = True):
        """Merge provided画像字段到当前 profile 中。"""
        if not isinstance(data, dict):
            return
        kp = data.get('knowledge_profile')
        if isinstance(kp, dict):
            cur = self.knowledge_profile or {}
            cur.update(kp)
            self.knowledge_profile = cur
        cs = data.get('cognitive_style')
        if cs:
            self.cognitive_style = cs
        lg = data.get('learning_goals')
        if isinstance(lg, list):
            cur = list(self.learning_goals or [])
            # append new unique goals
            for g in lg:
                if g not in cur:
                    cur.append(g)
            self.learning_goals = cur
        ms = data.get('misconceptions')
        if isinstance(ms, list):
            cur = list(self.misconceptions or [])
            for m in ms:
                if m not in cur:
                    cur.append(m)
            self.misconceptions = cur
        eg = data.get('engagement')
        if isinstance(eg, dict):
            cur = self.engagement or {}
            cur.update(eg)
            self.engagement = cur
        prefs = data.get('preferences') or data.get('learning_preferences')
        if isinstance(prefs, dict):
            cur = self.learning_preferences or {}
            cur.update(prefs)
            self.learning_preferences = cur
        if save:
            self.save()

    def __str__(self):
        return f"{self.user} Profile"


class ProfileEvent(models.Model):
    """用户行为画像事件，用于追踪哪些行为影响了画像。"""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile_events')
    event_type = models.CharField(max_length=80)
    source_app = models.CharField(max_length=80, blank=True, default='')
    course_id = models.IntegerField(blank=True, null=True)
    material_id = models.IntegerField(blank=True, null=True)
    confidence = models.FloatField(default=1.0)
    payload = models.JSONField(default=dict, blank=True)
    profile_delta = models.JSONField(default=dict, blank=True)
    dedupe_key = models.CharField(max_length=160, blank=True, default='', db_index=True)
    processed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'agent_profile_events'
        indexes = [
            models.Index(fields=['user', 'event_type', 'created_at']),
            models.Index(fields=['processed_at']),
        ]

    def __str__(self):
        return f"ProfileEvent {self.event_type} for {self.user_id}"


class LearningResource(models.Model):
    RESOURCE_TYPES = [
        ('doc', '文档/讲义'),
        ('ppt', 'PPT'),
        ('quiz', '练习题'),
        ('animation', 'H5动画'),
        ('video', '视频/动画'),
        ('code', '代码案例'),
    ]
    title = models.CharField(max_length=255)
    resource_type = models.CharField(max_length=20, choices=RESOURCE_TYPES, default='doc')
    content = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    # 简单标签支持（用于搜索/分类）
    tags = models.JSONField(default=list, blank=True)
    # Embedding 向量（JSON list），用于相似检索
    embedding = models.JSONField(default=list, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


class AgentTask(models.Model):
    STATUS_CHOICES = [
        ('pending', '待处理'),
        ('running', '运行中'),
        ('done', '完成'),
        ('failed', '失败'),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=200, blank=True)
    input_data = models.JSONField(default=dict, blank=True)
    output_data = models.JSONField(default=dict, blank=True)
    # 优先级（越大优先级越高）与依赖任务列表
    priority = models.IntegerField(default=50)
    depends_on = models.JSONField(default=list, blank=True)
    # 任务进度（0-100）与简要结果摘要
    progress = models.IntegerField(default=0)
    result_summary = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Task {self.id} ({self.status}) [{self.progress}%]"


class Conversation(models.Model):
    """对话会话模型，用于存储一次连续的 tutor 对话。"""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=255, blank=True)
    context_summary = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'conversations'

    def __str__(self):
        return f"Conversation {self.id} - {self.title or self.user.username}"


class Message(models.Model):
    ROLE_CHOICES = [
        ('student', 'Student'),
        ('assistant', 'Assistant'),
        ('system', 'System'),
        ('tool', 'Tool'),
    ]
    CONTENT_TYPES = [
        ('text', 'Text'),
        ('code', 'Code'),
        ('quiz', 'Quiz'),
        ('ppt', 'PPT'),
        ('json', 'JSON'),
    ]

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='student')
    content = models.TextField()
    content_type = models.CharField(max_length=20, choices=CONTENT_TYPES, default='text')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'messages'
        indexes = [models.Index(fields=['conversation', 'created_at'])]

    def __str__(self):
        return f"Message {self.id} ({self.role}) in convo {self.conversation_id}"


class ReasoningChain(models.Model):
    """推理链记录 - 存储USER-LLM R1的推理过程"""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reasoning_chains')
    query = models.TextField()
    conversation_id = models.IntegerField(null=True, blank=True)
    reasoning_steps = models.JSONField(default=list)
    profile_delta = models.JSONField(default=dict)
    confidence = models.FloatField(default=0.0)
    validation_result = models.JSONField(default=dict)
    inference_time = models.DateTimeField(auto_now_add=True)
    profile_version = models.IntegerField(default=1)

    class Meta:
        ordering = ['-inference_time']
        indexes = [
            models.Index(fields=['user', 'inference_time']),
            models.Index(fields=['conversation_id']),
        ]

    def __str__(self):
        return f"ReasoningChain for {self.user.username} at {self.inference_time}"


class EventEmbedding(models.Model):
    """事件嵌入向量 - 存储ProfileEvent的嵌入向量"""
    profile_event = models.OneToOneField(
        ProfileEvent,
        on_delete=models.CASCADE,
        related_name='embedding_record'
    )
    embedding_vector = models.JSONField(default=list)
    embedding_model = models.CharField(max_length=100, default='sha256_hash')
    dimension = models.IntegerField(default=256)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'agent_event_embeddings'
        indexes = [
            models.Index(fields=['embedding_model']),
        ]
    
    def __str__(self):
        return f"Embedding for event {self.profile_event_id}"


class MultipartMessage(models.Model):
    """多模态消息 - 支持图片等多模态输入"""
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='multimodal_parts')
    modality_type = models.CharField(max_length=20, choices=[
        ('text', '文本'),
        ('image', '图片'),
        ('audio', '音频'),
        ('file', '文件'),
    ])
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'agent_multipart_messages'
    
    def __str__(self):
        return f"MultipartMessage {self.id} ({self.modality_type})"
