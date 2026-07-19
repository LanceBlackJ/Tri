import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from agent_system.models import Conversation, ProfileEvent, StudentProfile
from core.models import User
from curriculum_app.models import (
    Animation,
    Course,
    CourseMaterial,
    CourseOutline,
    LearningPlan,
    LearningProgress,
    MaterialChunk,
    MaterialQuestionStat,
    OutlineExport,
    Slide,
)
from profile_app.models import StudentProfile as ProfileAppStudentProfile


ERROR_MARKERS = ('Traceback (most recent call last)', 'Server Error (500)')


class PublicPagesSmokeTests(TestCase):
    """匿名访问下的页面与登录跳转检查。"""

    def test_landing_page_renders_for_anonymous_user(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'landing.html')

    def test_login_page_renders(self):
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)

    def test_register_page_renders(self):
        response = self.client.get(reverse('register'))
        self.assertEqual(response.status_code, 200)

    def test_login_required_pages_redirect_anonymous_to_login(self):
        login_url = reverse('login')
        protected_urls = [
            reverse('profile'),
            reverse('profile_dashboard'),
            reverse('profile_building'),
            reverse('profile_view'),
            reverse('detailed_profile'),
            reverse('chat_interface'),
            reverse('teacher_course_library'),
            reverse('course_generator'),
            reverse('learning_progress'),
            reverse('weak_area_practice'),
        ]
        for url in protected_urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(login_url))

    def test_public_course_library_renders_for_anonymous_user(self):
        response = self.client.get(reverse('course_library'))
        self.assertEqual(response.status_code, 200)
        for marker in ERROR_MARKERS:
            self.assertNotIn(marker, response.content.decode('utf-8'))


