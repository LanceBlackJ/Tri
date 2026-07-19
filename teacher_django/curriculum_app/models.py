from django.db import models
from core.models import User


class CourseOutline(models.Model):
    """
    课程大纲模型，对应 course_outlines 表
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('generating', 'Generating'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    estimated_hours = models.FloatField(default=0)
    outline_data = models.TextField()  # JSON字符串存储完整课程大纲
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    progress = models.FloatField(default=0)  # 生成进度 0-100
    exported_pptx = models.CharField(max_length=512, blank=True, null=True)
    export_status = models.CharField(max_length=20, default='idle')
    export_progress = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'course_outlines'
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['status']),
            models.Index(fields=['export_status']),
        ]

    def __str__(self):
        return self.title


class OutlineExport(models.Model):
    """
    导出文件记录，用于保存历史导出（PPTX）信息
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    course_outline = models.ForeignKey(CourseOutline, on_delete=models.CASCADE, related_name='exports')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    file_path = models.CharField(max_length=512, blank=True, null=True)
    filename = models.CharField(max_length=255, blank=True, null=True)
    filesize = models.BigIntegerField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    task_id = models.CharField(max_length=255, blank=True, null=True)
    message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'outline_exports'
        indexes = [
            models.Index(fields=['course_outline']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"Export {self.filename or self.pk} for {self.course_outline.title}"


class Slide(models.Model):
    """
    幻灯片数据模型，对应 slides 表
    """
    course_outline = models.ForeignKey(CourseOutline, on_delete=models.CASCADE)
    chapter_id = models.CharField(max_length=100)
    slide_data = models.TextField()  # JSON字符串存储幻灯片数组
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'slides'
        indexes = [
            models.Index(fields=['course_outline']),
            models.Index(fields=['chapter_id']),
        ]

    def __str__(self):
        return f"Slides for {self.course_outline.title} - {self.chapter_id}"


class Animation(models.Model):
    """
    动画代码模型，对应 animations 表
    """
    ANIMATION_TYPE_CHOICES = [
        ('css', 'CSS'),
        ('canvas', 'Canvas'),
        ('svg', 'SVG'),
    ]

    course_outline = models.ForeignKey(CourseOutline, on_delete=models.CASCADE)
    chapter_id = models.CharField(max_length=100)
    concept_name = models.CharField(max_length=200)  # 动画对应的概念名称
    animation_code = models.TextField()  # HTML/CSS/JS动画代码
    animation_type = models.CharField(max_length=20, choices=ANIMATION_TYPE_CHOICES, default='css')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'animations'
        indexes = [
            models.Index(fields=['course_outline']),
            models.Index(fields=['chapter_id']),
        ]

    def __str__(self):
        return f"Animation {self.concept_name} for {self.course_outline.title}"


class LearningProgress(models.Model):
    """
    学习进度模型，对应 learning_progress 表
    """
    STATUS_CHOICES = [
        ('not_started', 'Not Started'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    course_outline = models.ForeignKey(CourseOutline, on_delete=models.CASCADE)
    chapter_id = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='not_started')
    completed_slides = models.IntegerField(default=0)
    total_slides = models.IntegerField(default=0)
    quiz_score = models.FloatField(default=0)
    last_accessed_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'learning_progress'
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['course_outline']),
        ]
        unique_together = ('user', 'course_outline', 'chapter_id')

    def __str__(self):
        return f"Progress for {self.user.username} - {self.course_outline.title} - {self.chapter_id}"


class LearningPlan(models.Model):
    """
    学习计划模型，对应 learning_plans 表
    """
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('generated', 'Generated'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    plan_data = models.TextField()  # 可存 JSON 字符串或其它结构化内容
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'learning_plans'
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return self.title


class Course(models.Model):
    """
    教师录入型课程，承载上传资料课程的元信息。
    """

    SOURCE_TYPE_CHOICES = [
        ('uploaded', 'Uploaded'),
        ('generated', 'Generated'),
    ]
    VISIBILITY_CHOICES = [
        ('private', 'Private'),
        ('login', 'Login Users'),
        ('public', 'Public'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('published', 'Published'),
        ('archived', 'Archived'),
    ]

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='courses')
    title = models.CharField(max_length=200)
    summary = models.TextField(blank=True, default='')
    description = models.TextField(blank=True, default='')
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPE_CHOICES, default='uploaded')
    visibility = models.CharField(max_length=20, choices=VISIBILITY_CHOICES, default='login')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    cover_image = models.ImageField(upload_to='course_covers/', blank=True, null=True)
    tags = models.CharField(max_length=255, blank=True, default='')
    published_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'courses'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['owner']),
            models.Index(fields=['status']),
            models.Index(fields=['visibility']),
        ]

    def __str__(self):
        return self.title


