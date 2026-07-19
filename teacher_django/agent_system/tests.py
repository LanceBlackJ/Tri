import json

from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import User
from curriculum_app.models import Course, CourseMaterial, LearningPlan, MaterialChunk
from .agents import (
    PeerLearnerAgent,
    QuizAgent,
    SelfExplanationAgent,
    StudentSimulatorAgent,
    build_analogy_seed,
    get_user_profile_dict,
)
from .models import Conversation, Message, ProfileEvent, StudentProfile, LearningResource
from .generation import GenerationManager, animation_code_is_safe, build_animation_prompt, build_animation_retry_prompt, build_slide_deck_prompt, normalize_animation_assets, normalize_slide_deck, sanitize_animation_code, slides_to_markdown
from .services.profile_auto_updater import BKTUpdater, KnowledgeUpdater, ProfileAutoUpdater, build_review_queue
from .services.profile_builder import ProfileBuilder
from .views import _build_conversation_system_prompt, _detect_response_mode, _format_learning_plan_context, SELF_EXPLANATION_PROMPT

from django.core.management import call_command


class ProcessProfileEventsCommandTests(TestCase):
    def test_process_profile_events_command_applies_pending_events(self):
        user = User.objects.create_user(username='ppe_user', email='ppe@example.com', password='x')
        # ensure no profile yet
        self.assertFalse(StudentProfile.objects.filter(user=user).exists())
        payload = {'score': 65, 'knowledge_tags': ['测试知识点'], 'review_recommendations': []}
        ev = ProfileEvent.objects.create(user=user, event_type='material_quiz_submitted', payload=payload, confidence=0.6)
        self.assertIsNone(ev.processed_at)
        call_command('process_profile_events', '--limit', '10')
        ev.refresh_from_db()
        self.assertIsNotNone(ev.processed_at)
        prof = StudentProfile.objects.get(user=user)
        self.assertIn('测试知识点', prof.knowledge_profile)
        self.assertEqual(prof.engagement.get('last_quiz_score'), round(65, 1))


class KnowledgeUpdaterNormalizeLevelTests(SimpleTestCase):
    def test_normalize_level_passes_through_fraction(self):
        self.assertEqual(KnowledgeUpdater.normalize_level(0.6), 0.6)

    def test_normalize_level_converts_dict_mastery_score(self):
        value = {'mastery_score': 80, 'source': 'material_quiz'}
        self.assertEqual(KnowledgeUpdater.normalize_level(value), 0.8)

    def test_normalize_level_converts_percentage_scale(self):
        self.assertEqual(KnowledgeUpdater.normalize_level(75), 0.75)

    def test_normalize_level_converts_text_labels(self):
        self.assertEqual(KnowledgeUpdater.normalize_level('高级'), 0.95)
        self.assertEqual(KnowledgeUpdater.normalize_level('中级'), 0.65)
        self.assertEqual(KnowledgeUpdater.normalize_level('初级'), 0.3)

    def test_normalize_level_falls_back_to_default_for_unknown(self):
        self.assertEqual(KnowledgeUpdater.normalize_level(None), 0.5)
        self.assertEqual(KnowledgeUpdater.normalize_level('未知'), 0.5)


class ProfileAutoUpdaterKnowledgeFromQuizTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='kp-user', email='kp@example.com', password='pass12345')

    def test_update_knowledge_from_quiz_handles_dict_formatted_entry(self):
        """material_quiz_submitted 写入的 {'mastery_score': ...} 字典格式不应导致 TypeError。"""
        StudentProfile.objects.create(
            user=self.user,
            knowledge_profile={'函数极限': {'mastery_score': 60, 'source': 'material_quiz'}},
        )

        updater = ProfileAutoUpdater(self.user)
        delta = updater.update_knowledge_from_quiz(
            knowledge_tags=['函数极限'],
            is_correct=True,
            difficulty='standard',
        )

        self.assertIn('knowledge_profile', delta)
        profile = StudentProfile.objects.get(user=self.user)
        self.assertEqual(profile.knowledge_profile['函数极限'], 0.884)
        self.assertIn('函数极限', profile.knowledge_timestamps)

    def test_update_knowledge_from_quiz_handles_text_label_entry(self):
        """对话画像构建写入的文本等级（'初级'/'中级'/'高级'）不应导致 TypeError。"""
        StudentProfile.objects.create(
            user=self.user,
            knowledge_profile={'指针': '初级'},
        )

        updater = ProfileAutoUpdater(self.user)
        updater.update_knowledge_from_quiz(
            knowledge_tags=['指针'],
            is_correct=False,
            difficulty='standard',
        )

        profile = StudentProfile.objects.get(user=self.user)
        self.assertEqual(profile.knowledge_profile['指针'], 0.146)
        self.assertIn('指针', profile.knowledge_timestamps)


class BKTUpdaterTests(SimpleTestCase):
    def test_correct_answer_increases_mastery_standard(self):
        new_level, _ = BKTUpdater.calculate_new_level(0.5, True, 'standard', 0)
        self.assertAlmostEqual(new_level, 0.836, places=3)

    def test_incorrect_answer_decreases_mastery_standard(self):
        new_level, _ = BKTUpdater.calculate_new_level(0.5, False, 'standard', 0)
        self.assertAlmostEqual(new_level, 0.2, places=3)

    def test_challenge_difficulty_rewards_correct_more_than_reinforce(self):
        challenge_level, _ = BKTUpdater.calculate_new_level(0.5, True, 'challenge', 0)
        reinforce_level, _ = BKTUpdater.calculate_new_level(0.5, True, 'reinforce', 0)
        self.assertAlmostEqual(challenge_level, 0.919, places=3)
        self.assertAlmostEqual(reinforce_level, 0.796, places=3)
        self.assertGreater(challenge_level, reinforce_level)

    def test_forgetting_decay_pulls_mastery_toward_baseline_from_above(self):
        decayed = BKTUpdater.apply_forgetting_decay(0.9, days_elapsed=30)
        self.assertAlmostEqual(decayed, 0.5506, places=4)

    def test_forgetting_decay_never_raises_mastery_below_baseline(self):
        # 遗忘不应让低于基线的弱知识点"自动变好"：应原样保持，不向基线上升
        decayed = BKTUpdater.apply_forgetting_decay(0.1, days_elapsed=30)
        self.assertAlmostEqual(decayed, 0.1, places=6)

    def test_boundary_values_stay_within_unit_interval(self):
        low, _ = BKTUpdater.calculate_new_level(0.0, False, 'standard', 0)
        high, _ = BKTUpdater.calculate_new_level(1.0, True, 'standard', 0)
        self.assertAlmostEqual(low, 0.1, places=3)
        self.assertAlmostEqual(high, 1.0, places=3)

    def test_unknown_difficulty_falls_back_to_standard(self):
        fallback_level, _ = BKTUpdater.calculate_new_level(0.5, True, 'unknown_stage', 0)
        standard_level, _ = BKTUpdater.calculate_new_level(0.5, True, 'standard', 0)
        self.assertEqual(fallback_level, standard_level)

    def test_predict_days_to_threshold_for_high_mastery(self):
        days = BKTUpdater.predict_days_to_threshold(0.9, 0.6)
        self.assertAlmostEqual(days, 22.907, places=3)
        # 反函数自洽性：衰减该天数后应恰好回到阈值
        self.assertAlmostEqual(BKTUpdater.apply_forgetting_decay(0.9, days), 0.6, places=6)

    def test_predict_days_to_threshold_returns_zero_when_already_below(self):
        self.assertEqual(BKTUpdater.predict_days_to_threshold(0.5, 0.6), 0.0)

    def test_predict_days_to_threshold_returns_inf_when_threshold_at_or_below_baseline(self):
        self.assertEqual(BKTUpdater.predict_days_to_threshold(0.9, 0.4), float('inf'))

    def test_days_elapsed_since_handles_none_and_iso_timestamp(self):
        self.assertEqual(BKTUpdater.days_elapsed_since(None), 0.0)

        now = timezone.now()
        ten_days_ago = now - timedelta(days=10)
        days = BKTUpdater.days_elapsed_since(ten_days_ago.isoformat(), now=now)
        self.assertAlmostEqual(days, 10.0, places=5)


class ReviewQueueTests(SimpleTestCase):
    def test_build_review_queue_sorts_by_urgency(self):
        now = timezone.now()
        knowledge_profile = {'A': 0.9, 'B': 0.9, 'C': 0.5}
        knowledge_timestamps = {
            'A': (now - timedelta(days=30)).isoformat(),
            'B': now.isoformat(),
            'C': (now - timedelta(days=5)).isoformat(),
        }

        items = build_review_queue(knowledge_profile, knowledge_timestamps, now=now)

        self.assertEqual([item['tag'] for item in items], ['A', 'C', 'B'])
        self.assertTrue(items[0]['is_due'])
        self.assertTrue(items[1]['is_due'])
        self.assertFalse(items[2]['is_due'])

    def test_build_review_queue_excludes_items_that_never_decay_below_threshold(self):
        now = timezone.now()
        knowledge_profile = {'A': 0.9, 'B': 0.3}
        knowledge_timestamps = {'A': now.isoformat(), 'B': now.isoformat()}

        items = build_review_queue(knowledge_profile, knowledge_timestamps, threshold=0.4, now=now)

        tags = [item['tag'] for item in items]
        self.assertNotIn('A', tags)
        self.assertIn('B', tags)

    def test_build_review_queue_respects_limit(self):
        now = timezone.now()
        knowledge_profile = {f'tag{i}': 0.3 for i in range(7)}
        knowledge_timestamps = {f'tag{i}': (now - timedelta(days=i)).isoformat() for i in range(7)}

        items = build_review_queue(knowledge_profile, knowledge_timestamps, limit=3, now=now)

        self.assertEqual(len(items), 3)

    def test_build_review_queue_skips_dunder_metadata_keys(self):
        now = timezone.now()
        knowledge_profile = {
            'A': 0.3,
            '__domains__': ['数学', '编程'],
            '__level__': '中级',
        }
        knowledge_timestamps = {'A': now.isoformat()}

        items = build_review_queue(knowledge_profile, knowledge_timestamps, now=now)

        tags = [item['tag'] for item in items]
        self.assertEqual(tags, ['A'])


class ResourceEventTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='res_user', email='res@example.com', password='pw')
        self.client.force_login(self.user)

    def test_api_search_resources_records_event(self):
        # create a resource to be found
        LearningResource.objects.create(title='测试资源', resource_type='doc', content='内容', author=self.user)
        url = reverse('agent_system:api_search_resources')
        response = self.client.get(url, {'q': '测试资源'})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get('ok'))
        event = ProfileEvent.objects.filter(user=self.user, event_type='resource_search').latest('id')
        self.assertIsNotNone(event)
        self.assertTrue(event.processed_at)

    def test_api_search_resources_only_returns_own_resources(self):
        other = User.objects.create_user(username='other_user', email='other@example.com', password='pw')
        LearningResource.objects.create(title='我的资源', resource_type='doc', content='内容', author=self.user)
        LearningResource.objects.create(title='别人的资源', resource_type='doc', content='内容', author=other)

        url = reverse('agent_system:api_search_resources')
        response = self.client.get(url, {'q': '资源'})

        self.assertEqual(response.status_code, 200)
        titles = [r['title'] for r in response.json()['results']]
        self.assertIn('我的资源', titles)
        self.assertNotIn('别人的资源', titles)

    def test_api_export_resource_records_event(self):
        res = LearningResource.objects.create(title='导出资源', resource_type='doc', content='abc', author=self.user)
        url = reverse('agent_system:api_export_resource', args=[res.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        event = ProfileEvent.objects.filter(user=self.user, event_type='resource_exported').latest('id')
        self.assertIsNotNone(event.processed_at)


class ResourceOwnershipTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner_user', email='owner@example.com', password='pw')
        self.other = User.objects.create_user(username='other_user2', email='other2@example.com', password='pw')
        self.client.force_login(self.user)

    def test_api_quiz_grade_rejects_other_users_resource(self):
        quiz = LearningResource.objects.create(
            title='别人的练习题', resource_type='quiz', author=self.other,
            content=json.dumps({'questions': [{'id': '1', 'correct_answer': 'A', 'score': 1}]}),
        )
        url = reverse('agent_system:api_quiz_grade')
        response = self.client.post(
            url, data=json.dumps({'resource_id': quiz.id, 'answers': {'1': 'A'}}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_api_compute_embedding_rejects_other_users_resource(self):
        res = LearningResource.objects.create(title='别人的资源', resource_type='doc', content='内容', author=self.other)
        url = reverse('agent_system:api_compute_embedding', kwargs={'pk': res.id})
        response = self.client.post(url, data=json.dumps({'text': '内容'}), content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_api_quiz_grade_allows_own_resource(self):
        quiz = LearningResource.objects.create(
            title='我的练习题', resource_type='quiz', author=self.user,
            content=json.dumps({'questions': [{'id': '1', 'correct_answer': 'A', 'score': 1}]}),
        )
        url = reverse('agent_system:api_quiz_grade')
        response = self.client.post(
            url, data=json.dumps({'resource_id': quiz.id, 'answers': {'1': 'A'}}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['score'], 1)


class ConversationPromptModeTests(SimpleTestCase):
    def test_detects_direct_explanation_requests(self):
        mode = _detect_response_mode('请帮我讲解“下列哪项不是总线的特点？”为什么总是出错。')
        self.assertEqual(mode, 'direct_explanation')

    def test_detects_guided_planning_requests(self):
        mode = _detect_response_mode('请帮我规划一下这门课接下来两周怎么学。')
        self.assertEqual(mode, 'guided_planning')

    def test_ta_and_learning_prompts_include_multimodal_guide(self):
        # 多模态答疑：TA 与学习模式的提示词都要引导 AI 用公式(LaTeX)与图解(mermaid)
        for mode in ('ta', 'learning'):
            prompt = _build_conversation_system_prompt(
                '帮我讲讲梯度下降的流程', guidance_summary='', known_summary='',
                material_context={'text': 'x'}, tutor_mode=mode)
            self.assertIn('mermaid', prompt, mode)
            self.assertIn('LaTeX', prompt, mode)
            self.assertIn('多模态表达', prompt, mode)

    def test_direct_explanation_prompt_forbids_background_interview(self):
        prompt = _build_conversation_system_prompt(
            '请帮我讲解“下列哪项不是总线的特点？”为什么总是出错。',
            guidance_summary='直接进入讲解。',
            known_summary='已知课程为计算机组成原理。',
            material_context={'text': '当前课程：计算机组成原理'}
        )
        self.assertIn('此类请求必须先直接回答', prompt)
        self.assertIn('不要先询问学生在学什么课程', prompt)
        self.assertIn('默认问题属于这门课', prompt)

    def test_direct_explanation_prompt_requires_fixed_five_section_output(self):
        prompt = _build_conversation_system_prompt(
            '请帮我讲解“下列哪项不是总线的特点？”为什么总是出错。',
            guidance_summary='直接进入讲解。',
            known_summary='已知课程为计算机组成原理。',
            material_context={'text': '当前课程：计算机组成原理'}
        )
        self.assertIn('回答必须严格使用以下 5 段小标题', prompt)
        self.assertIn('1. 结论', prompt)
        self.assertIn('5. 立刻自测', prompt)

    def test_ta_persona_is_direct_and_not_socratic(self):
        prompt = _build_conversation_system_prompt(
            '这一页的负梯度为什么是下降最快的方向？',
            guidance_summary='',
            known_summary='',
            material_context={},
            tutor_mode='ta',
        )
        self.assertIn('助教', prompt)
        self.assertIn('直接', prompt)  # 直接回答、不绕弯子
        # 助教绝不能是苏格拉底式（不直接给答案）
        self.assertNotIn('不直接给答案', prompt)
        self.assertNotIn('苏格拉底式对话规则', prompt)

    def test_chat_and_learning_personas_unchanged(self):
        chat_prompt = _build_conversation_system_prompt('随便聊聊', '', '', tutor_mode='chat')
        self.assertIn('友好、乐于助人的AI助手', chat_prompt)
        learning_prompt = _build_conversation_system_prompt('帮我规划学习', '', '', tutor_mode='learning')
        self.assertIn('苏格拉底式教学法', learning_prompt)

    def test_course_context_prompt_prefers_three_part_answer_structure(self):
        prompt = _build_conversation_system_prompt(
            '这门课里梯度方向为什么总容易弄反？',
            guidance_summary='先给结论。',
            known_summary='已知课程为机器学习。',
            material_context={'text': '当前课程：机器学习\n课程知识地图：梯度下降'}
        )
        self.assertIn('默认按三段输出', prompt)
        self.assertIn('先给结论', prompt)
        self.assertIn('再给资料依据或推理过程', prompt)
        self.assertIn('最后给下一步行动建议', prompt)

    def test_prompt_avoids_material_basis_wording(self):
        prompt = _build_conversation_system_prompt(
            '请解释这一页的核心概念。',
            guidance_summary='直接回答。',
            known_summary='已知课程为计算机组成原理。',
            material_context={'text': '当前课程：计算机组成原理'}
        )
        self.assertNotIn('缺少资料依据', prompt)

    def test_learning_plan_context_formatter_preserves_adjusted_priority(self):
        summary = _format_learning_plan_context({
            'title': '梯度下降学习路线（已按当前状态调整）',
            'weak_areas': ['梯度方向', '学习率'],
            'top_module_name': '阶段0：先补当前薄弱点',
            'top_module_focus': '先把最近错误最多的知识点补齐。',
            'top_lessons': [
                {'title': '回看梯度方向', 'objectives': '重新理解梯度和负梯度的区别'},
            ],
            'recommendation_reason': ['检测到当前薄弱点：梯度方向，已将补弱阶段提前。'],
        })

        self.assertIn('最近同步的学习路径：梯度下降学习路线（已按当前状态调整）', summary)
        self.assertIn('当前优先补弱：梯度方向、学习率', summary)
        self.assertIn('当前优先阶段：阶段0：先补当前薄弱点', summary)
        self.assertIn('优先任务1：回看梯度方向', summary)
        self.assertIn('推荐原因：检测到当前薄弱点：梯度方向，已将补弱阶段提前。', summary)

    def test_material_quiz_prompt_forbids_page_summary_style_questions(self):
        prompt = QuizAgent.build_material_quiz_prompt('计算机组成原理 / 第二章', '知识点：总线判优（第3页/张）\n总线判优用于决定哪个主设备可使用总线。', 5)
        self.assertIn('不\u8981出‘请根据资料简述第1页核心内容’', prompt)
        self.assertIn('允许结合你已有的学科知识', prompt)

    def test_parse_quiz_json_does_not_fabricate_example_questions_on_failure(self):
        agent = QuizAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = 'still not json'

        payload = agent._parse_quiz_json('not json at all', '计算机组成原理 / 第二章', 5)

        self.assertEqual(payload, {'questions': []})

    def test_learning_mode_prompt_includes_analogy_seed(self):
        prompt = _build_conversation_system_prompt(
            '请帮我讲讲反向传播。',
            guidance_summary='先给结论。',
            known_summary='已知课程为机器学习。',
            material_context={},
            tutor_mode='learning',
            analogy_seed='梯度下降',
        )

        self.assertIn('类比教学提示', prompt)
        self.assertIn('梯度下降', prompt)

    def test_learning_mode_prompt_omits_analogy_section_when_seed_empty(self):
        prompt = _build_conversation_system_prompt(
            '请帮我讲讲反向传播。',
            guidance_summary='先给结论。',
            known_summary='已知课程为机器学习。',
            material_context={},
            tutor_mode='learning',
            analogy_seed='',
        )

        self.assertNotIn('类比教学提示', prompt)

    def test_peer_teaching_prompt_includes_recent_topics(self):
        prompt = _build_conversation_system_prompt(
            '你好',
            guidance_summary='',
            known_summary='',
            material_context={},
            tutor_mode='peer_teaching',
            recent_topics='梯度下降、反向传播',
        )

        self.assertIn('梯度下降、反向传播', prompt)
        self.assertIn('最近学了', prompt)

    def test_peer_teaching_prompt_omits_recent_topics_when_empty(self):
        prompt = _build_conversation_system_prompt(
            '你好',
            guidance_summary='',
            known_summary='',
            material_context={},
            tutor_mode='peer_teaching',
        )

        self.assertNotIn('最近学了这些内容', prompt)


class SafetyProtectedPhraseTests(SimpleTestCase):
    """敏感词过滤不能误伤合法的教育/历史术语（鸦片战争、南京大屠杀等）。"""

    def test_legit_history_terms_not_censored(self):
        from agent_system.services.safety import censor_text, check_text
        for t in ['鸦片战争的背景', '南京大屠杀', '虎门销烟', '禁毒教育', '毒品危害与预防',
                  '非暴力不合作运动']:
            self.assertEqual(censor_text(t), t, f'{t} 被误打码')
            self.assertTrue(check_text(t)['safe'], f'{t} 被误判违禁')

    def test_genuine_harmful_content_still_censored(self):
        from agent_system.services.safety import censor_text, check_text
        self.assertEqual(censor_text('吸食海洛因'), '吸食***')
        self.assertEqual(censor_text('制造冰毒'), '制造**')
        self.assertFalse(check_text('贩卖毒品')['safe'])


class PlannerParseOutlineTests(TestCase):
    """PlannerAgent 解析大纲响应时，遇到非 JSON/占位/纯文本不应崩溃，应回退默认大纲。"""

    def test_parse_outline_response_handles_non_json_without_crash(self):
        from agent_system.planner_agent import PlannerAgent
        p = PlannerAgent(User.objects.create_user(username='planner-parse', email='pp@x.com', password='p12345678'))
        for bad in ['[占位] 接口不可用', '就是一段没有大括号的纯文本', '[1,2,3]', '']:
            out = p._parse_outline_response(bad, 'C++', {})
            self.assertIsInstance(out, dict, f'输入 {bad!r} 未返回 dict')
            self.assertIn('chapters', out)


class CurriculumStandardsTopicFocusTests(SimpleTestCase):
    """标准库不该把无关学科概念硬塞给主题——否则大纲会跑偏（历史课冒出编程章）。"""

    def setUp(self):
        from agent_system.curriculum_standards import CurriculumStandards
        self.std = CurriculumStandards()

    def test_unknown_topic_is_general_not_cs(self):
        # 识别不出学科的主题应归为 general，而不是默认塞成计算机
        self.assertEqual(self.std._detect_subject('中国近代史'), 'general')
        self.assertEqual(self.std._detect_subject('唐诗宋词鉴赏'), 'general')

    def test_known_subjects_still_detected(self):
        self.assertEqual(self.std._detect_subject('Python编程'), 'cs')
        self.assertEqual(self.std._detect_subject('高等数学'), 'math')
        self.assertEqual(self.std._detect_subject('细胞生物学'), 'biology')

    def test_no_concept_dump_for_unmatched_topic(self):
        # 匹配不到具体知识点时返回空，而不是把整套学科概念(编程/数据结构/机器学习)全塞进来
        self.assertEqual(self.std.query_standards('中国近代史')['concepts'], [])
        self.assertEqual(self.std._find_concepts('C++'), [])


class BuildAnalogySeedTests(SimpleTestCase):
    def test_returns_empty_string_for_non_dict_or_empty_profile(self):
        self.assertEqual(build_analogy_seed({}), '')
        self.assertEqual(build_analogy_seed(None), '')

    def test_selects_concepts_with_string_level_high_or_medium(self):
        profile = {
            '梯度下降': '高级',
            '矩阵乘法': '中级',
            '反向传播': '初级',
            'overall': '高级',
        }

        seed = build_analogy_seed(profile)

        self.assertIn('梯度下降', seed)
        self.assertIn('矩阵乘法', seed)
        self.assertNotIn('反向传播', seed)
        self.assertNotIn('overall', seed)
        self.assertLess(seed.index('梯度下降'), seed.index('矩阵乘法'))

    def test_selects_concepts_with_mastery_score_above_threshold(self):
        profile = {
            '梯度下降': {'mastery_score': 85, 'source': 'self_explanation'},
            '矩阵乘法': {'mastery_score': 60},
            '反向传播': {'mastery_score': 75},
        }

        seed = build_analogy_seed(profile)

        self.assertIn('梯度下降', seed)
        self.assertIn('反向传播', seed)
        self.assertNotIn('矩阵乘法', seed)

    def test_respects_limit(self):
        profile = {'概念A': '高级', '概念B': '高级', '概念C': '高级'}

        seed = build_analogy_seed(profile, limit=2)

        self.assertEqual(len(seed.split('、')), 2)

    def test_returns_empty_string_when_no_concept_qualifies(self):
        profile = {'梯度下降': '初级', '矩阵乘法': {'mastery_score': 50}}

        self.assertEqual(build_analogy_seed(profile), '')


class CoursewareGenerationStructureTests(SimpleTestCase):
    def test_normalize_slide_deck_repairs_missing_fields(self):
        slides = normalize_slide_deck(
            '{"slides":[{"title":"总线是什么","bullets":["连接部件","传输数据"]}]}',
            '计算机组成原理中的总线',
            {'blueprint': {}},
        )

        self.assertGreaterEqual(len(slides), 6)
        self.assertEqual(slides[0]['title'], '总线是什么')
        self.assertEqual(slides[0]['layout'], 'cover')
        self.assertEqual(slides[0]['theme'], 'academic_light')
        self.assertIn('visual_blocks', slides[0])
        self.assertIn('teacher_action', slides[0])
        self.assertIn('student_interaction', slides[0])
        self.assertIn('speaker_notes', slides[0])
        self.assertIn('teaching_task', slides[0])

        rendered = slides_to_markdown(slides, '计算机组成原理中的总线')
        self.assertIn('总线是什么', rendered)

    def test_planner_blueprint_to_display_replaces_generic_skeleton(self):
        # PlannerAgent 真实生成的章节应被转换成前端蓝图结构（不再是通用模板）
        from .generation import _planner_blueprint_to_display
        generated = {'blueprint': {
            'objectives': ['掌握C++核心语法', '能写出健壮的C++程序'],
            'chapters': [
                {'number': 1, 'title': 'C++编程基础', 'duration': '30',
                 'teaching_goal': '掌握变量、数据类型与控制结构',
                 'key_points': ['变量与类型', '控制结构']},
                {'number': 2, 'title': '指针与内存管理', 'duration': '45',
                 'teaching_goal': '理解指针与动态内存',
                 'core_concepts': ['指针', 'new/delete']},
            ],
        }}
        disp = _planner_blueprint_to_display(generated, 'C++')
        self.assertIsNotNone(disp)
        titles = [c['title'] for c in disp['chapters']]
        self.assertEqual(titles[0], '第1章 C++编程基础')
        self.assertEqual(titles[1], '第2章 指针与内存管理')
        # 目标来自 key_points/core_concepts，而不是"掌握本章最核心的1-2个关键点"这类占位
        self.assertEqual(disp['chapters'][0]['objectives'], ['变量与类型', '控制结构'])
        self.assertEqual(disp['chapters'][1]['objectives'], ['指针', 'new/delete'])
        # 时长换算：30分钟→0.5h、45分钟→0.8h附近
        self.assertAlmostEqual(disp['chapters'][0]['estimated_hours'], 0.5, places=1)
        self.assertEqual(disp['chapter_count'], 2)
        self.assertEqual(disp['objectives'], ['掌握C++核心语法', '能写出健壮的C++程序'])

    def test_planner_blueprint_to_display_returns_none_without_chapters(self):
        from .generation import _planner_blueprint_to_display
        self.assertIsNone(_planner_blueprint_to_display({'blueprint': {'chapters': []}}, 'C++'))
        self.assertIsNone(_planner_blueprint_to_display({}, 'C++'))

    def test_animation_assembled_from_llm_is_renderable_and_safe(self):
        # 大模型返回的 html/css/js 应被拼成单 doctype、含样式与脚本、且安全的自包含文档
        import json as _json
        from .generation import normalize_animation_assets, animation_code_is_safe
        payload = _json.dumps({'need_animation': True, 'animations': [{
            'concept_name': '冒泡排序', 'animation_type': 'css',
            'html': "<div class='bar'></div>",
            'css': '.bar{width:50px;height:50px;animation:mv 2s infinite}@keyframes mv{50%{transform:translateY(20px)}}',
            'js': "setInterval(function(){},600);",
        }]}, ensure_ascii=False)
        anims = normalize_animation_assets(payload, '冒泡排序', {'blueprint': {}}, fallback=False)
        self.assertEqual(len(anims), 1)
        code = anims[0]['animation_code']
        self.assertEqual(code.lower().count('<!doctype'), 1)
        self.assertIn('<style', code.lower())
        self.assertIn('@keyframes', code)
        self.assertIn('setInterval', code)          # 动画所需的定时器被保留
        self.assertTrue(animation_code_is_safe(code))

    def test_default_animation_single_doctype(self):
        from .generation import _generate_default_animations, animation_code_is_safe
        for t in ['梯度下降', '冒泡排序']:
            d = _generate_default_animations(t)
            self.assertTrue(d)
            code = d[0]['animation_code']
            self.assertEqual(code.lower().count('<!doctype'), 1, f'{t} 双 doctype')
            self.assertTrue(animation_code_is_safe(code))

    def test_sanitize_removes_fetch_keeps_animation_js(self):
        from .generation import sanitize_animation_code
        code = '<script>fetch("http://x");function draw(){requestAnimationFrame(draw);}draw();</script><div>x</div>'
        out = sanitize_animation_code(code)
        self.assertNotIn('fetch(', out)                       # 危险调用移除
        self.assertIn('function draw', out)                   # 正常函数不误伤
        self.assertIn('requestAnimationFrame', out)           # 动画驱动保留

    def test_placeholder_slide_content_detected(self):
        # 大模型把 prompt 的 JSON 示例原样抄回来时，应被识别为占位、不当作有效内容
        from .generation import _slide_content_is_placeholder
        self.assertTrue(_slide_content_is_placeholder({'speaker_notes': '完整讲稿，不少于原稿长度'}))
        self.assertTrue(_slide_content_is_placeholder({'bullets': ['完整句子', '完整句子']}))
        self.assertFalse(_slide_content_is_placeholder(
            {'speaker_notes': '二次函数是形如 ax^2+bx+c 的函数，图像是抛物线'}))

    def test_synth_speaker_notes_uses_bullets_not_stub(self):
        # 没讲稿时用要点合成能念的讲稿，而不是"讲解X的核心要点"空话
        from .generation import _synth_speaker_notes
        notes = _synth_speaker_notes('二次函数', '顶点式', ['顶点坐标是(h,k)', '开口方向由a决定'])
        self.assertIn('顶点坐标', notes)
        self.assertNotIn('核心要点', notes)

    def test_normalize_slide_deck_fallback_notes_are_teachable(self):
        # 缺讲稿的页经 normalize 后，讲稿应包含本页要点内容，而不是通用空话
        slides = normalize_slide_deck(
            {'slides': [{'title': '判别式', 'bullets': ['判别式Δ=b²-4ac', 'Δ>0有两个实根']}]},
            '二次函数', {'blueprint': {}})
        self.assertIn('判别式Δ', slides[0]['speaker_notes'])
        self.assertNotIn('核心要点', slides[0]['speaker_notes'])

    def test_normalize_slide_deck_falls_back_to_teachable_deck(self):
        slides = normalize_slide_deck('not json', '梯度下降', {'blueprint': {}})

        self.assertGreaterEqual(len(slides), 8)
        self.assertEqual(slides[0]['type'], 'cover')
        self.assertEqual(slides[0]['layout'], 'cover')
        self.assertIn('speaker_notes', slides[0])
        self.assertTrue(any(slide['layout'] == 'case_study' for slide in slides))
        self.assertTrue(any(slide['layout'] == 'comparison' for slide in slides))

        all_text = json.dumps(slides, ensure_ascii=False)
        self.assertIn('损失函数', all_text)
        self.assertIn('学习率', all_text)
        self.assertIn('负梯度', all_text)
        self.assertNotIn('概念容易碎片化', all_text)

    def test_slide_deck_prompt_requires_layout_and_visual_blocks(self):
        prompt = build_slide_deck_prompt('计算机组成原理中的总线', {'blueprint': {'chapters': []}})

        self.assertIn('layout', prompt)
        self.assertIn('visual_blocks', prompt)
        self.assertIn('teaching_strategy', prompt)
        self.assertIn('deck_decisions', prompt)
        self.assertIn('speaker_notes', prompt)
        self.assertIn('bullets', prompt)
        self.assertIn('process_flow', prompt)

    def test_animation_code_sanitizer_blocks_network_and_storage_access(self):
        code = '<script>fetch("/x"); localStorage.setItem("a","b"); eval("1+1")</script>'
        cleaned = sanitize_animation_code(code)

        self.assertNotIn('fetch(', cleaned)
        self.assertNotIn('localStorage', cleaned)
        self.assertNotIn('eval(', cleaned)
        self.assertTrue(animation_code_is_safe(cleaned))

    def test_normalize_animation_assets_builds_sandbox_ready_document(self):
        animations = normalize_animation_assets(
            '{"animations":[{"concept_name":"总线仲裁","animation_type":"css","html":"<button>开始</button>","css":"button{color:#2563eb}","js":""}]}',
            '计算机组成原理中的总线',
            {'blueprint': {}},
        )

        self.assertEqual(len(animations), 1)
        self.assertEqual(animations[0]['concept_name'], '总线仲裁')
        self.assertIn('<!doctype html>', animations[0]['animation_code'])
        self.assertTrue(animations[0]['safe'])

    def test_normalize_animation_assets_includes_js_and_keeps_animation_timers(self):
        # 关键回归：动画的 js 必须被拼进 animation_code（此前被丢弃 → 动画动不起来），
        # 且 setInterval/requestAnimationFrame 等动画所需 API 不能被当作危险代码删掉。
        animations = normalize_animation_assets(
            '{"animations":[{"concept_name":"排序动画","animation_type":"canvas",'
            '"html":"<canvas id=\\"c\\"></canvas>","css":"canvas{width:100%}",'
            '"js":"var t=setInterval(function(){requestAnimationFrame(draw);},50);"}]}',
            '排序算法',
            {'blueprint': {}},
        )
        self.assertEqual(len(animations), 1)
        code = animations[0]['animation_code']
        self.assertIn('<script>', code)
        self.assertIn('setInterval', code)  # 定时器保留
        self.assertIn('requestAnimationFrame', code)

    def test_normalize_animation_assets_still_strips_dangerous_js(self):
        animations = normalize_animation_assets(
            '{"animations":[{"concept_name":"演示","animation_type":"css",'
            '"html":"<div>x</div>","css":"","js":"fetch(\\"/x\\"); var t=setInterval(step,30);"}]}',
            '主题', {'blueprint': {}},
        )
        code = animations[0]['animation_code']
        self.assertNotIn('fetch(', code)      # 危险调用被移除
        self.assertIn('setInterval', code)    # 动画定时器保留

    def test_build_animation_retry_prompt_requires_non_repeated_runnable_assets(self):
        prompt = build_animation_retry_prompt(
            '计算机组成原理中的总线',
            {'blueprint': {'chapters': []}},
            [{'chapter_id': 'chapter_1', 'concept_name': '总线仲裁', 'animation_type': 'css', 'usage_note': '演示总线竞争'}],
        )

        self.assertIn('不要重复已有概念', prompt)
        self.assertIn('不要输出空 html', prompt)
        self.assertIn('总线仲裁', prompt)

    def test_animation_prompt_allows_ai_to_skip_unnecessary_animation(self):
        prompt = build_animation_prompt('马克思主义基本原理概论', {'blueprint': {'chapters': []}})

        self.assertIn('need_animation', prompt)
        self.assertIn('如果主题更适合静态讲解', prompt)
        self.assertIn('animations 返回空数组', prompt)

    def test_normalize_animation_assets_respects_explicit_no_animation_decision(self):
        animations = normalize_animation_assets(
            '{"need_animation": false, "reason": "该主题更适合静态讲解", "animations": []}',
            '马克思主义基本原理概论',
            {'blueprint': {}},
            fallback=False,
        )

        self.assertEqual(animations, [])


class StagedPptGenerationTests(TestCase):
    """验证分阶段PPT生成：先生成骨架，再并行生成各页详细内容。"""

    def setUp(self):
        self.user = User.objects.create_user(username='ppt_gen_user', email='ppt_gen@example.com', password='test1234')
        self.manager = GenerationManager(self.user, '梯度下降')
        self.manager._safe_text = lambda text: (text, {'safe': True, 'labels': []})

    def test_generate_deck_skeleton_parses_ai_structure(self):
        skeleton_json = json.dumps({
            'slides': [
                {'layout': 'cover', 'theme': 'academic_light', 'title': '梯度下降导论',
                 'teaching_goal': '了解课程整体安排', 'chapter_id': '', 'content_brief': '封面',
                 'needs_code': False, 'needs_animation': False, 'needs_quiz': False},
                {'layout': 'two_column', 'theme': 'tech_blue', 'title': '用代码实现梯度下降',
                 'teaching_goal': '能写出一次参数更新', 'chapter_id': '', 'content_brief': '展示梯度下降的Python实现',
                 'needs_code': True, 'needs_animation': False, 'needs_quiz': False},
                {'layout': 'quiz_check', 'theme': 'academic_light', 'title': '随堂检测',
                 'teaching_goal': '检验对学习率的理解', 'chapter_id': '', 'content_brief': '检验对梯度下降的理解',
                 'needs_code': False, 'needs_animation': False, 'needs_quiz': True},
            ]
        }, ensure_ascii=False)

        self.manager.client.generate_text = lambda prompt, max_tokens=1024: skeleton_json

        skeleton = self.manager._generate_deck_skeleton({'blueprint': {}}, {})

        self.assertEqual(len(skeleton), 3)
        self.assertEqual(skeleton[0]['layout'], 'cover')
        self.assertFalse(skeleton[0]['needs_code'])
        self.assertTrue(skeleton[1]['needs_code'])
        self.assertTrue(skeleton[2]['needs_quiz'])

    def test_generate_slide_contents_fills_code_and_quiz_blocks(self):
        skeleton = [
            {'layout': 'cover', 'theme': 'academic_light', 'title': '梯度下降导论',
             'teaching_goal': '', 'chapter_id': '', 'content_brief': '封面',
             'needs_code': False, 'needs_animation': False, 'needs_quiz': False},
            {'layout': 'two_column', 'theme': 'tech_blue', 'title': '用代码实现梯度下降',
             'teaching_goal': '', 'chapter_id': '', 'content_brief': '展示梯度下降的Python实现',
             'needs_code': True, 'needs_animation': False, 'needs_quiz': False},
            {'layout': 'quiz_check', 'theme': 'academic_light', 'title': '随堂检测',
             'teaching_goal': '', 'chapter_id': '', 'content_brief': '检验对梯度下降的理解',
             'needs_code': False, 'needs_animation': False, 'needs_quiz': True},
        ]

        generic_content = json.dumps({
            'bullets': ['梯度下降是一种优化算法', '通过迭代更新参数最小化损失函数'],
            'visual_blocks': [{'kind': 'bullet_card', 'label': '本节概览', 'items': ['损失函数', '负梯度', '学习率']}],
            'speaker_notes': '今天我们来学习梯度下降的基本思想。',
            'teacher_action': '讲解梯度下降的核心思想',
            'student_interaction': '思考损失函数下降的方向',
        }, ensure_ascii=False)

        code_content = json.dumps({
            'bullets': ['用Python实现一次梯度下降更新'],
            'visual_blocks': [{'kind': 'code', 'label': '梯度下降更新代码', 'language': 'python', 'code': 'w = w - lr * grad'}],
            'speaker_notes': '我们来看一段代码，演示参数如何更新。',
            'teacher_action': '演示代码运行结果',
            'student_interaction': '修改学习率观察收敛速度',
        }, ensure_ascii=False)

        quiz_content = json.dumps({
            'bullets': ['检验对学习率作用的理解'],
            'visual_blocks': [{
                'kind': 'question',
                'question_text': '学习率过大时，损失函数曲线通常会出现什么现象？',
                'question_type': 'choice',
                'choices': [
                    {'label': 'A. 平滑下降', 'value': 'A'},
                    {'label': 'B. 震荡甚至发散', 'value': 'B'},
                    {'label': 'C. 立刻收敛到最优', 'value': 'C'},
                    {'label': 'D. 不会有任何变化', 'value': 'D'},
                ],
                'correct_answer': 'B',
                'explanation': '过大的学习率会导致参数更新步幅过大，损失函数震荡甚至发散。',
            }],
            'speaker_notes': '请大家思考并选择正确答案。',
            'teacher_action': '组织学生讨论答案',
            'student_interaction': '选择答案并解释理由',
        }, ensure_ascii=False)

        def fake_generate_text(prompt, max_tokens=1024):
            if '"kind":"code"' in prompt:
                return code_content
            if '"kind":"question"' in prompt:
                return quiz_content
            return generic_content

        self.manager.client.generate_text = fake_generate_text

        slides = self.manager._generate_slide_contents(skeleton, {'blueprint': {}}, {}, '')

        self.assertEqual(len(slides), 3)

        code_slide = next(s for s in slides if s['needs_code'])
        quiz_slide = next(s for s in slides if s['needs_quiz'])

        self.assertTrue(any(b.get('kind') == 'code' for b in code_slide['visual_blocks']))
        self.assertTrue(any(b.get('kind') == 'question' for b in quiz_slide['visual_blocks']))
        self.assertTrue(quiz_slide['speaker_notes'])


class ProfileUpdateSummaryTests(SimpleTestCase):
    """对话抽到画像信号 → 前端"画像已更新"提示摘要。"""

    def test_summary_lists_updated_dimensions(self):
        from agent_system.views import _profile_update_summary
        self.assertIsNone(_profile_update_summary(None))
        self.assertIsNone(_profile_update_summary({}))
        out = _profile_update_summary({'cognitive_style': '视觉型', 'learning_goals': ['考研'], 'nope': 1})
        self.assertTrue(out['updated'])
        self.assertIn('认知风格', out['dimensions'])
        self.assertIn('学习目标', out['dimensions'])
        self.assertNotIn('nope', out['dimensions'])


class EmbeddingSemanticTests(SimpleTestCase):
    """词袋哈希向量：共享词多的文本更相似（不再是 sha256 噪声）。"""

    def test_shared_tokens_give_higher_similarity(self):
        from agent_system.services.embeddings import compute_embedding, cosine_similarity
        q = compute_embedding('梯度下降的学习率如何影响收敛')
        related = compute_embedding('学习率过大会导致梯度下降不收敛')
        unrelated = compute_embedding('唐诗宋词的意象与格律赏析')
        self.assertGreater(cosine_similarity(q, related), cosine_similarity(q, unrelated))

    def test_empty_text_returns_zero_vector(self):
        from agent_system.services.embeddings import compute_embedding
        self.assertEqual(set(compute_embedding('')), {0.0})

    def test_identical_text_similarity_is_one(self):
        from agent_system.services.embeddings import compute_embedding, cosine_similarity
        v = compute_embedding('线性代数中的特征值与特征向量')
        self.assertAlmostEqual(cosine_similarity(v, v), 1.0, places=5)


class XinghuoStreamParseTests(SimpleTestCase):
    """流式：把 OpenAI 兼容 SSE 行解析成纯文本增量（此前直接吐 JSON，前端没法用）。"""

    def test_parse_stream_line_extracts_delta_content(self):
        from agent_system.services.xinghuo_client import XinghuoClient
        p = XinghuoClient._parse_stream_line
        self.assertEqual(p('data: {"choices":[{"delta":{"content":"你好"}}]}'), '你好')
        self.assertEqual(p('{"choices":[{"delta":{"content":"世界"}}]}'), '世界')
        # message.content / text 兼容
        self.assertEqual(p('data: {"choices":[{"message":{"content":"整段"}}]}'), '整段')

    def test_parse_stream_line_skips_noise(self):
        from agent_system.services.xinghuo_client import XinghuoClient
        p = XinghuoClient._parse_stream_line
        for noise in ['', '   ', 'data: [DONE]', 'data: ', ': keep-alive',
                      'data: {"choices":[{"delta":{}}]}', 'not json']:
            self.assertIsNone(p(noise), noise)


class MultiResourceGenerationTests(TestCase):
    """赛题要求 ≥5 种真实资源：mindmap/reading/code 应真·LLM 生成，失败明确反馈。"""

    def setUp(self):
        self.user = User.objects.create_user(username='multi_res', email='mr@example.com', password='p12345678')
        self.gm = GenerationManager(self.user, 'Python装饰器')
        self.gm._safe_text = lambda t: (t, {'safe': True})

    def test_mindmap_generates_real_content(self):
        self.gm.client.generate_text = lambda p, max_tokens=1024: '- 装饰器\n  - 概念\n  - 应用'
        res = self.gm._generate_mindmap({'blueprint': {}})
        self.assertNotEqual(res.get('status'), 'failed')
        self.assertIn('装饰器', res.get('content', ''))
        self.assertEqual(res['metadata']['kind'], 'mindmap')
        self.assertTrue(any(c['agent'] == 'MindMapAgent' for c in self.gm.collaboration_log))

    def test_reading_generates_real_content(self):
        self.gm.client.generate_text = lambda p, max_tokens=1024: '1. 流畅的Python - Luciano - 讲装饰器 - 进阶'
        res = self.gm._generate_reading({'blueprint': {}})
        self.assertNotEqual(res.get('status'), 'failed')
        self.assertTrue(res.get('content'))
        self.assertEqual(res['metadata']['kind'], 'reading')

    def test_code_uses_llm_and_extracts_block(self):
        self.gm.client.generate_text = lambda p, max_tokens=1024: '```python\ndef w(f):\n    return f\n```\n演示装饰器。'
        res = self.gm._generate_code({'blueprint': {}})
        self.assertEqual(res.get('language'), 'python')
        self.assertIn('def w', res.get('code', ''))
        self.assertEqual(res['metadata']['source'], 'llm')

    def test_code_falls_back_to_template_then_fails_loudly(self):
        # LLM 失败 + 命中硬编码主题 → 模板兜底（真实代码）
        gm2 = GenerationManager(self.user, 'Python基础'); gm2._safe_text = lambda t: (t, {'safe': True})
        gm2.client.generate_text = lambda p, max_tokens=1024: '[占位] AI内容生成失败'
        r1 = gm2._generate_code({'blueprint': {}})
        self.assertEqual(r1['metadata'].get('source'), 'template')
        self.assertTrue(r1.get('code'))
        # LLM 失败 + 无硬编码主题 → 明确失败，不编造
        gm3 = GenerationManager(self.user, '中国近代史'); gm3._safe_text = lambda t: (t, {'safe': True})
        gm3.client.generate_text = lambda p, max_tokens=1024: '[占位] AI内容生成失败'
        r2 = gm3._generate_code({'blueprint': {}})
        self.assertEqual(r2.get('status'), 'failed')
        self.assertEqual(r2.get('code'), '')

    def test_mindmap_reading_fail_loudly_when_api_down(self):
        self.gm.client.generate_text = lambda p, max_tokens=1024: '[占位] AI内容生成失败'
        for res in (self.gm._generate_mindmap({'blueprint': {}}), self.gm._generate_reading({'blueprint': {}})):
            self.assertEqual(res.get('status'), 'failed')
            self.assertEqual(res.get('content'), '')
            self.assertIn('失败', res.get('error', ''))

    def test_doc_carries_fact_check_and_safety_flag(self):
        # 讲义生成后带上事实审校结果与内容安全标记（评委可见证据）；审校无错时不触发纠正
        def fake(p, max_tokens=1024):
            if '待审内容' in p:  # _llm_fact_review 的 prompt 特征
                return '{"has_errors": false, "severity": "low", "errors": []}'
            return '这是一份关于Python装饰器的讲义，讲清概念与例子。'
        self.gm.client.generate_text = fake
        res = self.gm._generate_doc_with_standards({'blueprint': {}}, {}, '')
        self.assertTrue(res['fact_check']['reviewed'])
        self.assertTrue(res['fact_check']['reliable'])
        self.assertFalse(res['fact_check']['corrected'])
        self.assertTrue(res['metadata']['content_safety_checked'])
        self.assertTrue(res['metadata']['fact_reviewed'])

    def test_doc_intercepts_and_corrects_factual_errors(self):
        # 防幻觉真拦截：审校发现高危错误 → 带纠正清单重生成 → 复审通过 → 采纳纠正稿
        state = {'reviews': 0}
        def fake(p, max_tokens=1024):
            if '待审内容' in p:  # 审校调用
                state['reviews'] += 1
                if state['reviews'] == 1:
                    return '{"has_errors": true, "severity": "high", "errors": ["把二叉搜索树说成自平衡，应为普通BST不自平衡"]}'
                return '{"has_errors": false, "severity": "low", "errors": []}'
            return '纠正后的讲义正文。' if '逐一纠正' in p else '原始讲义正文（含错误）。'
        self.gm.client.generate_text = fake
        res = self.gm._generate_doc_with_standards({'blueprint': {}}, {}, '')
        self.assertTrue(res['fact_check']['corrected'])
        self.assertTrue(res['fact_check']['reliable'])
        self.assertIn('纠正后', res['content'])

    def test_fact_check_handles_empty_and_failure_gracefully(self):
        self.assertIsNone(self.gm._fact_check(''))  # 空内容不校验
        with patch('agent_system.services.safety.verify_factuality', side_effect=Exception('boom')):
            self.assertIsNone(self.gm._fact_check('一些内容'))  # 校验异常不阻断

    def test_generate_drafts_dispatches_all_six_types(self):
        self.gm.client.generate_text = lambda p, max_tokens=1024: '真实内容 def x(): pass ```python\nx=1\n```'
        self.gm.resource_types = ['doc', 'quiz', 'code', 'mindmap', 'reading']
        # ppt 较重，这里只验证分派存在（doc/quiz/code/mindmap/reading 都产出结果）
        self.gm._generate_drafts({'blueprint': {'chapters': []}}, {'learning_objectives': []})
        for rt in ('doc', 'quiz', 'code', 'mindmap', 'reading'):
            self.assertIn(rt, self.gm.results, f'{rt} 未被生成')


class SkeletonFirstPptTests(TestCase):
    """验证骨架法转正后的整条 PPT 管线：占位哨兵、叙事注入、跨页去重、出题拆分、动画独立成页。"""

    def setUp(self):
        self.user = User.objects.create_user(username='ppt_flow_user', email='ppt_flow@example.com', password='test1234')
        self.manager = GenerationManager(self.user, '梯度下降')
        self.manager._safe_text = lambda text: (text, {'safe': True, 'labels': []})
        self.outline_data = {'blueprint': {'chapters': []}}

    def _skeleton_json(self):
        return json.dumps({'slides': [
            {'layout': 'cover', 'theme': 'academic_light', 'title': '梯度下降导论', 'teaching_goal': '了解全局',
             'core_explanation': '本课讲解梯度下降的核心思想与实现方法，共分若干节。', 'concrete_example': '下山找最低点',
             'key_question': '为何要迭代', 'one_line': '梯度下降是什么', 'narrative_role': 'motivation',
             'needs_code': False, 'needs_animation': False, 'needs_quiz': False},
            {'layout': 'two_column', 'theme': 'tech_blue', 'title': '核心机制', 'teaching_goal': '能说出原理',
             'core_explanation': '沿负梯度方向更新参数，逐步逼近损失最小值。', 'concrete_example': 'w=w-lr*grad',
             'key_question': '负梯度为何是下降方向', 'one_line': '沿负梯度更新参数', 'narrative_role': 'mechanism',
             'needs_code': False, 'needs_animation': False, 'needs_quiz': False},
            {'layout': 'case_study', 'theme': 'tech_blue', 'title': '实际应用', 'teaching_goal': '能举例',
             'core_explanation': '在神经网络训练中广泛使用小批量梯度下降。', 'concrete_example': '训练分类器',
             'key_question': '为何用小批量', 'one_line': '训练中的应用', 'narrative_role': 'example',
             'needs_code': False, 'needs_animation': False, 'needs_quiz': False},
            {'layout': 'quiz_check', 'theme': 'academic_light', 'title': '随堂检测', 'teaching_goal': '检验理解',
             'core_explanation': '检验对学习率与收敛的理解。', 'concrete_example': '', 'key_question': '',
             'one_line': '随堂自测', 'narrative_role': 'consolidation',
             'needs_code': False, 'needs_animation': False, 'needs_quiz': True},
        ]}, ensure_ascii=False)

    def _slide_content(self, prompt):
        # 让"应用"页第一条要点与"核心机制"页重复，以验证跨页去重。
        # 注意：叙事注入会把全课大纲放进每页 prompt，因此必须按"当前页标题"标记匹配，
        # 即 build_slide_content_prompt 里的 标题："XXX"，而不是宽松地搜标题子串。
        dup_bullet = '沿负梯度方向更新参数逐步逼近损失函数的最小值这是核心思想'
        if '标题："随堂检测"' in prompt:
            q = lambda i: {
                'kind': 'question', 'question_text': f'第{i}题：学习率过大时会怎样？', 'question_type': 'choice',
                'choices': [{'label': 'A. 平滑下降', 'value': 'A'}, {'label': 'B. 震荡发散', 'value': 'B'}],
                'correct_answer': 'B', 'explanation': '步幅过大导致震荡甚至发散。',
            }
            return json.dumps({'bullets': ['检验对学习率的理解'], 'visual_blocks': [q(1), q(2)],
                               'speaker_notes': '请独立作答后再看解析，重点理解原因所在。' * 3,
                               'teacher_action': '组织讨论', 'student_interaction': '独立作答'}, ensure_ascii=False)
        if '标题："核心机制"' in prompt:
            return json.dumps({'bullets': [dup_bullet, '学习率控制每步更新的幅度大小与收敛速度'],
                               'visual_blocks': [{'kind': 'concept_node', 'label': '负梯度', 'text': '损失下降最快的方向就是负梯度方向'}],
                               'speaker_notes': '我们先理解负梯度为什么是下降最快的方向。' * 3,
                               'teacher_action': '板书推导', 'student_interaction': '跟随推导'}, ensure_ascii=False)
        if '标题："实际应用"' in prompt:
            return json.dumps({'bullets': [dup_bullet, '实际训练中常用小批量梯度下降以平衡效率与稳定'],
                               'visual_blocks': [{'kind': 'bullet_card', 'label': '应用', 'text': '在图像分类等任务中广泛应用梯度下降优化'}],
                               'speaker_notes': '接下来看它在真实训练里怎么用。' * 3,
                               'teacher_action': '举例', 'student_interaction': '思考'}, ensure_ascii=False)
        return json.dumps({'bullets': ['理解本页核心内容并能复述给同学听'],
                           'visual_blocks': [{'kind': 'bullet_card', 'label': '导入', 'text': '带着问题进入本节课的学习之旅'}],
                           'speaker_notes': '本节课我们从一个问题开始。' * 3,
                           'teacher_action': '提问', 'student_interaction': '思考'}, ensure_ascii=False)

    def _fake_generate(self):
        skeleton_json = self._skeleton_json()

        def fake(prompt, max_tokens=1024):
            if '教学PPT蓝图' in prompt:
                return skeleton_json
            if '生成完整的教学内容' in prompt:
                return self._slide_content(prompt)
            if '教学总编' in prompt:
                return json.dumps({'revisions': []}, ensure_ascii=False)
            return ''  # 动画等：返回空
        return fake

    def test_placeholder_sentinel_prevents_leak_when_api_down(self):
        # 模拟星火 API 全程不可用，只返回占位文本
        self.manager.client.generate_text = lambda prompt, max_tokens=1024: '[占位讲解] 星火接口当前不可用'
        result = self.manager._generate_ppt_with_standards(self.outline_data, {}, '')
        slides = result['slides']
        self.assertGreaterEqual(len(slides), 3)
        blob = json.dumps(slides, ensure_ascii=False)
        self.assertNotIn('[占位', blob)  # 占位文本绝不泄漏进最终 PPT
        for s in slides:
            for b in (s.get('bullets') or []):
                self.assertFalse(str(b).startswith('[占位'))

    def test_doc_generation_fails_loudly_when_api_down(self):
        # 接口不可用时讲义不写占位内容，而是明确失败
        self.manager.client.generate_text = lambda prompt, max_tokens=1024: '[占位] AI内容生成失败：接口暂时不可用，请稍后重试。'
        res = self.manager._generate_doc_with_standards(self.outline_data, {}, '')
        self.assertEqual(res.get('status'), 'failed')
        self.assertEqual(res.get('content'), '')
        self.assertIn('失败', res.get('error', ''))
        self.assertTrue(self.manager._llm_unavailable())

    def test_quiz_generation_returns_no_fake_questions_when_api_down(self):
        # 接口不可用时练习题不编造题目，而是明确失败
        self.manager.client.generate_text = lambda prompt, max_tokens=1024: '[占位] AI内容生成失败：接口暂时不可用，请稍后重试。'
        res = self.manager._generate_quiz_with_standards(self.outline_data, {})
        self.assertEqual(res.get('status'), 'failed')
        self.assertEqual(res.get('questions'), [])
        self.assertIn('失败', res.get('error', ''))

    def test_llm_unavailable_only_when_all_calls_failed(self):
        # 有一次成功就不算“全线失败”
        self.manager._note_llm('[占位] x')
        self.assertTrue(self.manager._llm_unavailable())
        self.manager._note_llm('这是正常返回的真实内容')
        self.assertFalse(self.manager._llm_unavailable())

    def test_full_pipeline_splits_quiz_dedupes_and_injects_narrative(self):
        self.manager.client.generate_text = self._fake_generate()
        result = self.manager._generate_ppt_with_standards(self.outline_data, {}, '')
        slides = result['slides']

        # 不变量1：至少3页
        self.assertGreaterEqual(len(slides), 3)
        # 不变量2：无占位泄漏
        self.assertNotIn('[占位', json.dumps(slides, ensure_ascii=False))
        # 不变量3：出题拆分——每个含题的页恰好只有1道题
        quiz_pages = [s for s in slides if any(b.get('kind') == 'question' for b in (s.get('visual_blocks') or []))]
        self.assertEqual(len(quiz_pages), 2)  # 原本1页2题 → 拆成2页
        for qp in quiz_pages:
            self.assertEqual(sum(1 for b in qp['visual_blocks'] if b.get('kind') == 'question'), 1)
        # 不变量4：跨页去重——重复要点在整册里只出现一次
        dup = '沿负梯度方向更新参数逐步逼近损失函数的最小值这是核心思想'
        occurrences = sum(1 for s in slides for b in (s.get('bullets') or []) if b == dup)
        self.assertEqual(occurrences, 1)
        # 不变量5：无重复标题（拆分后的题目页带"第N题"后缀因此不同）
        titles = [s.get('title') for s in slides]
        self.assertEqual(len(titles), len(set(titles)))

    def test_split_quiz_keeps_only_choice_questions(self):
        from .generation import _split_quiz_slides
        choice_q = {'kind': 'question', 'question_text': '选择题?', 'choices': ['A', 'B', 'C'], 'correct_answer': 'A'}
        short_q = {'kind': 'question', 'question_text': '简答题?', 'question_type': 'short_answer'}  # 无 choices
        slides = [{'layout': 'quiz_check', 'title': '随堂检测', 'bullets': [], 'visual_blocks': [choice_q, short_q]}]
        out = _split_quiz_slides('主题', slides)
        # 只保留 1 道选择题，简答题被丢弃
        qs = [b for s in out for b in (s.get('visual_blocks') or []) if b.get('kind') == 'question']
        self.assertEqual(len(qs), 1)
        self.assertTrue(all(isinstance(q.get('choices'), list) and q.get('choices') for q in qs))

    def test_split_quiz_drops_page_with_only_non_choice_question(self):
        from .generation import _split_quiz_slides
        short_q = {'kind': 'question', 'question_text': '请简述?', 'question_type': 'short_answer'}
        slides = [{'layout': 'quiz_check', 'title': '自测', 'bullets': [], 'visual_blocks': [short_q]}]
        out = _split_quiz_slides('主题', slides)
        # 整页只有非选择题 → 该页被丢弃（不渲染残缺题目）
        self.assertEqual(len(out), 0)

    def test_narrative_context_injected_into_slide_prompt(self):
        from .generation import build_slide_content_prompt, _build_deck_narrative
        skeleton = [
            {'title': '第一页', 'one_line': '讲第一页', 'layout': 'cover'},
            {'title': '第二页', 'one_line': '讲第二页', 'layout': 'two_column'},
        ]
        _build_deck_narrative(skeleton)
        prompt = build_slide_content_prompt('梯度下降', skeleton[0], {}, {}, '',
                                            narrative_context=skeleton[0]['narrative_context'])
        self.assertIn('全局叙事上下文', prompt)
        self.assertIn('第二页', prompt)  # 下一页信息被注入


class GenerationCollaborationLogTests(TestCase):
    """验证Agent协作过程（CriticAgent审核/ReflectionController修订/
    StudentSimulatorAgent模拟与个性化改写）会被记录到collaboration_log，
    供课程页面可视化展示。"""

    def setUp(self):
        self.user = User.objects.create_user(username='collab_user', email='collab@example.com', password='test1234')
        self.manager = GenerationManager(self.user, '梯度下降')

    def test_review_and_improve_logs_critic_and_reflection_steps(self):
        self.manager.results = {'doc': {'content': '# 梯度下降\n梯度下降是一种优化算法...'}}
        self.manager.reflection_controller.iterative_improvement = Mock(return_value={
            'final_content': '# 修订后的梯度下降讲义',
            'final_score': 90,
            'iterations': [
                {'iteration': 1, 'score': 60, 'needs_revision': True, 'feedback': ['例子太少']},
                {'iteration': 2, 'score': 90, 'needs_revision': False, 'feedback': []},
            ],
            'total_iterations': 2,
            'quality_met': True,
        })

        self.manager._review_and_improve()

        log = self.manager.collaboration_log
        review_entries = [e for e in log if e['agent'] == 'CriticAgent' and e['stage'] == 'review']
        revision_entries = [e for e in log if e['agent'] == 'ReflectionController' and e['stage'] == 'revision']

        self.assertEqual(len(review_entries), 2)
        self.assertEqual(len(revision_entries), 1)
        self.assertTrue(all(e['resource_type'] == 'doc' for e in review_entries + revision_entries))
        self.assertEqual(review_entries[0]['score'], 60)
        self.assertTrue(review_entries[0]['needs_revision'])
        self.assertEqual(review_entries[0]['feedback'], ['例子太少'])
        self.assertEqual(revision_entries[0]['iteration'], 1)
        self.assertEqual(review_entries[1]['score'], 90)
        self.assertFalse(review_entries[1]['needs_revision'])

    def test_personalize_for_student_logs_simulation_and_personalize(self):
        self.manager.results = {'doc': {'content': '# 梯度下降\n梯度下降是一种优化算法...'}}
        self.manager.user_profile = {'cognitive_style': '视觉型'}

        simulation_report = json.dumps({
            'persona_summary': '一名编程基础薄弱的学生',
            'overall_fit_score': 70,
            'goal_alignment': 70,
            'comprehension_issues': ['公式推导过快'],
            'misconception_triggers': [],
            'engagement_issues': [],
            'suggestions': ['增加图示'],
        }, ensure_ascii=False)
        revised_text = '# 个性化改写后的讲义\n更适合该学生的版本...'

        self.manager.client.generate_text = Mock(side_effect=[simulation_report, revised_text])

        self.manager._personalize_for_student()

        log = self.manager.collaboration_log
        sim_entries = [e for e in log if e['agent'] == 'StudentSimulatorAgent' and e['stage'] == 'simulation']
        personalize_entries = [e for e in log if e['agent'] == 'StudentSimulatorAgent' and e['stage'] == 'personalize']

        self.assertEqual(len(sim_entries), 1)
        self.assertEqual(sim_entries[0]['resource_type'], 'doc')
        self.assertEqual(sim_entries[0]['overall_fit_score'], 70)
        self.assertEqual(sim_entries[0]['suggestions'], ['增加图示'])

        self.assertEqual(len(personalize_entries), 1)
        self.assertEqual(personalize_entries[0]['resource_type'], 'doc')
        self.assertEqual(self.manager.results['doc']['final_content'], revised_text)
        self.assertTrue(self.manager.results['doc']['personalized'])

    def test_debate_review_reconsiders_on_disagreement(self):
        """两位审核员评分/是否需修改的分歧达到阈值时，应各自重新评估一次
        （多智能体辩论，Du et al. arXiv:2305.14325）。"""
        controller = self.manager.reflection_controller

        responses = [
            json.dumps({'score': 60, 'needs_revision': True, 'criteria': {'事实准确性': 60},
                        'feedback': ['公式有误'], 'suggestions': []}, ensure_ascii=False),
            json.dumps({'score': 85, 'needs_revision': False, 'criteria': {'完整性': 85},
                        'feedback': [], 'suggestions': ['增加例子']}, ensure_ascii=False),
            json.dumps({'score': 70, 'needs_revision': True, 'criteria': {'事实准确性': 70},
                        'feedback': ['公式有误，但影响较小'], 'suggestions': []}, ensure_ascii=False),
            json.dumps({'score': 80, 'needs_revision': False, 'criteria': {'完整性': 85},
                        'feedback': [], 'suggestions': ['增加例子']}, ensure_ascii=False),
        ]
        controller.critic.client.generate_text = Mock(side_effect=responses)

        result = controller.debate_review('梯度下降', '# 梯度下降\n梯度下降是一种优化算法...', 'doc', {}, 'college')

        self.assertEqual(
            [r['critic'] for r in result['debate_rounds']],
            ['CriticAgent', 'DebateCriticAgent', 'CriticAgent', 'DebateCriticAgent'],
        )
        self.assertEqual(result['debate_rounds'][0]['score'], 60)
        self.assertEqual(result['debate_rounds'][2]['score'], 70)
        self.assertEqual(result['debate_rounds'][3]['score'], 80)
        self.assertEqual(result['score'], 75)
        self.assertTrue(result['needs_revision'])

    def test_debate_review_skips_reconsideration_when_reviewers_agree(self):
        controller = self.manager.reflection_controller

        responses = [
            json.dumps({'score': 88, 'needs_revision': False, 'criteria': {'事实准确性': 88},
                        'feedback': [], 'suggestions': []}, ensure_ascii=False),
            json.dumps({'score': 90, 'needs_revision': False, 'criteria': {'完整性': 90},
                        'feedback': [], 'suggestions': []}, ensure_ascii=False),
        ]
        controller.critic.client.generate_text = Mock(side_effect=responses)

        result = controller.debate_review('梯度下降', '# 梯度下降\n梯度下降是一种优化算法...', 'doc', {}, 'college')

        self.assertEqual(len(result['debate_rounds']), 2)
        self.assertEqual(result['score'], 89)
        self.assertFalse(result['needs_revision'])

    def test_review_and_improve_logs_debate_rounds_for_first_iteration(self):
        self.manager.results = {'doc': {'content': '# 梯度下降\n梯度下降是一种优化算法...'}}
        debate_rounds = [
            {'critic': 'CriticAgent', 'score': 60, 'needs_revision': True, 'feedback': ['公式有误']},
            {'critic': 'DebateCriticAgent', 'score': 85, 'needs_revision': False, 'feedback': []},
        ]
        self.manager.reflection_controller.iterative_improvement = Mock(return_value={
            'final_content': '# 梯度下降\n梯度下降是一种优化算法...',
            'final_score': 73,
            'iterations': [
                {'iteration': 1, 'score': 73, 'needs_revision': False, 'feedback': [], 'debate_rounds': debate_rounds},
            ],
            'total_iterations': 1,
            'quality_met': False,
        })

        self.manager._review_and_improve()

        log = self.manager.collaboration_log
        debate_entries = [e for e in log if e['agent'] == 'CriticAgent' and e['stage'] == 'debate']
        review_entries = [e for e in log if e['agent'] == 'CriticAgent' and e['stage'] == 'review']

        self.assertEqual(len(debate_entries), 1)
        self.assertEqual(len(review_entries), 0)
        self.assertEqual(debate_entries[0]['resource_type'], 'doc')
        self.assertEqual(debate_entries[0]['score'], 73)
        self.assertEqual(debate_entries[0]['rounds'], debate_rounds)


class ConversationSendApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='api_student', email='api_student@example.com', password='test1234')
        self.client.force_login(self.user)

        self.course = Course.objects.create(
            owner=self.user,
            title='梯度下降导论',
            status='published',
            visibility='login',
        )
        self.material = CourseMaterial.objects.create(
            course=self.course,
            uploaded_by=self.user,
            title='梯度方向与学习率',
            material_type='pdf',
            file=SimpleUploadedFile('grad.pdf', b'fake-pdf-content', content_type='application/pdf'),
            processing_status='ready',
            display_order=1,
        )
        MaterialChunk.objects.create(
            material=self.material,
            chunk_index=0,
            source_page='7',
            heading='梯度方向与学习率',
            keyword_summary='梯度方向, 学习率',
            content='梯度下降中，参数应沿负梯度方向更新，学习率控制每次更新步长。',
        )
        LearningPlan.objects.create(
            user=self.user,
            title='梯度下降学习路线（已按当前状态调整）',
            plan_data=json.dumps({
                'title': '梯度下降学习路线（已按当前状态调整）',
                'matched_course': {'id': self.course.id, 'title': self.course.title},
                'weak_areas': ['梯度方向'],
                'recommendation_reason': ['检测到当前薄弱点：梯度方向，已将补弱阶段提前。'],
                'modules': [
                    {
                        'name': '阶段0：先补当前薄弱点',
                        'focus': '先把梯度方向补齐。',
                        'lessons': [
                            {'title': '回看梯度方向', 'objectives': '厘清梯度和负梯度关系'},
                        ],
                    }
                ],
            }, ensure_ascii=False),
            status='generated',
        )

    def test_api_conversation_send_rejects_private_course_material_context_for_other_user(self):
        other_user = User.objects.create_user(username='other_student', email='other_student@example.com', password='test1234')
        self.client.force_login(other_user)

        private_course = Course.objects.create(
            owner=self.user,
            title='私有课程',
            status='draft',
            visibility='private',
        )
        private_material = CourseMaterial.objects.create(
            course=private_course,
            uploaded_by=self.user,
            title='私有资料',
            material_type='pdf',
            file=SimpleUploadedFile('private.pdf', b'fake-pdf-content', content_type='application/pdf'),
            processing_status='ready',
        )
        MaterialChunk.objects.create(
            material=private_material,
            chunk_index=0,
            source_page='1',
            heading='私有内容',
            content='这是一份不应被其他用户看到的资料片段。',
        )

        response = self.client.post(
            reverse('agent_system:api_conversation_send'),
            data=json.dumps({
                'text': '请结合这份资料回答。',
                'course_id': private_course.id,
                'material_id': private_material.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get('ok'))
        self.assertEqual(payload['course_context']['course_id'], None)
        self.assertEqual(payload['course_context']['material_id'], None)
        self.assertFalse(payload['course_context']['references'])

    def test_api_conversation_send_returns_course_context_references_and_plan(self):
        ProfileBuilder._instance = None

        response = self.client.post(
            reverse('agent_system:api_conversation_send'),
            data=json.dumps({
                'text': '我总是把梯度方向弄反，帮我讲解一下。',
                'course_id': self.course.id,
                'material_id': self.material.id,
                'current_page': '7',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get('ok'))
        self.assertIn('course_context', payload)
        self.assertEqual(payload['course_context']['course_id'], self.course.id)
        self.assertEqual(payload['course_context']['material_id'], self.material.id)
        self.assertEqual(payload['course_context']['current_page'], '7')
        self.assertTrue(payload['course_context']['references'])
        self.assertEqual(payload['course_context']['references'][0]['source_page'], '7')
        self.assertEqual(payload['course_context']['learning_plan']['top_module_name'], '阶段0：先补当前薄弱点')

        assistant_message = Message.objects.filter(conversation_id=payload['conversation_id'], role='assistant').latest('id')
        message_context = assistant_message.metadata.get('course_context') or {}
        self.assertEqual(message_context.get('current_page'), '7')
        self.assertEqual(message_context.get('course_id'), self.course.id)
        self.assertEqual(message_context.get('material_id'), self.material.id)
        self.assertTrue(message_context.get('references'))
        self.assertEqual(message_context.get('learning_plan', {}).get('top_module_name'), '阶段0：先补当前薄弱点')

    def test_api_conversation_send_records_profile_event_and_updates_profile(self):
        from . import views as agent_views

        original_generate_text = agent_views.profile_builder.client.generate_text
        agent_views.profile_builder.client.generate_text = lambda prompt: '我会按考试目标和案例讲解方式帮你安排。'
        try:
            response = self.client.post(
                reverse('agent_system:api_conversation_send'),
                data=json.dumps({
                    'text': '我想通过考试，喜欢案例讲解，每周能学5小时，Python零基础。',
                }),
                content_type='application/json',
            )
        finally:
            agent_views.profile_builder.client.generate_text = original_generate_text

        self.assertEqual(response.status_code, 200)
        event = ProfileEvent.objects.filter(user=self.user, event_type='conversation_message').latest('id')
        self.assertIsNotNone(event.processed_at)
        self.assertIn('profile_delta', event.payload)

        profile = StudentProfile.objects.get(user=self.user)
        self.assertIn('考试', profile.learning_goals)
        self.assertEqual(profile.cognitive_style, '听觉型')
        self.assertEqual(profile.learning_preferences.get('preferred_mode'), '讲解')
        self.assertEqual(profile.engagement.get('weekly_hours'), 5)
        self.assertEqual(profile.knowledge_profile.get('Python'), '初级')

    def test_api_conversation_send_includes_and_updates_long_term_memory(self):
        from . import views as agent_views

        convo = Conversation.objects.create(
            user=self.user,
            title='长期记忆测试对话',
            context_summary='学生想先把梯度方向搞清楚。',
        )
        for index in range(10):
            Message.objects.create(
                conversation=convo,
                role='student' if index % 2 == 0 else 'assistant',
                content=f'历史消息{index}',
                content_type='text',
            )

        captured = {'prompt': ''}
        original_generate_text = agent_views.profile_builder.client.generate_text

        def fake_generate(prompt):
            captured['prompt'] = prompt
            return '建议先把负梯度与梯度方向对应关系记牢，再做一题自测。'

        agent_views.profile_builder.client.generate_text = fake_generate
        try:
            response = self.client.post(
                reverse('agent_system:api_conversation_send'),
                data=json.dumps({
                    'conversation_id': convo.id,
                    'text': '我还是会把梯度方向写反，下一步怎么练？',
                    'course_id': self.course.id,
                    'material_id': self.material.id,
                    'current_page': '7',
                }),
                content_type='application/json',
            )
        finally:
            agent_views.profile_builder.client.generate_text = original_generate_text

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get('ok'))
        self.assertIn('conversation_memory', payload)
        self.assertIn('学生近期诉求', payload.get('conversation_memory') or '')
        self.assertIn('长期对话记忆', captured['prompt'])
        self.assertIn('学生想先把梯度方向搞清楚', captured['prompt'])

        convo.refresh_from_db()
        self.assertIn('导师最近建议', convo.context_summary or '')

        assistant_message = Message.objects.filter(conversation=convo, role='assistant').latest('id')
        self.assertEqual(assistant_message.metadata.get('conversation_memory'), convo.context_summary)


class ConversationPeerTeachingTests(TestCase):
    def test_peer_teaching_prompt_uses_xiaoai_persona_and_skips_socratic_rules(self):
        prompt = _build_conversation_system_prompt(
            '请帮我讲讲梯度下降。',
            guidance_summary='',
            known_summary='',
            material_context={},
            tutor_mode='peer_teaching',
        )

        self.assertIn('小艾', prompt)
        self.assertIn('你主动请对方给你讲讲这部分内容', prompt)
        self.assertNotIn('苏格拉底', prompt)

    def setUp(self):
        self.user = User.objects.create_user(username='peer_student', email='peer_student@example.com', password='test1234')
        self.client.force_login(self.user)

    def _send_peer_message(self, text, conversation_id=None, **extra):
        payload = {'text': text, 'mode': 'peer_teaching'}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        payload.update(extra)
        response = self.client.post(
            reverse('agent_system:api_conversation_send'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_profile_evaluated_every_three_student_turns(self):
        from . import views as agent_views

        eval_result = {
            'understood_points': ['梯度方向与参数更新方向相反'],
            'gap_points': [],
            'misconceptions_detected': ['认为学习率越大收敛越快'],
            'cognitive_style_observation': '倾向于用类比解释抽象概念',
            'teaching_score': 80,
        }

        original_generate_text = agent_views.profile_builder.client.generate_text
        agent_views.profile_builder.client.generate_text = lambda prompt: '原来是这样！那学习率太大会怎么样呢？'
        try:
            with patch('agent_system.agents.PeerLearnerAgent.evaluate_session', return_value=eval_result) as mock_eval:
                payload1 = self._send_peer_message('梯度下降就是沿着负梯度方向更新参数，每次更新的步长由学习率决定。')
                conversation_id = payload1['conversation_id']
                mock_eval.assert_not_called()
                profile = StudentProfile.objects.get(user=self.user)
                self.assertEqual(profile.cognitive_style, '')

                self._send_peer_message('如果学习率太小，模型每次只挪动一点点，收敛会很慢。', conversation_id)
                mock_eval.assert_not_called()
                profile.refresh_from_db()
                self.assertEqual(profile.cognitive_style, '')

                self._send_peer_message('反过来学习率太大，参数更新幅度过大，可能会在最优点附近来回震荡甚至发散。', conversation_id)
                mock_eval.assert_called_once()
        finally:
            agent_views.profile_builder.client.generate_text = original_generate_text

        profile.refresh_from_db()
        self.assertEqual(profile.cognitive_style, '倾向于用类比解释抽象概念')
        self.assertIn('认为学习率越大收敛越快', profile.misconceptions)

        event = ProfileEvent.objects.filter(user=self.user, event_type='peer_teaching_evaluation').latest('id')
        self.assertIsNotNone(event.processed_at)
        self.assertEqual(event.payload.get('profile_delta', {}).get('cognitive_style'), '倾向于用类比解释抽象概念')

    def test_peer_memories_are_injected_into_prompt(self):
        """费曼互教模式下，记忆流中相关的历史观察应被检索并注入prompt
        （生成式智能体记忆流，Park et al. 2023 UIST）。"""
        from . import views as agent_views

        profile, _ = StudentProfile.objects.get_or_create(user=self.user)
        now_iso = timezone.now().isoformat()
        profile.peer_memory_stream = [{
            'id': 'mem-1',
            'type': 'observation',
            'content': '这名同学上次提到容易把学习率和动量搞混。',
            'importance': 9,
            'topic': '梯度下降学习率',
            'created_at': now_iso,
            'last_accessed_at': now_iso,
        }]
        profile.save(update_fields=['peer_memory_stream'])

        captured = {}

        def fake_generate(prompt, **kwargs):
            captured['prompt'] = prompt
            return '原来是这样！那学习率太大会怎么样呢？'

        original_generate_text = agent_views.profile_builder.client.generate_text
        agent_views.profile_builder.client.generate_text = fake_generate
        try:
            self._send_peer_message('梯度下降学习率应该怎么设置？')
        finally:
            agent_views.profile_builder.client.generate_text = original_generate_text

        self.assertIn('【你的记忆】', captured['prompt'])
        self.assertIn('容易把学习率和动量搞混', captured['prompt'])

    def test_recent_topics_field_is_used_as_topic_when_no_course_context(self):
        """没有课程/资料上下文时，前端传来的"最近学了什么"应作为话题
        注入prompt，让"小艾"能问到具体内容。"""
        from . import views as agent_views

        captured = {}

        def fake_generate(prompt, **kwargs):
            captured['prompt'] = prompt
            return '原来是这样！那你能再讲讲反向传播是怎么算梯度的吗？'

        original_generate_text = agent_views.profile_builder.client.generate_text
        agent_views.profile_builder.client.generate_text = fake_generate
        try:
            self._send_peer_message('你好呀', recent_topics='梯度下降、反向传播')
        finally:
            agent_views.profile_builder.client.generate_text = original_generate_text

        self.assertIn('梯度下降、反向传播', captured['prompt'])

    def test_memory_observations_are_stored_after_third_turn(self):
        """每3轮学生发言评估后，值得记住的观察应写入
        StudentProfile.peer_memory_stream（生成式智能体记忆流）。"""
        from . import views as agent_views

        eval_result = {
            'understood_points': [],
            'gap_points': [],
            'misconceptions_detected': [],
            'cognitive_style_observation': '',
            'teaching_score': 75,
            'memory_observations': [
                {'content': '经常把学习率和动量混淆', 'importance': 8},
            ],
        }

        original_generate_text = agent_views.profile_builder.client.generate_text
        agent_views.profile_builder.client.generate_text = lambda prompt, **kwargs: '原来是这样！'
        try:
            with patch('agent_system.agents.PeerLearnerAgent.evaluate_session', return_value=eval_result):
                payload1 = self._send_peer_message('梯度下降第一轮讲解。')
                conversation_id = payload1['conversation_id']
                self._send_peer_message('梯度下降第二轮讲解。', conversation_id)
                self._send_peer_message('梯度下降第三轮讲解。', conversation_id)
        finally:
            agent_views.profile_builder.client.generate_text = original_generate_text

        profile = StudentProfile.objects.get(user=self.user)
        self.assertEqual(len(profile.peer_memory_stream), 1)
        observation = profile.peer_memory_stream[0]
        self.assertEqual(observation['content'], '经常把学习率和动量混淆')
        self.assertEqual(observation['importance'], 8)
        self.assertEqual(observation['type'], 'observation')
        self.assertTrue(observation['topic'])


class StudentSimulatorAgentTests(SimpleTestCase):
    def test_simulate_reading_parses_valid_json(self):
        agent = StudentSimulatorAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = json.dumps({
            'persona_summary': '一名编程基础薄弱、偏好图示的学生',
            'overall_fit_score': 65,
            'goal_alignment': 70,
            'comprehension_issues': ['指针概念讲解过快'],
            'misconception_triggers': ['认为数组和指针完全等价'],
            'engagement_issues': ['例子偏抽象'],
            'suggestions': ['增加图示辅助讲解'],
        })

        report = agent.simulate_reading('C语言指针', '指针是一种变量...', 'doc', {
            'cognitive_style': '视觉型',
            'misconceptions': ['认为数组和指针完全等价'],
        })

        self.assertEqual(report['overall_fit_score'], 65)
        self.assertEqual(report['goal_alignment'], 70)
        self.assertEqual(report['comprehension_issues'], ['指针概念讲解过快'])
        self.assertEqual(report['misconception_triggers'], ['认为数组和指针完全等价'])
        self.assertEqual(report['engagement_issues'], ['例子偏抽象'])
        self.assertEqual(report['suggestions'], ['增加图示辅助讲解'])
        self.assertEqual(report['persona_summary'], '一名编程基础薄弱、偏好图示的学生')

    def test_simulate_reading_falls_back_to_defaults_on_non_json(self):
        agent = StudentSimulatorAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = '这是一段不是JSON的自然语言回复。'

        report = agent.simulate_reading('C语言指针', '指针是一种变量...', 'doc', {})

        self.assertEqual(report['overall_fit_score'], 70)
        self.assertEqual(report['goal_alignment'], 70)
        self.assertEqual(report['persona_summary'], '')
        self.assertEqual(report['comprehension_issues'], [])
        self.assertEqual(report['misconception_triggers'], [])
        self.assertEqual(report['engagement_issues'], [])
        self.assertEqual(report['suggestions'], [])

    def test_personalize_revision_returns_rewritten_text(self):
        agent = StudentSimulatorAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = '# 改写后的讲义\n更适合该学生的版本...'

        revised = agent.personalize_revision('C语言指针', '# 原讲义\n指针是一种变量...', 'doc', {
            'comprehension_issues': ['指针概念讲解过快'],
            'suggestions': ['增加图示辅助讲解'],
        })

        self.assertEqual(revised, '# 改写后的讲义\n更适合该学生的版本...')
        agent.client.generate_text.assert_called_once()
        self.assertEqual(agent.client.generate_text.call_args.kwargs.get('max_tokens'), 3072)


class PeerLearnerAgentTests(SimpleTestCase):
    def test_respond_returns_client_text(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = '你能再讲讲学习率太大会有什么问题吗？'

        reply = agent.respond('梯度下降', ['Student: 梯度下降就是沿着负梯度方向更新参数。'], {
            'cognitive_style': '视觉型',
        })

        self.assertEqual(reply, '你能再讲讲学习率太大会有什么问题吗？')
        agent.client.generate_text.assert_called_once()

    def test_respond_includes_memory_text_when_memories_provided(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())
        captured = {}

        def fake_generate(prompt, max_tokens=512):
            captured['prompt'] = prompt
            return '你上次说学习率太大会震荡，这次还会犯这个错误吗？'

        agent.client.generate_text.side_effect = fake_generate

        reply = agent.respond('梯度下降', ['Student: ...'], {}, memories=[
            {'content': '这名同学经常把学习率和动量混淆', 'importance': 8},
        ])

        self.assertEqual(reply, '你上次说学习率太大会震荡，这次还会犯这个错误吗？')
        self.assertIn('【你的记忆】', captured['prompt'])
        self.assertIn('这名同学经常把学习率和动量混淆', captured['prompt'])

    def test_evaluate_session_parses_valid_json(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = json.dumps({
            'understood_points': ['梯度方向与参数更新方向相反'],
            'gap_points': ['没有提到学习率过大可能导致震荡'],
            'misconceptions_detected': ['认为学习率越大收敛越快'],
            'cognitive_style_observation': '倾向于用类比解释抽象概念',
            'teaching_score': 75,
            'memory_observations': [
                {'content': '经常把学习率和动量混淆', 'importance': 8},
            ],
        })

        result = agent.evaluate_session('梯度下降', 'Student: ...\n小艾: ...', {})

        self.assertEqual(result['understood_points'], ['梯度方向与参数更新方向相反'])
        self.assertEqual(result['gap_points'], ['没有提到学习率过大可能导致震荡'])
        self.assertEqual(result['misconceptions_detected'], ['认为学习率越大收敛越快'])
        self.assertEqual(result['cognitive_style_observation'], '倾向于用类比解释抽象概念')
        self.assertEqual(result['teaching_score'], 75)
        self.assertEqual(result['memory_observations'], [
            {'content': '经常把学习率和动量混淆', 'importance': 8},
        ])

    def test_evaluate_session_falls_back_to_defaults_on_non_json(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = '这是一段不是JSON的自然语言回复。'

        result = agent.evaluate_session('梯度下降', 'Student: ...\n小艾: ...', {})

        self.assertEqual(result['understood_points'], [])
        self.assertEqual(result['gap_points'], [])
        self.assertEqual(result['misconceptions_detected'], [])
        self.assertEqual(result['cognitive_style_observation'], '')
        self.assertEqual(result['teaching_score'], 70)
        self.assertEqual(result['memory_observations'], [])

    def test_evaluate_session_clamps_and_drops_invalid_memory_observations(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = json.dumps({
            'understood_points': [],
            'gap_points': [],
            'misconceptions_detected': [],
            'cognitive_style_observation': '',
            'teaching_score': 75,
            'memory_observations': [
                {'content': '一次性的小问题', 'importance': 99},
                {'content': '负重要度', 'importance': -3},
                {'content': '', 'importance': 5},
                {'importance': 7},
            ],
        })

        result = agent.evaluate_session('梯度下降', 'Student: ...\n小艾: ...', {})

        self.assertEqual(result['memory_observations'], [
            {'content': '一次性的小问题', 'importance': 10},
            {'content': '负重要度', 'importance': 1},
        ])

    def test_select_relevant_memories_returns_empty_for_empty_stream(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())

        self.assertEqual(agent.select_relevant_memories([], '梯度下降'), [])

    def test_select_relevant_memories_ranks_by_relevance_recency_and_importance(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())
        now = timezone.now()
        recent_relevant = {
            'id': '1', 'type': 'observation', 'content': '梯度下降的学习率容易设置过大',
            'importance': 9, 'topic': '梯度下降',
            'created_at': now.isoformat(), 'last_accessed_at': now.isoformat(),
        }
        old_unrelated = {
            'id': '2', 'type': 'observation', 'content': '这个学生对C语言指针有些困惑',
            'importance': 9, 'topic': 'C语言指针',
            'created_at': (now - timedelta(days=400)).isoformat(),
            'last_accessed_at': (now - timedelta(days=400)).isoformat(),
        }

        top = agent.select_relevant_memories([old_unrelated, recent_relevant], '梯度下降', k=1)

        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]['id'], '1')

    def test_select_relevant_memories_respects_k(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())
        now_iso = timezone.now().isoformat()
        memory_stream = [
            {'id': str(i), 'type': 'observation', 'content': f'观察{i}', 'importance': 5,
             'topic': '梯度下降', 'created_at': now_iso, 'last_accessed_at': now_iso}
            for i in range(5)
        ]

        top = agent.select_relevant_memories(memory_stream, '梯度下降', k=3)

        self.assertEqual(len(top), 3)

    def test_generate_reflection_returns_none_for_empty_stream(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())

        self.assertIsNone(agent.generate_reflection([], '梯度下降'))
        agent.client.generate_text.assert_not_called()

    def test_generate_reflection_returns_none_below_threshold(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())
        now_iso = timezone.now().isoformat()
        memory_stream = [
            {'id': '1', 'type': 'observation', 'content': '观察1', 'importance': 5,
             'topic': '梯度下降', 'created_at': now_iso, 'last_accessed_at': now_iso},
        ]

        reflection = agent.generate_reflection(memory_stream, '梯度下降', threshold=15)

        self.assertIsNone(reflection)
        agent.client.generate_text.assert_not_called()

    def test_generate_reflection_triggers_when_threshold_reached(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = '这名学生经常把学习率和动量混淆，建议多用类比讲解。'
        now_iso = timezone.now().isoformat()
        memory_stream = [
            {'id': '1', 'type': 'observation', 'content': '观察1', 'importance': 9,
             'topic': '梯度下降', 'created_at': now_iso, 'last_accessed_at': now_iso},
            {'id': '2', 'type': 'observation', 'content': '观察2', 'importance': 8,
             'topic': '梯度下降', 'created_at': now_iso, 'last_accessed_at': now_iso},
        ]

        reflection = agent.generate_reflection(memory_stream, '梯度下降', threshold=15)

        self.assertIsNotNone(reflection)
        self.assertEqual(reflection['type'], 'reflection')
        self.assertEqual(reflection['importance'], 8)
        self.assertEqual(reflection['topic'], '梯度下降')
        self.assertEqual(reflection['content'], '这名学生经常把学习率和动量混淆，建议多用类比讲解。')

    def test_generate_reflection_ignores_observations_before_last_reflection(self):
        agent = PeerLearnerAgent(user=object(), client=Mock())
        old_time = (timezone.now() - timedelta(days=10)).isoformat()
        reflection_time = (timezone.now() - timedelta(days=5)).isoformat()
        memory_stream = [
            {'id': '1', 'type': 'observation', 'content': '观察1', 'importance': 9,
             'topic': '梯度下降', 'created_at': old_time, 'last_accessed_at': old_time},
            {'id': 'r1', 'type': 'reflection', 'content': '早期总结', 'importance': 8,
             'topic': '梯度下降', 'created_at': reflection_time, 'last_accessed_at': reflection_time},
        ]

        reflection = agent.generate_reflection(memory_stream, '梯度下降', threshold=5)

        self.assertIsNone(reflection)
        agent.client.generate_text.assert_not_called()


class SelfExplanationAgentTests(SimpleTestCase):
    def test_evaluate_parses_valid_json(self):
        agent = SelfExplanationAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = json.dumps({
            'concept': '梯度下降',
            'covered_points': ['沿负梯度方向更新参数'],
            'gaps': ['没有提到学习率的作用'],
            'misconceptions': ['认为梯度下降一定能找到全局最优'],
            'quality_score': 65,
            'feedback': '基本思路正确，但还需补充学习率的作用。',
        })

        result = agent.evaluate('梯度下降', '梯度下降是一种优化算法...', '就是沿着梯度方向更新参数...')

        self.assertEqual(result['concept'], '梯度下降')
        self.assertEqual(result['covered_points'], ['沿负梯度方向更新参数'])
        self.assertEqual(result['gaps'], ['没有提到学习率的作用'])
        self.assertEqual(result['misconceptions'], ['认为梯度下降一定能找到全局最优'])
        self.assertEqual(result['quality_score'], 65)
        self.assertEqual(result['feedback'], '基本思路正确，但还需补充学习率的作用。')

    def test_evaluate_falls_back_to_defaults_on_non_json(self):
        agent = SelfExplanationAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = '这是一段不是JSON的自然语言回复。'

        result = agent.evaluate('梯度下降', '讲解内容...', '学生解释...')

        self.assertEqual(result['concept'], '')
        self.assertEqual(result['covered_points'], [])
        self.assertEqual(result['gaps'], [])
        self.assertEqual(result['misconceptions'], [])
        self.assertEqual(result['quality_score'], 70.0)
        self.assertEqual(result['feedback'], '')

    def test_evaluate_clamps_quality_score_to_valid_range(self):
        agent = SelfExplanationAgent(user=object(), client=Mock())
        agent.client.generate_text.return_value = json.dumps({
            'concept': '梯度下降',
            'quality_score': 150,
        })

        result = agent.evaluate('梯度下降', '...', '...')

        self.assertEqual(result['quality_score'], 100.0)


class ConversationSelfExplanationTests(TestCase):
    """自我解释提示（Self-Explanation Effect, Chi et al.）集成测试。"""

    def setUp(self):
        self.user = User.objects.create_user(username='se_student', email='se_student@example.com', password='test1234')
        self.client.force_login(self.user)

    def _send_learning_message(self, text, conversation_id=None):
        payload = {'text': text, 'mode': 'learning'}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        response = self.client.post(
            reverse('agent_system:api_conversation_send'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_direct_explanation_reply_appends_self_explanation_prompt(self):
        from . import views as agent_views

        original_generate_text = agent_views.profile_builder.client.generate_text
        agent_views.profile_builder.client.generate_text = lambda prompt, **kwargs: (
            '1. 结论：...\n2. 为什么会错：...\n3. 正确区分点：...\n4. 回看哪里：...\n5. 立刻自测：...'
        )
        try:
            payload = self._send_learning_message('下列哪项不是总线的特点？为什么总是出错。')
        finally:
            agent_views.profile_builder.client.generate_text = original_generate_text

        self.assertTrue(payload.get('ok'))
        assistant_message = Message.objects.filter(conversation_id=payload['conversation_id'], role='assistant').latest('id')

        self.assertTrue(assistant_message.content.endswith(SELF_EXPLANATION_PROMPT))
        self.assertTrue(assistant_message.metadata.get('awaiting_self_explanation'))
        explanation_text = assistant_message.metadata.get('explanation_text')
        self.assertFalse(explanation_text.endswith(SELF_EXPLANATION_PROMPT))
        self.assertEqual(assistant_message.content, explanation_text + SELF_EXPLANATION_PROMPT)

    def test_self_explanation_evaluation_updates_profile_and_creates_event(self):
        from . import views as agent_views

        convo = Conversation.objects.create(user=self.user, title='自我解释测试对话')
        Message.objects.create(
            conversation=convo,
            role='student',
            content='请帮我讲讲反向传播。',
            content_type='text',
        )
        Message.objects.create(
            conversation=convo,
            role='assistant',
            content='1. 结论：反向传播通过链式法则逐层计算梯度...',
            content_type='text',
            metadata={
                'awaiting_self_explanation': True,
                'explanation_text': '1. 结论：反向传播通过链式法则逐层计算梯度...',
            },
        )

        se_result = {
            'concept': '反向传播',
            'covered_points': ['链式法则用于计算梯度'],
            'gaps': ['没有提到激活函数的导数'],
            'misconceptions': ['认为反向传播只更新输出层权重'],
            'quality_score': 65,
            'feedback': '基本思路正确，但激活函数导数部分需要补充。',
        }

        original_generate_text = agent_views.profile_builder.client.generate_text
        agent_views.profile_builder.client.generate_text = lambda prompt, **kwargs: '继续讲解...'
        try:
            with patch('agent_system.agents.SelfExplanationAgent.evaluate', return_value=se_result):
                payload = self._send_learning_message('反向传播就是从输出层往前算梯度，更新每一层的权重。', convo.id)
        finally:
            agent_views.profile_builder.client.generate_text = original_generate_text

        self.assertTrue(payload.get('ok'))

        event = ProfileEvent.objects.filter(user=self.user, event_type='self_explanation_evaluation').latest('id')
        self.assertIsNotNone(event.processed_at)
        self.assertEqual(
            event.payload.get('profile_delta', {}).get('misconceptions'),
            ['认为反向传播只更新输出层权重'],
        )

        profile = StudentProfile.objects.get(user=self.user)
        self.assertIn('认为反向传播只更新输出层权重', profile.misconceptions)
        concept_profile = profile.knowledge_profile.get('反向传播')
        self.assertIsNotNone(concept_profile)
        self.assertEqual(concept_profile['mastery_score'], 65)
        self.assertEqual(concept_profile['source'], 'self_explanation')


class GetUserProfileDictTests(TestCase):
    def test_returns_empty_dict_when_no_profile(self):
        user = User.objects.create_user(username='sim_no_profile', email='sim_no_profile@example.com', password='pw')

        self.assertEqual(get_user_profile_dict(user), {})

    def test_returns_empty_dict_for_cold_start_profile(self):
        user = User.objects.create_user(username='sim_cold_start', email='sim_cold_start@example.com', password='pw')
        StudentProfile.objects.create(user=user)

        self.assertEqual(get_user_profile_dict(user), {})

    def test_returns_full_profile_when_populated(self):
        user = User.objects.create_user(username='sim_populated', email='sim_populated@example.com', password='pw')
        StudentProfile.objects.create(
            user=user,
            knowledge_profile={'指针': 0.4},
            cognitive_style='视觉型',
            learning_goals=['掌握C语言指针'],
            misconceptions=['认为数组和指针完全等价'],
            engagement={'score': 60},
            learning_preferences={'pace': 'slow'},
        )

        profile = get_user_profile_dict(user)

        self.assertEqual(profile, {
            'knowledge_profile': {'指针': 0.4},
            'cognitive_style': '视觉型',
            'learning_goals': ['掌握C语言指针'],
            'misconceptions': ['认为数组和指针完全等价'],
            'engagement': {'score': 60},
            'learning_preferences': {'pace': 'slow'},
        })