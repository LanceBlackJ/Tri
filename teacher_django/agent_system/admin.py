from django.contrib import admin
from .models import StudentProfile, LearningResource, AgentTask


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'cognitive_style', 'engagement_score', 'updated_at')
    search_fields = ('user__username',)

    def engagement_score(self, obj):
        try:
            return (obj.engagement or {}).get('score')
        except Exception:
            return None
    engagement_score.short_description = '参与度得分'


@admin.register(LearningResource)
class LearningResourceAdmin(admin.ModelAdmin):
    list_display = ('title', 'resource_type', 'author', 'tags_list', 'created_at')
    list_filter = ('resource_type',)
    search_fields = ('title',)
    actions = ('create_regeneration_task',)

    def tags_list(self, obj):
        try:
            return ','.join(obj.tags or [])
        except Exception:
            return ''
    tags_list.short_description = '标签'

    def create_regeneration_task(self, request, queryset):
        created = 0
        from .models import AgentTask
        for res in queryset:
            topic = res.title
            rtype = res.resource_type
            data = {'topic': topic, 'resource_types': [rtype], 'origin_resource_id': res.id}
            AgentTask.objects.create(user=request.user, name=f'regenerate:{res.id}', input_data=data, status='pending')
            created += 1
        self.message_user(request, f'已创建 {created} 个再生成任务（待后台处理）')
    create_regeneration_task.short_description = '为所选资源创建再生成任务'


@admin.register(AgentTask)
class AgentTaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'user', 'status', 'progress', 'created_at', 'updated_at')
    list_filter = ('status',)
    search_fields = ('name', 'user__username')
    readonly_fields = ('output_data', 'result_summary')