class CourseMaterial(models.Model):
    """
    课程资料文件，支持 PDF/PPT/DOCX 等教学资源上传。
    """

    MATERIAL_TYPE_CHOICES = [
        ('pdf', 'PDF'),
        ('ppt', 'PPT/PPTX'),
        ('doc', 'DOC/DOCX'),
        ('video', 'Video'),
        ('image', 'Image'),
        ('archive', 'Archive'),
        ('other', 'Other'),
    ]
    PROCESSING_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('ready', 'Ready'),
        ('failed', 'Failed'),
    ]

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='materials')
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='uploaded_course_materials')
    title = models.CharField(max_length=200)
    material_type = models.CharField(max_length=20, choices=MATERIAL_TYPE_CHOICES, default='other')
    file = models.FileField(upload_to='course_materials/%Y/%m/%d/')
    description = models.TextField(blank=True, default='')
    display_order = models.PositiveIntegerField(default=0)
    file_size = models.BigIntegerField(default=0)
    processing_status = models.CharField(max_length=20, choices=PROCESSING_STATUS_CHOICES, default='pending')
    page_count = models.PositiveIntegerField(default=0)
    extracted_text = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'course_materials'
        ordering = ['display_order', 'created_at']
        indexes = [
            models.Index(fields=['course']),
            models.Index(fields=['material_type']),
            models.Index(fields=['processing_status']),
        ]

    def __str__(self):
        return f"{self.course.title} - {self.title}"

    def save(self, *args, **kwargs):
        if self.file and not self.file_size:
            try:
                self.file_size = self.file.size or 0
            except Exception:
                self.file_size = 0
        super().save(*args, **kwargs)


class MaterialChunk(models.Model):
    """
    资料解析后的片段，为后续检索增强答疑与出题提供统一语料。
    """

    material = models.ForeignKey(CourseMaterial, on_delete=models.CASCADE, related_name='chunks')
    chunk_index = models.PositiveIntegerField(default=0)
    source_page = models.CharField(max_length=50, blank=True, default='')
    heading = models.CharField(max_length=255, blank=True, default='')
    content = models.TextField()
    # 关键词摘要由 LLM 生成、长度不定，实测会超过 255（SQLite 不校验长度、MySQL 严格），
    # 故用 TextField 不设上限，避免迁移到 MySQL 时报 1406 Data too long。
    keyword_summary = models.TextField(blank=True, default='')
    embedding = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'material_chunks'
        ordering = ['material', 'chunk_index']
        unique_together = ('material', 'chunk_index')
        indexes = [
            models.Index(fields=['material']),
            models.Index(fields=['source_page']),
        ]

    def __str__(self):
        return f"Chunk {self.chunk_index} for {self.material.title}"