class AuthenticatedPagesSmokeTests(TestCase):
    """登录后逐页检查渲染是否正常（结项前的整体功能自检）。"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='smoke-user', email='smoke@example.com', password='pass12345',
        )

        cls.course = Course.objects.create(
            owner=cls.user,
            title='结项自检课程',
            summary='用于结项前整体功能自检的示例课程',
            status='published',
            visibility='login',
        )
        cls.material = CourseMaterial.objects.create(
            course=cls.course,
            uploaded_by=cls.user,
            title='自检资料',
            material_type='pdf',
            file=SimpleUploadedFile('smoke.pdf', b'fake-pdf-content', content_type='application/pdf'),
            processing_status='ready',
            page_count=12,
        )
        MaterialChunk.objects.create(
            material=cls.material,
            chunk_index=0,
            source_page='1',
            heading='第一章',
            keyword_summary='自检关键词',
            content='用于结项前功能自检的示例资料内容。',
        )

        cls.outline = CourseOutline.objects.create(
            user=cls.user,
            title='结项自检大纲',
            description='结项前自检用大纲',
            outline_data='{}',
            status='completed',
            progress=100,
        )
        Slide.objects.create(course_outline=cls.outline, chapter_id='ppt_main', slide_data='[]')
        Animation.objects.create(
            course_outline=cls.outline,
            chapter_id='chapter_1',
            concept_name='自检示例概念',
            animation_code='<html></html>',
        )
        OutlineExport.objects.create(
            course_outline=cls.outline,
            user=cls.user,
            filename='smoke.pptx',
            status='completed',
        )
        LearningProgress.objects.create(
            user=cls.user,
            course_outline=cls.outline,
            chapter_id='chapter_1',
            status='in_progress',
            completed_slides=3,
            total_slides=8,
            quiz_score=78,
        )

        cls.plan = LearningPlan.objects.create(
            user=cls.user,
            title='结项自检学习路线',
            plan_data=json.dumps({
                'title': '结项自检学习路线',
                'matched_course': {
                    'id': cls.course.id,
                    'title': cls.course.title,
                    'summary': cls.course.summary,
                    'topics': ['自检主题'],
                },
                'recent_progress': [
                    {
                        'outline_id': cls.outline.id,
                        'outline_title': cls.outline.title,
                        'chapter_id': 'chapter_1',
                        'status': 'in_progress',
                        'quiz_score': 78,
                    },
                ],
                'recommendation_reason': ['基于结项自检数据生成'],
                'modules': [
                    {
                        'name': '阶段一：自检模块',
                        'focus': '验证学习路径详情页渲染',
                        'lessons': [
                            {
                                'title': '自检小节',
                                'objectives': '确认页面在结项前可正常访问',
                                'resources': ['doc', 'quiz', 'animation'],
                            },
                        ],
                    },
                ],
            }, ensure_ascii=False),
            status='generated',
        )

        MaterialQuestionStat.objects.create(
            user=cls.user,
            course=cls.course,
            material=cls.material,
            question_fingerprint='smoke-fp-1',
            question_text='示例问题？',
            knowledge_tag='自检知识点',
            source_page='1',
            source_heading='第一章',
            wrong_count=2,
            consecutive_wrong_count=1,
        )

        StudentProfile.objects.create(
            user=cls.user,
            knowledge_profile={'自检知识点': 0.5},
            knowledge_timestamps={'自检知识点': timezone.now().isoformat()},
            learning_goals=['完成结项自检'],
            cognitive_style='视觉型',
        )
        ProfileAppStudentProfile.objects.create(
            user=cls.user,
            course_id='default',
            profile_data=json.dumps({
                'knowledge_profile': {'自检知识点': 0.5},
                'learning_goals': ['完成结项自检'],
                'misconceptions': [],
                'engagement': {'score': 70},
                'learning_preferences': {'preferred_mode': '实践'},
            }, ensure_ascii=False),
            confidence_scores='{}',
        )

        cls.conversation = Conversation.objects.create(user=cls.user, title='结项自检对话')
        ProfileEvent.objects.create(
            user=cls.user,
            event_type='material_quiz_submitted',
            payload={'material_title': cls.material.title},
            processed_at=timezone.now(),
        )

    def setUp(self):
        self.client.login(username='smoke-user', password='pass12345')

    def _assert_ok(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, msg=f'{url} -> {response.status_code}')
        content = response.content.decode('utf-8')
        for marker in ERROR_MARKERS:
            self.assertNotIn(marker, content, msg=f'{url} contains error marker {marker!r}')
        return response

    def test_home_dashboard(self):
        self._assert_ok(reverse('home'))

    def test_auth_profile_page(self):
        self._assert_ok(reverse('profile'))

    def test_profile_dashboard(self):
        self._assert_ok(reverse('profile_dashboard'))

    def test_profile_building_page(self):
        self._assert_ok(reverse('profile_building'))

    def test_profile_view_page(self):
        self._assert_ok(reverse('profile_view'))

    def test_detailed_profile_page(self):
        self._assert_ok(reverse('detailed_profile'))

    def test_course_library(self):
        self._assert_ok(reverse('course_library'))

    def test_teacher_course_library(self):
        self._assert_ok(reverse('teacher_course_library'))

    def test_teacher_course_detail(self):
        self._assert_ok(reverse('teacher_course_detail', args=[self.course.id]))

    def test_course_study_default(self):
        self._assert_ok(reverse('course_study', args=[self.course.id]))

    def test_course_study_with_material_and_page(self):
        url = reverse('course_study', args=[self.course.id]) + f'?material={self.material.id}&page=1'
        self._assert_ok(url)

    def test_course_ai_chat(self):
        url = reverse('course_ai_chat', args=[self.course.id]) + f'?material={self.material.id}&current_page=1'
        self._assert_ok(url)

    def test_course_generator(self):
        self._assert_ok(reverse('course_generator'))

    def test_learning_progress(self):
        self._assert_ok(reverse('learning_progress'))

    def test_weak_area_practice(self):
        self._assert_ok(reverse('weak_area_practice'))

    def test_learning_plan_detail(self):
        self._assert_ok(reverse('learning_plan_detail', args=[self.plan.id]))

    def test_course_outline(self):
        self._assert_ok(reverse('course_outline', args=[self.outline.id]))

    def test_chat_interface(self):
        self._assert_ok(reverse('chat_interface'))

    def test_agent_overview(self):
        self._assert_ok(reverse('agent_system:agent_overview'))

    def test_agent_generator_page(self):
        self._assert_ok(reverse('agent_system:generator_page'))
