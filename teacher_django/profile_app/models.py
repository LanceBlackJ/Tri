from django.db import models
from core.models import User


class StudentProfile(models.Model):
    """
    学生画像模型，对应原项目的 student_profiles 表
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    course_id = models.CharField(max_length=100, default='default')
    profile_data = models.TextField()  # JSON字符串存储完整画像
    confidence_scores = models.TextField(blank=True, null=True)  # JSON字符串存储各维度置信度
    version = models.IntegerField(default=1)
    last_updated = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'student_profiles'
        indexes = [
            models.Index(fields=['user', 'course_id']),
            models.Index(fields=['last_updated']),
        ]
        unique_together = ('user', 'course_id')

    def __str__(self):
        return f"Profile for {self.user.username} - {self.course_id}"


class ProfileSnapshot(models.Model):
    """
    学习成长报告的画像快照，记录某一时刻的六维雷达图数据，用于纵向对比。
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    course_id = models.CharField(max_length=100, default='default')
    radar_labels = models.TextField()  # JSON数组: 六维标签
    radar_values = models.TextField()  # JSON数组: 六维分值(0-100)
    knowledge_snapshot = models.TextField(blank=True, default='{}')  # JSON: {知识点: 分数}
    profile_hash = models.CharField(max_length=64)  # sha256(profile_data + confidence_scores)
    ai_narrative = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'profile_snapshots'
        indexes = [
            models.Index(fields=['user', 'course_id', '-created_at'], name='profile_snap_user_crs_idx'),
            models.Index(fields=['profile_hash'], name='profile_snap_hash_idx'),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"Snapshot for {self.user.username} - {self.course_id} @ {self.created_at}"


class ProfileConversationSession(models.Model):
    """
    画像构建会话模型，对应 profile_conversation_sessions 表
    """
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('abandoned', 'Abandoned'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    course_id = models.CharField(max_length=100, default='default')
    asked_dimensions = models.TextField()  # JSON数组: 已询问的维度
    answered_dimensions = models.TextField()  # JSON数组: 已获得答案的维度
    skipped_dimensions = models.TextField()  # JSON数组: 用户跳过的维度
    conversation_history = models.TextField()  # JSON数组: 完整对话历史
    current_round = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'profile_conversation_sessions'
        indexes = [
            models.Index(fields=['user', 'course_id']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"Session for {self.user.username} - {self.course_id}"


class ProfileEvent(models.Model):
    """[已弃用 / DEAD MODEL] 不要再使用本模型。

    真正在用的画像事件模型是 agent_system.models.ProfileEvent（字段为 payload JSONField、
    含 dedupe_key/processed_at/profile_delta）。本模型（字段 event_payload TextField）全库无任何
    读写，仅因历史迁移 profile_app/migrations/0001_initial.py 建了 profile_events 表而保留。
    保留仅为避免删表迁移风险；新增代码一律 import agent_system 版本。
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    event_type = models.CharField(max_length=50)  # 'conversation', 'quiz_result', etc.
    event_payload = models.TextField()  # JSON字符串存储事件详情
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'profile_events'
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['event_type']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"Event {self.event_type} for {self.user.username}"