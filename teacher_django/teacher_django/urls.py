"""
URL configuration for teacher_django project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render
from django.conf import settings
from django.conf.urls.static import static
from django.db.models import Q, Avg
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta


def _accessible_courses_for_user(user, Course):
    if getattr(user, 'is_authenticated', False):
        return Course.objects.filter(
            Q(owner=user)
            | (
                Q(status='published')
                & (Q(visibility='public') | Q(visibility='login'))
            )
        ).distinct()
    return Course.objects.filter(status='published', visibility='public')


def _build_home_dashboard_context(user=None):
    from curriculum_app.models import Course, CourseMaterial, MaterialQuizAttempt, MaterialQuestionStat, MaterialWeakAreaArchive
    try:
        from agent_system.models import Conversation, ProfileEvent
    except Exception:
        Conversation = None
        ProfileEvent = None

    is_authenticated = getattr(user, 'is_authenticated', False)

    if is_authenticated:
        profile = getattr(user, 'student_profile', None)
        engagement = profile.engagement if profile and isinstance(profile.engagement, dict) else {}
        engagement_score = engagement.get('score')
        learning_goals = list(profile.learning_goals or []) if profile else []
        misconception_count = len(profile.misconceptions or []) if profile else 0
        cognitive_style = (profile.cognitive_style or '待补充') if profile else '待补充'
        display_name = (getattr(user, 'full_name', '') or '').strip() or user.username
        avatar_url = (getattr(user, 'avatar_url', '') or '').strip()
        user_major = (getattr(user, 'major', '') or '').strip()
        user_grade = (getattr(user, 'grade', '') or '').strip()
        user_initial = (display_name[:1] or user.username[:1] or 'U').upper()

        owned_course_qs = Course.objects.filter(owner=user).prefetch_related('materials').order_by('-updated_at')
        accessible_course_qs = _accessible_courses_for_user(user, Course).prefetch_related('materials').order_by('-updated_at')

        week_start = timezone.now() - timedelta(days=7)
        weekly_attempts = MaterialQuizAttempt.objects.filter(user=user, created_at__gte=week_start)
        weekly_summary = weekly_attempts.aggregate(avg_score=Avg('score'))
        weekly_learning_days = len({attempt.created_at.date() for attempt in weekly_attempts.only('created_at')})

        recent_attempts = list(
            MaterialQuizAttempt.objects.filter(user=user)
            .select_related('course', 'material')
            .order_by('-created_at')[:8]
        )

        recent_learning = []
        seen_material_ids = set()
        for attempt in recent_attempts:
            if attempt.material_id in seen_material_ids:
                continue
            seen_material_ids.add(attempt.material_id)
            review_page = ''
            recommended_pages = attempt.recommended_review_pages if isinstance(attempt.recommended_review_pages, list) else []
            if recommended_pages:
                review_page = str(recommended_pages[0].get('source_page') or '').strip()
            study_url = reverse('course_study', args=[attempt.course_id]) + f'?material={attempt.material_id}'
            if review_page:
                study_url += f'&page={review_page}'
            recent_learning.append({
                'course_title': attempt.course.title,
                'material_title': attempt.material.title,
                'score': attempt.score,
                'difficulty_stage': attempt.get_difficulty_stage_display(),
                'updated_at': attempt.created_at,
                'review_page': review_page,
                'study_url': study_url,
            })
            if len(recent_learning) >= 3:
                break

        if not recent_learning:
            for course in accessible_course_qs[:3]:
                first_material = next(iter(course.materials.all()), None)
                study_url = reverse('course_study', args=[course.id])
                if first_material:
                    study_url += f'?material={first_material.id}'
                recent_learning.append({
                    'course_title': course.title,
                    'material_title': first_material.title if first_material else '等待教师添加资料',
                    'score': None,
                    'difficulty_stage': '待开始',
                    'updated_at': course.updated_at,
                    'review_page': '',
                    'study_url': study_url,
                })

        hero_primary = recent_learning[0]['study_url'] if recent_learning else reverse('course_library')
        hero_secondary = reverse('teacher_course_library')

        focus_areas = []
        archived_stat_ids = list(
            MaterialWeakAreaArchive.objects.filter(user=user).values_list('question_stat_id', flat=True)
        )

        weak_stats = list(
            MaterialQuestionStat.objects.filter(user=user, wrong_count__gt=0)
            .exclude(id__in=archived_stat_ids)
            .select_related('course', 'material')
            .order_by('-consecutive_wrong_count', '-wrong_count', '-last_seen_at')[:4]
        )
        for stat in weak_stats:
            target_url = reverse('course_study', args=[stat.course_id]) + f'?material={stat.material_id}'
            if stat.source_page:
                target_url += f'&page={stat.source_page}'
            focus_areas.append({
                'knowledge_tag': stat.knowledge_tag or '未归类知识点',
                'course_title': stat.course.title,
                'material_title': stat.material.title,
                'wrong_count': stat.wrong_count,
                'consecutive_wrong_count': stat.consecutive_wrong_count,
                'source_page': stat.source_page,
                'source_heading': stat.source_heading,
                'target_url': target_url,
            })

        if not focus_areas:
            focus_areas = [
                {
                    'knowledge_tag': '先建立你的学习画像',
                    'course_title': '成长中心',
                    'material_title': '画像与目标',
                    'wrong_count': 0,
                    'consecutive_wrong_count': 0,
                    'source_page': '',
                    'source_heading': '先完善画像，系统才能给出更具体的学习建议。',
                    'target_url': reverse('profile_building'),
                }
            ]

        recent_conversations = []
        if Conversation is not None:
            for convo in Conversation.objects.filter(user=user).order_by('-updated_at')[:3]:
                recent_conversations.append({
                    'id': convo.id,
                    'title': (convo.title or '未命名对话').strip() or '未命名对话',
                    'updated_at': convo.updated_at,
                    'detail_url': reverse('chat_interface') + f'?conversation_id={convo.id}',
                })

        if not recent_conversations:
            recent_conversations = [
                {
                    'id': None,
                    'title': '还没有最近对话',
                    'updated_at': None,
                    'detail_url': reverse('chat_interface'),
                }
            ]

        profile_event_cards = []
        event_label_map = {
            'material_quiz_submitted': '资料小测更新画像',
            'conversation_message': 'AI 对话更新画像',
            'course_ai_message': '课程 AI 更新画像',
        }
        if ProfileEvent is not None:
            try:
                for event in ProfileEvent.objects.filter(user=user).order_by('-created_at')[:4]:
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    profile_event_cards.append({
                        'title': event_label_map.get(event.event_type, event.event_type),
                        'detail': payload.get('material_title') or payload.get('course_title') or payload.get('text') or '已记录一条新的画像信号',
                        'processed': bool(event.processed_at),
                        'created_at': event.created_at,
                    })
            except Exception:
                profile_event_cards = []

        if not profile_event_cards:
            profile_event_cards = [
                {
                    'title': '等待新的画像信号',
                    'detail': '做题、学习和对话后，这里会显示哪些行为正在更新画像。',
                    'processed': False,
                    'created_at': None,
                }
            ]

        growth_timeline = []
        if recent_learning:
            lead = recent_learning[0]
            growth_timeline.append({
                'title': '最近一次学习',
                'detail': f"{lead['course_title']} / {lead['material_title']}",
                'meta': f"{lead['difficulty_stage']} · {lead['updated_at'].strftime('%m月%d日') if lead.get('updated_at') else '刚刚'}",
                'url': lead['study_url'],
            })
        if focus_areas:
            lead_focus = focus_areas[0]
            growth_timeline.append({
                'title': '当前补弱焦点',
                'detail': lead_focus['knowledge_tag'],
                'meta': f"{lead_focus['course_title']} · 累计错误 {lead_focus['wrong_count']} 次",
                'url': lead_focus['target_url'],
            })
        if recent_conversations:
            lead_convo = recent_conversations[0]
            growth_timeline.append({
                'title': '最近对话',
                'detail': lead_convo['title'],
                'meta': lead_convo['updated_at'].strftime('%m月%d日 %H:%M') if lead_convo.get('updated_at') else '暂无时间',
                'url': lead_convo['detail_url'],
            })
        growth_timeline.append({
            'title': '今日任务',
            'detail': '先处理主任务，再补画像和课程运营。',
            'meta': '个人主页任务视图',
            'url': hero_primary,
        })

        today_actions = [
            {
                'title': '继续当前学习',
                'detail': recent_learning[0]['course_title'] if recent_learning else '先从课程库开始一门课程',
                'url': hero_primary,
            },
            {
                'title': '补齐学习画像',
                'detail': '补全目标、偏好和易错点，系统会给出更准的建议。',
                'url': reverse('profile_building'),
            },
            {
                'title': '查看最近对话',
                'detail': recent_conversations[0]['title'],
                'url': recent_conversations[0]['detail_url'],
            },
        ]

        teacher_course_cards = []
        for course in owned_course_qs[:3]:
            teacher_course_cards.append({
                'title': course.title,
                'summary': (course.summary or course.description or '这门课程正在等待进一步完善资料与学习路径。')[:88],
                'status': course.get_status_display(),
                'visibility': course.get_visibility_display(),
                'material_count': course.materials.count(),
                'detail_url': reverse('teacher_course_detail', args=[course.id]),
                'updated_at': course.updated_at,
            })

        teaching_metrics = {
            'total_courses': owned_course_qs.count(),
            'published_courses': owned_course_qs.filter(status='published').count(),
            'draft_courses': owned_course_qs.filter(status='draft').count(),
            'material_count': CourseMaterial.objects.filter(course__owner=user).count(),
            'processing_material_count': CourseMaterial.objects.filter(course__owner=user, processing_status__in=['pending', 'processing']).count(),
        }

        learning_metrics = {
            'weekly_practice_count': weekly_attempts.count(),
            'weekly_average_score': weekly_summary.get('avg_score') or 0,
            'weekly_learning_days': weekly_learning_days,
            'tracked_focus_count': len(weak_stats),
            'engagement_score': engagement_score,
            'goal_count': len(profile.learning_goals or []) if profile else 0,
        }

    else:
        profile = None
        engagement_score = None
        learning_goals = []
        misconception_count = 0
        cognitive_style = '待补充'
        display_name = '访客'
        avatar_url = ''
        user_major = ''
        user_grade = ''
        user_initial = 'G'

        accessible_course_qs = Course.objects.filter(status='published', visibility='public').prefetch_related('materials').order_by('-updated_at')

        recent_learning = []
        for course in accessible_course_qs[:3]:
            first_material = next(iter(course.materials.all()), None)
            study_url = reverse('course_study', args=[course.id])
            if first_material:
                study_url += f'?material={first_material.id}'
            recent_learning.append({
                'course_title': course.title,
                'material_title': first_material.title if first_material else '等待教师添加资料',
                'score': None,
                'difficulty_stage': '待开始',
                'updated_at': course.updated_at,
                'review_page': '',
                'study_url': study_url,
            })

        hero_primary = recent_learning[0]['study_url'] if recent_learning else reverse('course_library')
        hero_secondary = reverse('course_library')

        focus_areas = [
            {
                'knowledge_tag': '登录后开始学习',
                'course_title': '欢迎使用',
                'material_title': '请先登录或注册',
                'wrong_count': 0,
                'consecutive_wrong_count': 0,
                'source_page': '',
                'source_heading': '登录后系统会根据您的学习情况提供个性化建议。',
                'target_url': reverse('login'),
            }
        ]

        recent_conversations = [
            {
                'id': None,
                'title': '登录后开启对话',
                'updated_at': None,
                'detail_url': reverse('login'),
            }
        ]

        profile_event_cards = [
            {
                'title': '登录体验完整功能',
                'detail': '登录后系统会记录您的学习行为，为您提供个性化学习建议。',
                'processed': False,
                'created_at': None,
            }
        ]

        growth_timeline = [
            {
                'title': '浏览公开课程',
                'detail': '了解系统提供的学习资源',
                'meta': '课程库',
                'url': reverse('course_library'),
            },
            {
                'title': '创建账户',
                'detail': '注册并完善学习画像',
                'meta': '开始学习之旅',
                'url': reverse('register'),
            },
            {
                'title': '开始学习',
                'detail': '选择一门课程开始学习',
                'meta': '个性化学习',
                'url': hero_primary,
            },
        ]

        today_actions = [
            {
                'title': '浏览课程库',
                'detail': '查看系统提供的公开课程',
                'url': reverse('course_library'),
            },
            {
                'title': '注册账户',
                'detail': '创建账户并开始个性化学习',
                'url': reverse('register'),
            },
            {
                'title': '登录系统',
                'detail': '已有账户？立即登录',
                'url': reverse('login'),
            },
        ]

        teacher_course_cards = []
        teaching_metrics = {
            'total_courses': 0,
            'published_courses': 0,
            'draft_courses': 0,
            'material_count': 0,
            'processing_material_count': 0,
        }

        learning_metrics = {
            'weekly_practice_count': 0,
            'weekly_average_score': 0,
            'weekly_learning_days': 0,
            'tracked_focus_count': 0,
            'engagement_score': None,
            'goal_count': 0,
        }

    return {
        'dashboard': {
            'engagement_score': engagement_score,
            'has_profile': bool(profile),
            'display_name': display_name,
            'avatar_url': avatar_url,
            'user_initial': user_initial,
            'user_major': user_major,
            'user_grade': user_grade,
            'learning_goals': learning_goals[:3],
            'misconception_count': misconception_count,
            'cognitive_style': cognitive_style,
            'recent_learning': recent_learning,
            'focus_areas': focus_areas,
            'recent_conversations': recent_conversations,
            'growth_timeline': growth_timeline,
            'profile_event_cards': profile_event_cards,
            'today_actions': today_actions,
            'teacher_course_cards': teacher_course_cards,
            'teaching_metrics': teaching_metrics,
            'learning_metrics': learning_metrics,
            'hero_primary': hero_primary,
            'hero_secondary': hero_secondary,
            'has_teacher_workspace': teaching_metrics['total_courses'] > 0,
        }
    }


def home_view(request):
    """首页视图"""
    if request.user.is_authenticated:
        try:
            from agent_system.services.profile_events import record_profile_event
            record_profile_event(request.user, 'home_viewed', {'has_profile': bool(getattr(request.user, 'student_profile', None))}, source_app='site.home', confidence=0.15)
        except Exception:
            pass

    return render(request, 'home.html', _build_home_dashboard_context(request.user if request.user.is_authenticated else None))


urlpatterns = [
    path('admin/', admin.site.urls),
    path('auth/', include('auth_app.urls')),
    path('profile/', include('profile_app.urls')),
    path('curriculum/', include('curriculum_app.urls')),
    path('chat/', include('chat_app.urls')),
    path('agent/', include('agent_system.urls')),
    path('', home_view, name='home'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