class MaterialQuizAttempt(models.Model):
    """
    资料练习题作答记录，用于沉淀练习历史、错题本和难度递进信息。
    """

    DIFFICULTY_STAGE_CHOICES = [
        ('standard', 'Standard'),
        ('reinforce', 'Reinforce'),
        ('progressive', 'Progressive'),
        ('challenge', 'Challenge'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='material_quiz_attempts')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='material_quiz_attempts')
    material = models.ForeignKey(CourseMaterial, on_delete=models.CASCADE, related_name='quiz_attempts')
    quiz_resource = models.ForeignKey('agent_system.LearningResource', on_delete=models.SET_NULL, null=True, blank=True, related_name='material_quiz_attempts')
    quiz_snapshot = models.JSONField(default=dict, blank=True)
    answers = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    question_fingerprints = models.JSONField(default=list, blank=True)
    knowledge_tags = models.JSONField(default=list, blank=True)
    recommended_review_pages = models.JSONField(default=list, blank=True)
    difficulty_stage = models.CharField(max_length=20, choices=DIFFICULTY_STAGE_CHOICES, default='standard')
    focus_question_fingerprint = models.CharField(max_length=64, blank=True, default='')
    score = models.FloatField(default=0)
    total_questions = models.PositiveIntegerField(default=0)
    correct_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'material_quiz_attempts'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'material']),
            models.Index(fields=['course', 'material']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"QuizAttempt {self.user} / {self.material.title} / {self.created_at:%Y-%m-%d %H:%M}"


class MaterialQuizAdaptivePolicy(models.Model):
    """
    资料级出题自适应策略。
    用轻量参数调优替代训练大模型本体，实现“可学习”的出题闭环。
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='material_quiz_policies')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='material_quiz_policies')
    material = models.ForeignKey(CourseMaterial, on_delete=models.CASCADE, related_name='quiz_policies')
    feedback_counts = models.JSONField(default=dict, blank=True)
    strategy = models.JSONField(default=dict, blank=True)
    ability_rating = models.FloatField(default=1200.0)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'material_quiz_adaptive_policies'
        ordering = ['-updated_at']
        unique_together = ('user', 'material')
        indexes = [
            models.Index(fields=['user', 'material']),
            models.Index(fields=['course', 'material']),
            models.Index(fields=['updated_at']),
        ]

    def __str__(self):
        return f"QuizPolicy {self.user} / {self.material.title}"


class MaterialQuestionStat(models.Model):
    """
    题目粒度的学习画像，用于去重、错题本、知识点归类和连续错题推荐。
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='material_question_stats')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='material_question_stats')
    material = models.ForeignKey(CourseMaterial, on_delete=models.CASCADE, related_name='question_stats')
    question_fingerprint = models.CharField(max_length=64)
    question_text = models.TextField(blank=True, default='')
    canonical_answer = models.TextField(blank=True, default='')
    explanation = models.TextField(blank=True, default='')
    knowledge_tag = models.CharField(max_length=255, blank=True, default='')
    source_page = models.CharField(max_length=50, blank=True, default='')
    source_heading = models.CharField(max_length=255, blank=True, default='')
    attempts_count = models.PositiveIntegerField(default=0)
    seen_count = models.PositiveIntegerField(default=0)
    wrong_count = models.PositiveIntegerField(default=0)
    consecutive_wrong_count = models.PositiveIntegerField(default=0)
    similar_generation_count = models.PositiveIntegerField(default=0)
    last_result_correct = models.BooleanField(default=False)
    elo_rating = models.FloatField(default=1200.0)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'material_question_stats'
        ordering = ['-last_seen_at']
        unique_together = ('user', 'material', 'question_fingerprint')
        indexes = [
            models.Index(fields=['user', 'material']),
            models.Index(fields=['knowledge_tag']),
            models.Index(fields=['wrong_count']),
            models.Index(fields=['consecutive_wrong_count']),
        ]

    def __str__(self):
        return f"QuestionStat {self.user} / {self.material.title} / {self.question_fingerprint[:8]}"


class MaterialWeakAreaArchive(models.Model):
    """
    用户主动归档的薄弱点快照，用于把当前焦点与长期回看分开。
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='material_weak_area_archives')
    question_stat = models.ForeignKey(MaterialQuestionStat, on_delete=models.CASCADE, related_name='archives')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='weak_area_archives')
    material = models.ForeignKey(CourseMaterial, on_delete=models.CASCADE, related_name='weak_area_archives')
    knowledge_tag = models.CharField(max_length=255, blank=True, default='')
    source_page = models.CharField(max_length=50, blank=True, default='')
    source_heading = models.CharField(max_length=255, blank=True, default='')
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'material_weak_area_archives'
        ordering = ['-archived_at']
        unique_together = ('user', 'question_stat')
        indexes = [
            models.Index(fields=['user', 'archived_at']),
            models.Index(fields=['course', 'material']),
        ]

    def __str__(self):
        return f"WeakAreaArchive {self.user} / {self.knowledge_tag or self.question_stat_id}"