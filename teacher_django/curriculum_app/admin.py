from django.contrib import admin
from .models import CourseOutline, Slide, Animation, LearningProgress, LearningPlan, OutlineExport, Course, CourseMaterial, MaterialChunk, MaterialQuizAttempt, MaterialQuestionStat, MaterialWeakAreaArchive


@admin.register(CourseOutline)
class CourseOutlineAdmin(admin.ModelAdmin):
	list_display = ('title', 'user', 'status', 'created_at')
	list_filter = ('status', 'created_at')
	search_fields = ('title', 'user__username')


@admin.register(Slide)
class SlideAdmin(admin.ModelAdmin):
	list_display = ('course_outline', 'chapter_id', 'created_at')
	search_fields = ('chapter_id', 'course_outline__title')


@admin.register(Animation)
class AnimationAdmin(admin.ModelAdmin):
	list_display = ('concept_name', 'course_outline', 'animation_type', 'created_at')


@admin.register(LearningProgress)
class LearningProgressAdmin(admin.ModelAdmin):
	list_display = ('user', 'course_outline', 'chapter_id', 'status', 'last_accessed_at')


@admin.register(LearningPlan)
class LearningPlanAdmin(admin.ModelAdmin):
	list_display = ('title', 'user', 'status', 'created_at', 'updated_at')
	list_filter = ('status', 'created_at')
	search_fields = ('title', 'user__username')


@admin.register(OutlineExport)
class OutlineExportAdmin(admin.ModelAdmin):
	list_display = ('filename', 'course_outline', 'user', 'status', 'created_at', 'completed_at')
	list_filter = ('status', 'created_at')
	search_fields = ('filename', 'course_outline__title', 'user__username')


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
	list_display = ('title', 'owner', 'status', 'visibility', 'updated_at')
	list_filter = ('status', 'visibility', 'source_type')
	search_fields = ('title', 'owner__username', 'tags')


@admin.register(CourseMaterial)
class CourseMaterialAdmin(admin.ModelAdmin):
	list_display = ('title', 'course', 'material_type', 'processing_status', 'display_order', 'updated_at')
	list_filter = ('material_type', 'processing_status')
	search_fields = ('title', 'course__title', 'description')


@admin.register(MaterialChunk)
class MaterialChunkAdmin(admin.ModelAdmin):
	list_display = ('material', 'chunk_index', 'source_page', 'heading', 'created_at')
	search_fields = ('material__title', 'heading', 'content')


@admin.register(MaterialQuizAttempt)
class MaterialQuizAttemptAdmin(admin.ModelAdmin):
	list_display = ('user', 'material', 'difficulty_stage', 'score', 'correct_count', 'total_questions', 'created_at')
	list_filter = ('difficulty_stage', 'created_at')
	search_fields = ('user__username', 'material__title', 'course__title')


@admin.register(MaterialQuestionStat)
class MaterialQuestionStatAdmin(admin.ModelAdmin):
	list_display = ('user', 'material', 'knowledge_tag', 'wrong_count', 'consecutive_wrong_count', 'similar_generation_count', 'last_seen_at')
	list_filter = ('knowledge_tag', 'last_result_correct')
	search_fields = ('user__username', 'material__title', 'question_text', 'knowledge_tag')


@admin.register(MaterialWeakAreaArchive)
class MaterialWeakAreaArchiveAdmin(admin.ModelAdmin):
	list_display = ('user', 'course', 'material', 'knowledge_tag', 'archived_at')
	list_filter = ('archived_at',)
	search_fields = ('user__username', 'course__title', 'material__title', 'knowledge_tag')
