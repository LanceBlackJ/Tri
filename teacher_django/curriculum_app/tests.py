import json
import os
import shutil
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, SimpleTestCase, override_settings
from django.urls import reverse

from agent_system.models import LearningResource, ProfileEvent, StudentProfile
from agent_system.services.embeddings import compute_embedding
from core.models import User
from profile_app.models import StudentProfile as ProfileAppStudentProfile

from .models import Animation, Course, CourseMaterial, CourseOutline, LearningPlan, LearningProgress, MaterialChunk, MaterialQuestionStat, MaterialQuizAdaptivePolicy, MaterialQuizAttempt, OutlineExport, Slide
from .views import _build_fallback_quiz_questions, _build_material_quiz_context, _get_or_create_adaptive_policy, _normalize_course_topic, _normalize_practice_text, _parse_quiz_payload, _recompute_adaptive_strategy


class StructuredPptxExportTests(TestCase):
	def test_structured_slides_export_one_pptx_page_per_slide(self):
		from pptx import Presentation
		from .utils.pptx_exporter import export_outline_to_pptx

		media_root = tempfile.mkdtemp()
		self.addCleanup(lambda: shutil.rmtree(media_root, ignore_errors=True))
		user = User.objects.create_user(username='ppt_user', email='ppt@example.com', password='test1234')
		slides = [
			{
				'layout': 'cover',
				'theme': 'academic_light',
				'title': '总线讲解课',
				'teaching_goal': '建立整体认知',
				'bullets': ['理解总线作用', '识别关键组成', '完成自测'],
				'visual_blocks': [{'kind': 'bullet_card', 'label': '目标', 'items': ['理解总线作用', '识别关键组成', '完成自测']}],
				'speaker_notes': '用一个设备争用总线的例子开场。',
			},
			{
				'layout': 'process_flow',
				'theme': 'tech_blue',
				'title': '一次总线传输过程',
				'teaching_goal': '理解传输步骤',
				'bullets': ['申请总线', '获得仲裁', '传输数据', '释放总线'],
				'visual_blocks': [
					{'kind': 'step', 'label': '申请', 'text': '主设备提出请求'},
					{'kind': 'step', 'label': '仲裁', 'text': '仲裁器分配控制权'},
					{'kind': 'step', 'label': '传输', 'text': '完成数据交换'},
				],
				'speaker_notes': '强调每一步为什么不能省略。',
			},
		]
		outline = CourseOutline.objects.create(
			user=user,
			title='总线讲解课',
			outline_data=json.dumps({'title': '总线讲解课', 'slide_deck': slides, 'resources': {'ppt': {'structured_slides': slides}}}, ensure_ascii=False),
			status='completed',
			progress=100,
		)

		with override_settings(MEDIA_ROOT=media_root):
			file_path, filename = export_outline_to_pptx(outline)

		self.assertTrue(os.path.exists(file_path))
		self.assertTrue(filename.endswith('.pptx'))
		presentation = Presentation(file_path)
		self.assertEqual(len(presentation.slides), 1 + len(slides))


class QuizPayloadNormalizationTests(SimpleTestCase):
	def test_normalize_course_topic_strips_request_phrase(self):
		self.assertEqual(_normalize_course_topic('我想学习梯度下降'), '梯度下降')
		self.assertEqual(_normalize_course_topic('请讲解一下线性回归是怎么起作用的'), '线性回归')

	def test_normalize_practice_text_handles_fullwidth_and_punctuation(self):
		self.assertEqual(_normalize_practice_text('  I／O， 读 写。  '), 'i/o读写')
		self.assertEqual(_normalize_practice_text(' 正 确 '), '正确')

	def test_true_false_question_without_options_gets_default_choices(self):
		payload, questions = _parse_quiz_payload({
			'questions': [
				{
					'id': 1,
					'type': 'true_false',
					'question': 'plug and play技术是windows95发布后才支持的。',
					'answer': '错',
				}
			]
		})

		self.assertEqual(len(questions), 1)
		self.assertEqual(payload['questions'][0]['options'], ['正确', '错误'])
		self.assertEqual(payload['questions'][0]['answer'], '错误')
		self.assertEqual(payload['questions'][0]['type'], 'true_false')

	def test_choice_question_with_choices_field_is_normalized_to_options(self):
		payload, questions = _parse_quiz_payload({
			'questions': [
				{
					'id': 1,
					'type': 'single_choice',
					'question': '总线仲裁的核心作用是什么？',
					'choices': [
						{'label': 'A', 'text': '解决总线争用', 'correct': True},
						{'label': 'B', 'text': '提高主频', 'correct': False},
					],
				}
			]
		})

		self.assertEqual(len(questions), 1)
		self.assertEqual(payload['questions'][0]['options'], ['解决总线争用', '提高主频'])
		self.assertEqual(payload['questions'][0]['answer'], '解决总线争用')
		self.assertEqual(payload['questions'][0]['type'], 'single_choice')

	def test_single_choice_question_injects_missing_answer_into_options(self):
		payload, questions = _parse_quiz_payload({
			'questions': [
				{
					'id': 1,
					'type': 'single_choice',
					'question': '下列哪项不是总线的特点？',
					'options': ['分散连接 VS 总线连接', '总线的基本概念'],
					'answer': 'plug and play技术是windows95发布后才支持的。',
				}
			]
		})

		self.assertEqual(len(questions), 1)
		self.assertEqual(payload['questions'][0]['type'], 'single_choice')
		self.assertEqual(payload['questions'][0]['options'][0], 'plug and play技术是windows95发布后才支持的。')
		self.assertEqual(payload['questions'][0]['answer'], 'plug and play技术是windows95发布后才支持的。')

	def test_short_answer_without_answer_uses_explanation_or_is_removed(self):
		payload, questions = _parse_quiz_payload({
			'questions': [
				{
					'id': 1,
					'type': 'short_answer',
					'question': '在个人计算机中，系统增加设备时需要分配哪些系统资源？',
					'explanation': '需要分配中断号、DMA通道、I/O端口地址和内存地址等系统资源。',
				},
				{
					'id': 2,
					'type': 'short_answer',
					'question': '这道题既没有答案也没有解析。',
				}
			]
		})

		self.assertEqual(len(questions), 1)
		self.assertEqual(payload['questions'][0]['answer'], '需要分配中断号、DMA通道、I/O端口地址和内存地址等系统资源。')
		self.assertEqual(payload['questions'][0]['question'], '在个人计算机中，系统增加设备时需要分配哪些系统资源？')


class MaterialQuizPracticeFlowTests(TestCase):
	def setUp(self):
		from agent_system.services.profile_signal_collector import ProfileSignalCollector
		# 画像信号采集器使用进程级内存队列，按批量阈值触发处理；
		# 清空队列以避免跨用例残留信号在本用例事务中被处理。
		ProfileSignalCollector._queue = []

		self.user = User.objects.create_user(username='student1', email='student1@example.com', password='test1234')
		self.client.force_login(self.user)

		self.course = Course.objects.create(
			owner=self.user,
			title='计算机组成原理',
			status='published',
			visibility='login',
		)
		self.material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='总线与控制',
			material_type='pdf',
			file=SimpleUploadedFile('bus.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
			page_count=12,
		)
		MaterialChunk.objects.create(
			material=self.material,
			chunk_index=0,
			source_page='3',
			heading='总线仲裁',
			content='总线仲裁负责解决多个主设备争用总线的问题。',
		)

		self.quiz_resource = LearningResource.objects.create(
			title='总线与控制 - 资料练习题',
			resource_type='quiz',
			author=self.user,
			content=json.dumps({
				'questions': [
					{
						'id': 1,
						'type': 'single_choice',
						'question': '总线仲裁的核心作用是什么？',
						'options': ['解决总线争用', '提高主频'],
						'answer': '解决总线争用',
						'explanation': '仲裁机制用于决定哪个设备获得总线控制权。',
						'source_page': '3',
						'source_heading': '总线仲裁',
						'knowledge_tag': '总线仲裁',
					}
				]
			}, ensure_ascii=False),
			metadata={
				'course_id': self.course.id,
				'material_id': self.material.id,
				'material_title': self.material.title,
				'practice_profile': {
					'difficulty_stage': 'reinforce',
					'difficulty_label': '同层巩固',
					'focus_question_fingerprint': '',
				},
			},
		)

	def test_submit_material_quiz_uses_server_side_quiz_resource_for_scoring(self):
		url = reverse('submit_material_quiz', args=[self.course.id, self.material.id])
		response = self.client.post(
			url,
			data=json.dumps({
				'quiz_resource_id': self.quiz_resource.id,
				'quiz': {
					'questions': [
						{
							'question': '总线仲裁的核心作用是什么？',
							'answer': '提高主频',
						}
					]
				},
				'answers': {'q0': '解决总线争用'},
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertTrue(payload['success'])
		self.assertEqual(payload['score'], 100.0)
		self.assertEqual(payload['correct'], 1)
		self.assertEqual(MaterialQuizAttempt.objects.count(), 1)
		event = ProfileEvent.objects.filter(user=self.user, event_type='material_quiz_submitted').latest('id')
		self.assertIsNotNone(event.processed_at)
		self.assertEqual(event.course_id, self.course.id)
		self.assertEqual(event.material_id, self.material.id)

		from agent_system.services.profile_signal_collector import ProfileSignalCollector
		# 画像信号采集器按批量阈值异步处理；测试中显式触发一次处理，
		# 确保 BKT 知识追踪更新在断言前已落库。
		ProfileSignalCollector._process_batch()

		profile = StudentProfile.objects.get(user=self.user)
		self.assertIn('总线仲裁', profile.knowledge_profile)
		# BKT 更新在 material_quiz_submitted 事件之后运行，会把该知识点的
		# knowledge_profile 条目从事件写入的 0-100 量表字典统一转换为 0-1 浮点掌握度。
		self.assertEqual(profile.knowledge_profile['总线仲裁'], 1.0)
		self.assertEqual(profile.engagement.get('quiz_event_count'), 1)

	def test_second_wrong_answer_creates_wrong_book_and_review_recommendation(self):
		url = reverse('submit_material_quiz', args=[self.course.id, self.material.id])
		LearningPlan.objects.create(
			user=self.user,
			title='总线学习路径',
			plan_data=json.dumps({
				'title': '总线学习路径',
				'matched_course': {'id': self.course.id, 'title': self.course.title},
				'modules': [{'name': '阶段一：总线基础', 'lessons': []}],
			}, ensure_ascii=False),
			status='generated',
		)
		body = {
			'quiz_resource_id': self.quiz_resource.id,
			'answers': {'q0': '提高主频'},
		}

		first_response = self.client.post(url, data=json.dumps(body), content_type='application/json')
		self.assertEqual(first_response.status_code, 200)
		first_payload = first_response.json()
		self.assertIn('auto_learning_adjustment', first_payload)
		self.assertTrue(first_payload['auto_learning_adjustment']['triggered'])
		self.assertEqual(first_payload['auto_learning_adjustment']['trigger_source_label'], '资料小测触发')
		second_response = self.client.post(url, data=json.dumps(body), content_type='application/json')

		self.assertEqual(second_response.status_code, 200)
		payload = second_response.json()
		self.assertTrue(payload['success'])
		self.assertEqual(payload['correct'], 0)
		self.assertIn('practice_insights', payload)
		self.assertEqual(len(payload['practice_insights']['review_recommendations']), 1)
		self.assertEqual(payload['practice_insights']['wrong_book'][0]['knowledge_tag'], '总线仲裁')
		self.assertIn('auto_learning_adjustment', payload)
		auto_adjustment = payload['auto_learning_adjustment']
		self.assertIsNone(auto_adjustment)
		self.assertEqual(LearningPlan.objects.filter(user=self.user).count(), 2)
		new_plan = LearningPlan.objects.filter(user=self.user).order_by('-id').first()
		new_payload = json.loads(new_plan.plan_data)
		self.assertEqual(new_payload['adjustment_meta']['trigger_source'], 'material-quiz')
		self.assertEqual(new_payload['adjustment_meta']['trigger_attempt_id'], first_payload['quiz_attempt_id'])
		self.assertIn('自动触发路径重排', ''.join(new_payload['recommendation_reason']))

		stat = MaterialQuestionStat.objects.get(user=self.user, material=self.material)
		self.assertEqual(stat.wrong_count, 2)
		self.assertEqual(stat.consecutive_wrong_count, 2)

	def test_high_score_does_not_auto_refresh_learning_plan(self):
		LearningPlan.objects.create(
			user=self.user,
			title='总线学习路径',
			plan_data=json.dumps({
				'title': '总线学习路径',
				'matched_course': {'id': self.course.id, 'title': self.course.title},
				'modules': [{'name': '阶段一：总线基础', 'lessons': []}],
			}, ensure_ascii=False),
			status='generated',
		)

		response = self.client.post(
			reverse('submit_material_quiz', args=[self.course.id, self.material.id]),
			data=json.dumps({
				'quiz_resource_id': self.quiz_resource.id,
				'answers': {'q0': '解决总线争用'},
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertEqual(payload['score'], 100.0)
		self.assertIsNone(payload['auto_learning_adjustment'])
		self.assertEqual(LearningPlan.objects.filter(user=self.user).count(), 1)

	def test_submit_material_quiz_feedback_updates_adaptive_policy(self):
		submit_url = reverse('submit_material_quiz', args=[self.course.id, self.material.id])
		submit_response = self.client.post(
			submit_url,
			data=json.dumps({
				'quiz_resource_id': self.quiz_resource.id,
				'answers': {'q0': '提高主频'},
			}),
			content_type='application/json',
		)

		self.assertEqual(submit_response.status_code, 200)
		submit_payload = submit_response.json()
		attempt_id = submit_payload.get('quiz_attempt_id')
		self.assertTrue(attempt_id)

		feedback_url = reverse('submit_material_quiz_feedback', args=[self.course.id, self.material.id])
		feedback_response = self.client.post(
			feedback_url,
			data=json.dumps({
				'quiz_attempt_id': attempt_id,
				'feedback_type': 'off_topic',
			}),
			content_type='application/json',
		)

		self.assertEqual(feedback_response.status_code, 200)
		feedback_payload = feedback_response.json()
		self.assertTrue(feedback_payload['success'])
		self.assertEqual(feedback_payload['feedback_type'], 'off_topic')
		self.assertIn('adaptive_policy', feedback_payload)

		policy = MaterialQuizAdaptivePolicy.objects.get(user=self.user, material=self.material)
		self.assertEqual(policy.feedback_counts.get('off_topic'), 1)
		event = ProfileEvent.objects.filter(user=self.user, event_type='material_quiz_feedback').latest('id')
		self.assertEqual(event.payload['quiz_attempt_id'], attempt_id)
		self.assertEqual(event.payload['feedback_type'], 'off_topic')
		self.assertIsNotNone(event.processed_at)

		attempt = MaterialQuizAttempt.objects.get(id=attempt_id)
		result_feedback = (attempt.result or {}).get('user_feedback') or []
		self.assertTrue(result_feedback)
		self.assertEqual(result_feedback[-1]['feedback_type'], 'off_topic')

	def test_question_level_feedback_updates_knowledge_feedback_bucket(self):
		submit_url = reverse('submit_material_quiz', args=[self.course.id, self.material.id])
		submit_response = self.client.post(
			submit_url,
			data=json.dumps({
				'quiz_resource_id': self.quiz_resource.id,
				'answers': {'q0': '提高主频'},
			}),
			content_type='application/json',
		)
		self.assertEqual(submit_response.status_code, 200)
		submit_payload = submit_response.json()
		attempt_id = submit_payload.get('quiz_attempt_id')
		self.assertTrue(attempt_id)
		fingerprint = submit_payload['details'][0]['question_fingerprint']

		feedback_url = reverse('submit_material_quiz_feedback', args=[self.course.id, self.material.id])
		feedback_response = self.client.post(
			feedback_url,
			data=json.dumps({
				'quiz_attempt_id': attempt_id,
				'feedback_type': 'off_topic',
				'question_fingerprint': fingerprint,
			}),
			content_type='application/json',
		)

		self.assertEqual(feedback_response.status_code, 200)
		feedback_payload = feedback_response.json()
		self.assertEqual(feedback_payload['knowledge_tag'], '总线仲裁')

		policy = MaterialQuizAdaptivePolicy.objects.get(user=self.user, material=self.material)
		bucket = policy.strategy.get('knowledge_feedback', {}).get('总线仲裁', {})
		self.assertEqual(bucket.get('off_topic'), 1)

	def test_quiz_context_covers_late_chunks_in_long_material(self):
		for index in range(1, 13):
			MaterialChunk.objects.create(
				material=self.material,
				chunk_index=index,
				source_page=str(index + 3),
				heading=f'第{index + 3}页',
				content=f'这是第{index + 3}页的重点内容，包含章节 {index + 3} 的核心概念。',
			)

		context_text = _build_material_quiz_context(self.material, max_chunks=6, max_total_chars=4000)

		self.assertIn('总线仲裁（第3页/张）', context_text)
		self.assertIn('第15页（第15页/张）', context_text)

	@patch('curriculum_app.views._grade_short_answer_with_ai')
	def test_submit_material_quiz_uses_ai_semantic_grading_for_short_answer(self, grade_short_answer_mock):
		grade_short_answer_mock.return_value = {
			'correct_flag': True,
			'grading_source': 'ai',
			'grading_note': '语义一致，关键点完整。',
		}
		short_answer_resource = LearningResource.objects.create(
			title='总线与控制 - 简答练习题',
			resource_type='quiz',
			author=self.user,
			content=json.dumps({
				'questions': [
					{
						'id': 1,
						'type': 'short_answer',
						'question': '在个人计算机中，系统增加设备时需要分配哪些系统资源？',
						'answer': '需要分配中断号、DMA通道、I/O端口地址和内存地址等系统资源。',
						'explanation': '新增设备时需要占用中断、DMA、端口和内存地址等资源。',
					}
				]
			}, ensure_ascii=False),
			metadata={
				'course_id': self.course.id,
				'material_id': self.material.id,
				'material_title': self.material.title,
			},
		)

		url = reverse('submit_material_quiz', args=[self.course.id, self.material.id])
		response = self.client.post(
			url,
			data=json.dumps({
				'quiz_resource_id': short_answer_resource.id,
				'answers': {'q0': '需要分配IRQ、DMA、I/O端口和内存地址这些资源。'},
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertTrue(payload['success'])
		self.assertEqual(payload['correct'], 1)
		self.assertEqual(payload['details'][0]['grading_source'], 'ai')
		grade_short_answer_mock.assert_called_once()

	@patch('agent_system.agents.QuizAgent.generate_quiz_from_context')
	def test_generate_material_quiz_repairs_question_only_output(self, generate_quiz_mock):
		broken_resource = LearningResource.objects.create(
			title='总线与控制 - 损坏练习题',
			resource_type='quiz',
			author=self.user,
			content=json.dumps({
				'questions': [
					{'question': '总线传输周期包括哪些元素？'},
					{'question': '描述单总线结构中所有部件连到单一总线的影响。'},
				]
			}, ensure_ascii=False),
		)
		generate_quiz_mock.return_value = broken_resource

		url = reverse('generate_material_quiz', args=[self.course.id, self.material.id])
		response = self.client.post(
			url,
			data=json.dumps({'count': 2}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertTrue(payload['success'])
		self.assertEqual(payload['question_count'], 3)
		self.assertTrue(all(item.get('answer') for item in payload['quiz']['questions']))
		broken_resource.refresh_from_db()
		repaired = json.loads(broken_resource.content)
		self.assertTrue(all(item.get('answer') for item in repaired['questions']))
		event = ProfileEvent.objects.filter(user=self.user, event_type='material_quiz_generated').latest('id')
		self.assertEqual(event.payload['quiz_resource_id'], broken_resource.id)
		self.assertEqual(event.payload['question_count'], payload['question_count'])
		self.assertIsNotNone(event.processed_at)

	@patch('agent_system.agents.QuizAgent.generate_quiz_from_context')
	def test_generate_material_quiz_applies_adaptive_policy_bias(self, generate_quiz_mock):
		adaptive_resource = LearningResource.objects.create(
			title='总线与控制 - 自适应练习题',
			resource_type='quiz',
			author=self.user,
			content=json.dumps({
				'questions': [
					{
						'id': 1,
						'type': 'single_choice',
						'question': '总线仲裁主要解决什么问题？',
						'options': ['总线争用', '刷新频率'],
						'answer': '总线争用',
					}
				]
			}, ensure_ascii=False),
		)
		generate_quiz_mock.return_value = adaptive_resource
		MaterialQuizAdaptivePolicy.objects.create(
			user=self.user,
			course=self.course,
			material=self.material,
			feedback_counts={
				'useful': 0,
				'not_useful': 0,
				'too_easy': 3,
				'too_hard': 0,
				'off_topic': 0,
			},
			strategy={
				'question_count_delta': 0,
				'difficulty_bias': 'balanced',
				'domain_signal_threshold': 1,
				'off_topic_guard': 'standard',
			},
		)

		url = reverse('generate_material_quiz', args=[self.course.id, self.material.id])
		response = self.client.post(
			url,
			data=json.dumps({'count': 5}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertTrue(payload['success'])
		self.assertIn('adaptive_policy', payload)
		self.assertEqual(payload['adaptive_policy']['strategy']['difficulty_bias'], 'progressive')
		self.assertEqual(payload['practice_profile']['difficulty_stage'], 'progressive')

	@patch('agent_system.agents.QuizAgent.generate_quiz_from_context')
	def test_generate_material_quiz_persists_displayed_questions_for_consistent_scoring(self, generate_quiz_mock):
		resource = LearningResource.objects.create(
			title='总线与控制 - 同步评分题目',
			resource_type='quiz',
			author=self.user,
			content=json.dumps({
				'questions': [
					{
						'id': 1,
						'type': 'single_choice',
						'question': '总线仲裁主要解决什么问题？',
						'options': ['总线争用', '刷新频率'],
						'answer': '总线争用',
					}
				]
			}, ensure_ascii=False),
		)
		generate_quiz_mock.return_value = resource

		url = reverse('generate_material_quiz', args=[self.course.id, self.material.id])
		response = self.client.post(
			url,
			data=json.dumps({'count': 3}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertTrue(payload['success'])
		displayed_questions = payload['quiz']['questions']
		self.assertTrue(displayed_questions)

		resource.refresh_from_db()
		stored_payload = json.loads(resource.content or '{}')
		stored_questions = stored_payload.get('questions') if isinstance(stored_payload, dict) else []
		self.assertEqual(stored_questions, displayed_questions)
		self.assertTrue(all(item.get('question_fingerprint') for item in displayed_questions))

	def test_submit_material_quiz_rejects_mismatched_question_fingerprints(self):
		url = reverse('submit_material_quiz', args=[self.course.id, self.material.id])
		response = self.client.post(
			url,
			data=json.dumps({
				'quiz_resource_id': self.quiz_resource.id,
				'question_fingerprints': {'q0': 'mismatch-fingerprint'},
				'answers': {'q0': '解决总线争用'},
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 409)
		payload = response.json()
		self.assertFalse(payload['success'])
		self.assertIn('题目版本已更新', payload['error'])

	def test_fallback_quiz_questions_do_not_use_page_heading_as_prompt(self):
		page_material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='分页标题样例',
			material_type='pdf',
			file=SimpleUploadedFile('page-title.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
		)
		MaterialChunk.objects.create(
			material=page_material,
			chunk_index=0,
			source_page='1',
			heading='第 1 页',
			keyword_summary='即插即用资源分配',
			content='系统增加设备时通常需要分配中断号、DMA通道、I/O端口地址和内存地址。',
		)

		questions = _build_fallback_quiz_questions(page_material, raw_questions=[], count=1)

		self.assertEqual(len(questions), 1)
		self.assertEqual(questions[0]['type'], 'single_choice')
		self.assertIn('即插即用资源分配', questions[0]['question'])
		self.assertIn('说法正确的是', questions[0]['question'])
		self.assertNotIn('资料内容', questions[0]['question'])
		self.assertTrue(questions[0].get('options'))
		self.assertNotIn('第 1 页', questions[0]['question'])

	def test_fallback_quiz_questions_filter_non_domain_keyword_noise(self):
		noise_material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='关键词噪声样例',
			material_type='pdf',
			file=SimpleUploadedFile('noise.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
		)
		MaterialChunk.objects.create(
			material=noise_material,
			chunk_index=0,
			source_page='2',
			heading='第 2 页',
			keyword_summary='海纳百川 / 厚德笃学 / 自强不息 / 知行合一 / 系统总线 / 大连理工大学',
			content='系统总线用于在 CPU、内存与外设之间传输数据和控制信号。',
		)

		questions = _build_fallback_quiz_questions(noise_material, raw_questions=[], count=1)

		self.assertEqual(len(questions), 1)
		self.assertEqual(questions[0]['type'], 'single_choice')
		self.assertIn('系统总线', questions[0]['question'])
		self.assertIn('说法正确的是', questions[0]['question'])
		self.assertNotIn('资料内容', questions[0]['question'])
		self.assertIn('系统总线用于在 CPU、内存与外设之间传输数据和控制信号', questions[0]['answer'])
		self.assertTrue(all('页面展示优化' not in option for option in questions[0].get('options', [])))
		option_text = ' '.join(questions[0].get('options', []))
		self.assertIn('只在 CPU 内部单向工作', option_text)
		self.assertIn('不需要中断、DMA 或 I/O 资源分配', option_text)
		self.assertIn('不传输数据和地址信息', option_text)
		self.assertNotIn('海纳百川', questions[0]['question'])
		self.assertNotIn('厚德笃学', questions[0]['question'])
		self.assertNotIn('大连理工大学', questions[0]['question'])

	def test_fallback_quiz_questions_ignore_noisy_fact_sentence(self):
		noisy_fact_material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='内容噪声样例',
			material_type='pdf',
			file=SimpleUploadedFile('content-noise.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
		)
		MaterialChunk.objects.create(
			material=noisy_fact_material,
			chunk_index=0,
			source_page='4',
			heading='系统总线基础',
			keyword_summary='系统总线 / 数据传输',
			content='海纳百川 厚德笃学 自强不息 知行合一。系统总线负责在 CPU、内存和外设之间传输数据、地址和控制信号。',
		)

		questions = _build_fallback_quiz_questions(noisy_fact_material, raw_questions=[], count=1)

		self.assertEqual(len(questions), 1)
		self.assertEqual(questions[0]['type'], 'single_choice')
		self.assertIn('系统总线负责在 CPU、内存和外设之间传输数据、地址和控制信号', questions[0]['answer'])
		self.assertNotIn('海纳百川', questions[0]['answer'])
		self.assertIn('只在 CPU 内部单向工作', ' '.join(questions[0].get('options', [])))

	def test_fallback_quiz_questions_skips_non_domain_material(self):
		low_density_material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='低密度主题样例',
			material_type='pdf',
			file=SimpleUploadedFile('low-density.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
		)
		MaterialChunk.objects.create(
			material=low_density_material,
			chunk_index=0,
			source_page='3',
			heading='第 3 页',
			keyword_summary='校园文化',
			content='本页主要介绍课程背景与课堂安排。',
		)

		questions = _build_fallback_quiz_questions(low_density_material, raw_questions=[], count=1)

		self.assertEqual(len(questions), 0)

	def test_fallback_quiz_questions_skip_numeric_topic_candidate(self):
		numeric_noise_material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='数字噪声样例',
			material_type='pdf',
			file=SimpleUploadedFile('numeric-noise.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
		)
		MaterialChunk.objects.create(
			material=numeric_noise_material,
			chunk_index=0,
			source_page='6',
			heading='第 6 页',
			keyword_summary='51 / 系统总线',
			content='系统总线负责在 CPU、内存和外设之间传输数据、地址和控制信号。',
		)

		questions = _build_fallback_quiz_questions(numeric_noise_material, raw_questions=[], count=1)

		self.assertEqual(len(questions), 1)
		self.assertEqual(questions[0]['type'], 'single_choice')
		self.assertIn('系统总线', questions[0]['question'])
		self.assertNotIn('围绕“51”', questions[0]['question'])
		self.assertNotIn('围绕“', questions[0]['question'])

	def test_fallback_true_false_question_contains_options(self):
		multi_chunk_material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='判断题选项样例',
			material_type='pdf',
			file=SimpleUploadedFile('true-false-options.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
		)
		MaterialChunk.objects.create(
			material=multi_chunk_material,
			chunk_index=0,
			source_page='1',
			heading='系统总线',
			keyword_summary='系统总线',
			content='系统总线用于在 CPU、内存与外设之间传输数据和控制信号。',
		)
		MaterialChunk.objects.create(
			material=multi_chunk_material,
			chunk_index=1,
			source_page='2',
			heading='中断机制',
			keyword_summary='中断处理',
			content='中断机制用于在外设请求时打断当前流程并转入中断服务程序。',
		)

		questions = _build_fallback_quiz_questions(multi_chunk_material, raw_questions=[], count=2)

		self.assertEqual(len(questions), 2)
		self.assertEqual(questions[1]['type'], 'true_false')
		self.assertEqual(questions[1].get('options'), ['正确', '错误'])
		self.assertEqual(questions[1].get('answer'), '错误')
		self.assertIn('只在关机阶段触发', questions[1].get('question', ''))
		self.assertIn('资料中的关键事实：', questions[1].get('explanation', ''))
		self.assertIn('错误点：', questions[1].get('explanation', ''))

	def test_submit_material_quiz_true_false_accepts_synonym_answer(self):
		true_false_resource = LearningResource.objects.create(
			title='总线与控制 - 判断练习题',
			resource_type='quiz',
			author=self.user,
			content=json.dumps({
				'questions': [
					{
						'id': 1,
						'type': 'true_false',
						'question': '判断正误：系统总线负责在 CPU、内存和外设之间传输数据、地址和控制信号。',
						'options': ['正确', '错误'],
						'answer': '正确',
						'explanation': '资料中的关键事实：系统总线负责在 CPU、内存和外设之间传输数据、地址和控制信号。',
					}
				]
			}, ensure_ascii=False),
			metadata={
				'course_id': self.course.id,
				'material_id': self.material.id,
				'material_title': self.material.title,
			},
		)

		url = reverse('submit_material_quiz', args=[self.course.id, self.material.id])
		response = self.client.post(
			url,
			data=json.dumps({
				'quiz_resource_id': true_false_resource.id,
				'answers': {'q0': '对'},
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertTrue(payload['success'])
		self.assertEqual(payload['correct'], 1)

	def test_fallback_quiz_questions_skip_fragmented_enumeration_fact(self):
		fragment_material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='枚举碎片样例',
			material_type='pdf',
			file=SimpleUploadedFile('fragment-enum.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
		)
		MaterialChunk.objects.create(
			material=fragment_material,
			chunk_index=0,
			source_page='7',
			heading='I/O接口结构',
			keyword_summary='I/O接口',
			content='I/O接口0 I/O接口1 I/O接口n…',
		)

		questions = _build_fallback_quiz_questions(fragment_material, raw_questions=[], count=1)

		self.assertEqual(len(questions), 0)

	def test_fallback_quiz_questions_cover_rear_pages_when_count_is_small(self):
		coverage_material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='覆盖后段页码样例',
			material_type='pdf',
			file=SimpleUploadedFile('coverage.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
		)
		for index in range(10):
			MaterialChunk.objects.create(
				material=coverage_material,
				chunk_index=index,
				source_page=str(index + 1),
				heading=f'系统总线第{index + 1}节',
				keyword_summary='系统总线',
				content=f'系统总线负责在 CPU、内存和外设之间传输数据、地址和控制信号，第{index + 1}段。',
			)

		questions = _build_fallback_quiz_questions(coverage_material, raw_questions=[], count=2)

		self.assertEqual(len(questions), 2)
		pages = {str(item.get('source_page') or '') for item in questions}
		self.assertIn('10', pages)

	def test_fallback_quiz_questions_variant_seed_changes_selected_chunks(self):
		variant_material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='变体种子样例',
			material_type='pdf',
			file=SimpleUploadedFile('variant-seed.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
		)
		for index in range(8):
			MaterialChunk.objects.create(
				material=variant_material,
				chunk_index=index,
				source_page=str(index + 1),
				heading=f'总线事务第{index + 1}节',
				keyword_summary='总线事务',
				content=f'总线事务用于协调主设备与存储器的数据交换流程，第{index + 1}段。',
			)

		questions_seed_1 = _build_fallback_quiz_questions(variant_material, raw_questions=[], count=3, variant_seed=1)
		questions_seed_2 = _build_fallback_quiz_questions(variant_material, raw_questions=[], count=3, variant_seed=2)

		self.assertEqual(len(questions_seed_1), 3)
		self.assertEqual(len(questions_seed_2), 3)
		first_page_seed_1 = str(questions_seed_1[0].get('source_page') or '')
		first_page_seed_2 = str(questions_seed_2[0].get('source_page') or '')
		self.assertNotEqual(first_page_seed_1, first_page_seed_2)


class EloRatingTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='student_elo', email='student_elo@example.com', password='test1234')
		self.client.force_login(self.user)

		self.course = Course.objects.create(
			owner=self.user,
			title='计算机组成原理',
			status='published',
			visibility='login',
		)
		self.material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='总线与控制',
			material_type='pdf',
			file=SimpleUploadedFile('bus-elo.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
			page_count=12,
		)
		MaterialChunk.objects.create(
			material=self.material,
			chunk_index=0,
			source_page='3',
			heading='总线仲裁',
			content='总线仲裁负责解决多个主设备争用总线的问题。',
		)

		self.quiz_resource = LearningResource.objects.create(
			title='总线与控制 - 资料练习题',
			resource_type='quiz',
			author=self.user,
			content=json.dumps({
				'questions': [
					{
						'id': 1,
						'type': 'single_choice',
						'question': '总线仲裁的核心作用是什么？',
						'options': ['解决总线争用', '提高主频'],
						'answer': '解决总线争用',
						'explanation': '仲裁机制用于决定哪个设备获得总线控制权。',
						'source_page': '3',
						'source_heading': '总线仲裁',
						'knowledge_tag': '总线仲裁',
					}
				]
			}, ensure_ascii=False),
			metadata={
				'course_id': self.course.id,
				'material_id': self.material.id,
				'material_title': self.material.title,
				'practice_profile': {
					'difficulty_stage': 'reinforce',
					'difficulty_label': '同层巩固',
					'focus_question_fingerprint': '',
				},
			},
		)

	def test_elo_rating_increases_for_student_and_decreases_for_item_on_correct_answer(self):
		url = reverse('submit_material_quiz', args=[self.course.id, self.material.id])
		response = self.client.post(
			url,
			data=json.dumps({
				'quiz_resource_id': self.quiz_resource.id,
				'answers': {'q0': '解决总线争用'},
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertEqual(payload['correct'], 1)

		policy = MaterialQuizAdaptivePolicy.objects.get(user=self.user, material=self.material)
		self.assertGreater(policy.ability_rating, 1200.0)

		stat = MaterialQuestionStat.objects.get(user=self.user, material=self.material)
		self.assertLess(stat.elo_rating, 1200.0)

	def test_elo_rating_decreases_for_student_and_increases_for_item_on_incorrect_answer(self):
		url = reverse('submit_material_quiz', args=[self.course.id, self.material.id])
		response = self.client.post(
			url,
			data=json.dumps({
				'quiz_resource_id': self.quiz_resource.id,
				'answers': {'q0': '提高主频'},
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertEqual(payload['correct'], 0)

		policy = MaterialQuizAdaptivePolicy.objects.get(user=self.user, material=self.material)
		self.assertLess(policy.ability_rating, 1200.0)

		stat = MaterialQuestionStat.objects.get(user=self.user, material=self.material)
		self.assertGreater(stat.elo_rating, 1200.0)

	def test_elo_ratings_accumulate_across_multiple_attempts(self):
		url = reverse('submit_material_quiz', args=[self.course.id, self.material.id])
		body = {
			'quiz_resource_id': self.quiz_resource.id,
			'answers': {'q0': '解决总线争用'},
		}

		first_response = self.client.post(url, data=json.dumps(body), content_type='application/json')
		self.assertEqual(first_response.status_code, 200)
		policy = MaterialQuizAdaptivePolicy.objects.get(user=self.user, material=self.material)
		self.assertAlmostEqual(policy.ability_rating, 1212.0, places=2)
		first_rating = policy.ability_rating

		second_response = self.client.post(url, data=json.dumps(body), content_type='application/json')
		self.assertEqual(second_response.status_code, 200)
		policy.refresh_from_db()
		self.assertAlmostEqual(policy.ability_rating, 1223.31, places=2)
		self.assertGreater(policy.ability_rating, first_rating)

	def test_recompute_adaptive_strategy_uses_elo_gap_when_balanced(self):
		MaterialQuizAttempt.objects.create(
			user=self.user,
			course=self.course,
			material=self.material,
			score=70,
			total_questions=1,
			correct_count=1,
		)
		MaterialQuestionStat.objects.create(
			user=self.user,
			course=self.course,
			material=self.material,
			question_fingerprint='fp-elo-gap',
			elo_rating=1200.0,
		)

		policy = _get_or_create_adaptive_policy(self.user, self.course, self.material)

		policy.ability_rating = 1300.0
		snapshot = _recompute_adaptive_strategy(policy, self.user, self.material)
		self.assertEqual(snapshot['strategy']['difficulty_bias'], 'progressive')

		policy.ability_rating = 1100.0
		snapshot = _recompute_adaptive_strategy(policy, self.user, self.material)
		self.assertEqual(snapshot['strategy']['difficulty_bias'], 'reinforce')

	def test_submit_material_quiz_response_includes_ability_rating(self):
		url = reverse('submit_material_quiz', args=[self.course.id, self.material.id])
		response = self.client.post(
			url,
			data=json.dumps({
				'quiz_resource_id': self.quiz_resource.id,
				'answers': {'q0': '解决总线争用'},
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertIsInstance(payload['ability_rating'], (int, float))
		self.assertIsInstance(payload['ability_rating_delta'], (int, float))


class GeneratedCourseOutlineManagementTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='course_creator', email='creator@example.com', password='test1234')
		self.other_user = User.objects.create_user(username='other_creator', email='other@example.com', password='test1234')
		self.client.force_login(self.user)
		self.outline = CourseOutline.objects.create(
			user=self.user,
			title='总线讲解课',
			description='AI 生成课件',
			outline_data=json.dumps({'title': '总线讲解课'}, ensure_ascii=False),
			status='completed',
			progress=100,
		)
		Slide.objects.create(course_outline=self.outline, chapter_id='ppt_main', slide_data='[]')
		Animation.objects.create(course_outline=self.outline, chapter_id='chapter_1', concept_name='总线仲裁', animation_code='<html></html>')
		OutlineExport.objects.create(course_outline=self.outline, user=self.user, filename='demo.pptx', status='completed')

	@patch('curriculum_app.views._schedule_outline_generation', return_value=None)
	@patch('agent_system.planner_agent.PlannerAgent.generate_outline', return_value={
		'title': '梯度下降', 'objectives': ['理解梯度下降的原理'],
		'chapters': [
			{'number': 1, 'title': '梯度下降的直觉', 'teaching_goal': '理解为什么要沿梯度下降',
			 'key_points': ['损失函数', '学习率']},
			{'number': 2, 'title': '批量与随机梯度下降', 'teaching_goal': '区分 BGD/SGD',
			 'key_points': ['批量', '随机', '小批量']},
		],
	})
	def test_generate_course_normalizes_natural_language_topic(self, planner_mock, schedule_mock):
		response = self.client.post(reverse('generate_course'), {'topic': '我想学习梯度下降'})

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertTrue(payload['success'])
		schedule_mock.assert_called_once()
		outline = CourseOutline.objects.get(pk=payload['outline_id'])
		self.assertEqual(outline.title, '梯度下降')
		outline_data = json.loads(outline.outline_data)
		self.assertEqual(outline_data.get('title'), '梯度下降')
		# 大纲由 AI(PlannerAgent) 生成，不再是通用模板
		blueprint = outline_data.get('blueprint') or {}
		self.assertEqual(blueprint.get('outline_source'), 'ai')
		titles = [c.get('title') for c in blueprint.get('chapters', [])]
		self.assertEqual(titles[0], '第1章 梯度下降的直觉')
		self.assertNotIn('第1章 课程导入与全景理解', titles)

	def test_owner_can_delete_generated_course_outline_and_related_records(self):
		response = self.client.post(reverse('delete_course_outline', args=[self.outline.id]))

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertTrue(payload['success'])
		self.assertFalse(CourseOutline.objects.filter(id=self.outline.id).exists())
		self.assertFalse(Slide.objects.filter(course_outline_id=self.outline.id).exists())
		self.assertFalse(Animation.objects.filter(course_outline_id=self.outline.id).exists())
		self.assertFalse(OutlineExport.objects.filter(course_outline_id=self.outline.id).exists())

	def test_user_cannot_delete_other_users_generated_course_outline(self):
		other_outline = CourseOutline.objects.create(
			user=self.other_user,
			title='别人的课件',
			outline_data='{}',
			status='completed',
			progress=100,
		)

		response = self.client.post(reverse('delete_course_outline', args=[other_outline.id]))

		self.assertEqual(response.status_code, 403)
		self.assertTrue(CourseOutline.objects.filter(id=other_outline.id).exists())

	def test_owner_can_bulk_delete_generated_course_outlines(self):
		second_outline = CourseOutline.objects.create(
			user=self.user,
			title='流水线讲解课',
			outline_data='{}',
			status='completed',
			progress=100,
		)
		Slide.objects.create(course_outline=second_outline, chapter_id='ppt_main', slide_data='[]')

		response = self.client.post(
			reverse('bulk_delete_course_outlines'),
			data=json.dumps({'outline_ids': [self.outline.id, second_outline.id]}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertTrue(payload['success'])
		self.assertEqual(payload['deleted_count'], 2)
		self.assertCountEqual(payload['deleted_ids'], [self.outline.id, second_outline.id])
		self.assertFalse(CourseOutline.objects.filter(id__in=[self.outline.id, second_outline.id]).exists())
		self.assertFalse(Slide.objects.filter(course_outline_id=second_outline.id).exists())

	def test_bulk_delete_skips_other_users_generated_course_outlines(self):
		other_outline = CourseOutline.objects.create(
			user=self.other_user,
			title='别人的课件',
			outline_data='{}',
			status='completed',
			progress=100,
		)

		response = self.client.post(
			reverse('bulk_delete_course_outlines'),
			data=json.dumps({'outline_ids': [self.outline.id, other_outline.id]}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertTrue(payload['success'])
		self.assertEqual(payload['deleted_count'], 1)
		self.assertEqual(payload['deleted_ids'], [self.outline.id])
		self.assertEqual(payload['skipped_ids'], [other_outline.id])
		self.assertFalse(CourseOutline.objects.filter(id=self.outline.id).exists())
		self.assertTrue(CourseOutline.objects.filter(id=other_outline.id).exists())


class PersonalizedLearningPlanTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='plan_user', email='plan_user@example.com', password='test1234')
		self.client.force_login(self.user)
		ProfileAppStudentProfile.objects.create(
			user=self.user,
			course_id='default',
			profile_data=json.dumps({
				'knowledge_profile': {'梯度下降': '初级'},
				'cognitive_style': '视觉型',
				'learning_goals': ['理解梯度下降如何更新参数'],
				'misconceptions': ['总把梯度方向和更新方向搞反'],
				'engagement': {'score': 82, 'notes': '近期参与度较高'},
				'learning_preferences': {'preferred_mode': '图示 + 练习'},
			}, ensure_ascii=False),
			confidence_scores='{}',
		)
		self.course = Course.objects.create(
			owner=self.user,
			title='机器学习中的梯度下降',
			summary='从损失函数到参数更新的优化入门课程',
			status='published',
			visibility='login',
		)
		self.material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='梯度下降讲义',
			material_type='pdf',
			file=SimpleUploadedFile('gradient.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
		)
		MaterialChunk.objects.create(
			material=self.material,
			chunk_index=0,
			source_page='1',
			heading='梯度下降',
			keyword_summary='损失函数, 学习率, 参数更新',
			content='梯度下降通过计算损失函数对参数的梯度，并沿负梯度方向更新参数。',
		)
		self.second_material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='优化算法补充资料',
			material_type='pdf',
			file=SimpleUploadedFile('optimizer.pdf', b'fake-pdf-content-2', content_type='application/pdf'),
			processing_status='ready',
		)
		MaterialChunk.objects.create(
			material=self.second_material,
			chunk_index=0,
			source_page='7',
			heading='损失函数与梯度方向',
			keyword_summary='损失函数, 梯度方向, 负梯度',
			content='本节重点解释损失函数如何描述误差，以及梯度方向与负梯度更新之间的关系。',
		)
		self.outline = CourseOutline.objects.create(
			user=self.user,
			title='梯度下降课件',
			outline_data='{}',
			status='completed',
			progress=100,
		)
		LearningProgress.objects.create(
			user=self.user,
			course_outline=self.outline,
			chapter_id='chapter_1',
			status='in_progress',
			completed_slides=3,
			total_slides=8,
			quiz_score=78,
		)
		self.weak_stat = MaterialQuestionStat.objects.create(
			user=self.user,
			course=self.course,
			material=self.second_material,
			question_fingerprint='gradient-fp-1',
			question_text='为什么更新方向不是梯度方向？',
			knowledge_tag='梯度方向',
			source_page='7',
			source_heading='损失函数与梯度方向',
			wrong_count=4,
			consecutive_wrong_count=3,
		)

	def test_refresh_does_not_stack_remediation_stage_or_title(self):
		# 反复刷新不应叠加"阶段0：先补当前薄弱点"，标题也不应叠加"（已按当前状态调整）"
		from curriculum_app.views import _build_refreshed_learning_plan
		plan = LearningPlan.objects.create(
			user=self.user, title='梯度下降（已按当前状态调整）', status='generated',
			plan_data=json.dumps({
				'title': '梯度下降（已按当前状态调整）', 'plan_source': 'llm', 'modules': [
					{'name': '阶段0：先补当前薄弱点', 'estimated_hours': 1.5, 'focus': 'x',
					 'lessons': [{'title': 'a', 'objectives': '', 'resources': ['doc']}]},
					{'name': '阶段一：主线', 'estimated_hours': 3, 'focus': 'y',
					 'lessons': [{'title': 'b', 'objectives': '', 'resources': ['doc']}]},
				]}, ensure_ascii=False))
		refreshed = _build_refreshed_learning_plan(plan, self.user)
		names = [m['name'] for m in refreshed['modules']]
		self.assertLessEqual(sum(1 for n in names if n.startswith('阶段0')), 1)  # 不叠加
		self.assertEqual(refreshed['title'].count('（已按当前状态调整）'), 1)      # 后缀不叠加

	def test_sanitize_plan_modules_filters_invalid(self):
		from curriculum_app.views import _sanitize_plan_modules
		good = [
			{'name': '阶段一', 'estimated_hours': 3, 'focus': 'f',
			 'lessons': [{'title': '单元1', 'objectives': 'o', 'resources': ['doc', 'xxx', 'quiz']}]},
			{'name': '阶段二', 'lessons': [{'title': '单元2', 'resources': []}]},
		]
		out = _sanitize_plan_modules(good)
		self.assertEqual(len(out), 2)
		self.assertEqual(out[0]['lessons'][0]['resources'], ['doc', 'quiz'])   # 非法资源被过滤
		self.assertEqual(out[1]['lessons'][0]['resources'], ['doc'])           # 空资源→默认 doc
		self.assertIsNone(_sanitize_plan_modules('not a list'))
		self.assertIsNone(_sanitize_plan_modules([{'name': '只有一个阶段无单元', 'lessons': []}]))

	def test_plan_falls_back_to_template_when_llm_off(self):
		from curriculum_app.views import _build_personalized_learning_plan
		plan = _build_personalized_learning_plan('梯度下降', self.user, use_llm=False)
		self.assertEqual(plan['plan_source'], 'template')
		self.assertEqual(len(plan['modules']), 3)

	@patch('core.xunfei_spark.spark_client')
	def test_plan_uses_llm_modules_when_available(self, mock_client):
		mock_client.get_response.return_value = json.dumps({'modules': [
			{'name': '先补梯度方向', 'estimated_hours': 2, 'focus': '纠正方向混淆',
			 'lessons': [{'title': '梯度 vs 更新方向', 'objectives': '能说清负梯度', 'resources': ['doc', 'animation']}]},
			{'name': '练习巩固', 'estimated_hours': 2, 'focus': '练',
			 'lessons': [{'title': '专项练习', 'objectives': '做对', 'resources': ['quiz']}]},
		]}, ensure_ascii=False)
		from curriculum_app.views import _build_personalized_learning_plan
		plan = _build_personalized_learning_plan('梯度下降', self.user, use_llm=True)
		self.assertEqual(plan['plan_source'], 'llm')
		self.assertIn('先补梯度方向', [m['name'] for m in plan['modules']])

	@patch('core.xunfei_spark.spark_client', None)  # 关掉 LLM 编排走模板，测试保持确定、离线、快
	def test_tutor_generate_roadmap_uses_profile_and_course_context(self):
		response = self.client.post(
			reverse('tutor_generate'),
			data=json.dumps({'query': '请给我一份梯度下降学习路线'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertTrue(payload['success'])
		self.assertEqual(payload['type'], 'roadmap')
		plan = payload['plan_data']
		self.assertEqual(plan['profile_summary']['source'], 'merged')
		self.assertEqual(plan['profile_summary']['preferred_mode'], '图示 + 练习')
		self.assertEqual(plan['matched_course']['title'], '机器学习中的梯度下降')
		self.assertIn('总把梯度方向和更新方向搞反', ''.join(plan['recommendation_reason']))
		self.assertGreaterEqual(len(plan['modules']), 3)
		event = ProfileEvent.objects.filter(user=self.user, event_type='learning_plan_generated').latest('id')
		self.assertIsNotNone(event.processed_at)
		self.assertEqual(event.payload['plan_id'], payload['plan_id'])
		profile = StudentProfile.objects.get(user=self.user)
		self.assertIn('请给我一份梯度下降学习路线', profile.learning_goals)

	def test_course_study_view_records_profile_event(self):
		response = self.client.get(reverse('course_study', args=[self.course.id]) + f'?material={self.second_material.id}&page=7')

		self.assertEqual(response.status_code, 200)
		event = ProfileEvent.objects.filter(user=self.user, event_type='course_material_viewed').latest('id')
		self.assertIsNotNone(event.processed_at)
		self.assertEqual(event.course_id, self.course.id)
		self.assertEqual(event.material_id, self.second_material.id)
		profile = StudentProfile.objects.get(user=self.user)
		self.assertEqual(profile.engagement.get('last_material_id'), self.second_material.id)

	def test_learning_progress_view_exposes_recent_plans(self):
		LearningPlan.objects.create(
			user=self.user,
			title='梯度下降学习路线',
			plan_data=json.dumps({'title': '梯度下降学习路线', 'modules': [{'name': '阶段一', 'lessons': []}]}, ensure_ascii=False),
			status='generated',
		)

		response = self.client.get(reverse('learning_progress'))

		self.assertEqual(response.status_code, 200)
		self.assertIn('recent_plans', response.context)
		self.assertEqual(len(response.context['recent_plans']), 1)
		self.assertEqual(response.context['recent_plans'][0]['data']['title'], '梯度下降学习路线')

	def test_learning_plan_detail_view_renders_modules(self):
		plan = LearningPlan.objects.create(
			user=self.user,
			title='梯度下降学习路线',
			plan_data=json.dumps({
				'title': '梯度下降学习路线',
				'matched_course': {
					'id': self.course.id,
					'title': self.course.title,
					'summary': self.course.summary,
					'topics': ['损失函数'],
				},
				'recent_progress': [
					{
						'outline_id': self.outline.id,
						'outline_title': self.outline.title,
						'chapter_id': 'chapter_1',
						'status': 'in_progress',
						'quiz_score': 78,
					},
				],
				'recommendation_reason': ['基于近期画像和课程资料生成'],
				'modules': [
					{
						'name': '阶段一：先理解梯度和损失函数',
						'focus': '先把优化目标和更新方向搞清楚',
						'lessons': [
							{
								'title': '损失函数与梯度',
								'objectives': '理解为什么梯度能指示变化最快方向',
								'resources': ['doc', 'quiz', 'animation'],
							}
						],
					}
				],
			}, ensure_ascii=False),
			status='generated',
		)

		response = self.client.get(reverse('learning_plan_detail', args=[plan.id]))

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context['plan'].id, plan.id)
		self.assertEqual(len(response.context['modules']), 1)
		lesson_context = response.context['modules'][0]['lessons'][0]
		self.assertEqual(lesson_context['anchor_match']['material_id'], self.second_material.id)
		self.assertEqual(lesson_context['anchor_match']['source_page'], '7')
		self.assertContains(response, '阶段一：先理解梯度和损失函数')
		self.assertContains(response, reverse('course_study', args=[self.course.id]))
		self.assertContains(response, f'material={self.second_material.id}')
		self.assertContains(response, '&amp;page=7')
		self.assertContains(response, '#studyQuizPanelShell')
		self.assertContains(response, f"{reverse('course_ai_chat', args=[self.course.id])}?material={self.second_material.id}&amp;current_page=7")
		self.assertContains(response, reverse('course_outline', args=[self.outline.id]))
		self.assertContains(response, '开始练习')
		self.assertContains(response, '推荐定位：优化算法补充资料 / 第7页 / 损失函数与梯度方向')
		self.assertContains(response, '按当前薄弱点重排')

	def test_refresh_learning_plan_creates_adjusted_plan(self):
		plan = LearningPlan.objects.create(
			user=self.user,
			title='梯度下降学习路线',
			plan_data=json.dumps({'title': '梯度下降学习路线', 'modules': [{'name': '阶段一', 'lessons': []}]}, ensure_ascii=False),
			status='generated',
		)

		response = self.client.post(reverse('refresh_learning_plan', args=[plan.id]))

		self.assertEqual(response.status_code, 302)
		self.assertEqual(LearningPlan.objects.filter(user=self.user).count(), 2)
		new_plan = LearningPlan.objects.filter(user=self.user).order_by('-id').first()
		self.assertNotEqual(new_plan.id, plan.id)
		payload = json.loads(new_plan.plan_data)
		self.assertIn('已按当前状态调整', new_plan.title)
		self.assertEqual(payload['adjustment_meta']['source_plan_id'], plan.id)
		self.assertEqual(payload['weak_areas'][0], '梯度方向')
		self.assertIn('检测到当前薄弱点：梯度方向', ''.join(payload['recommendation_reason']))
		self.assertEqual(payload['modules'][0]['name'], '阶段0：先补当前薄弱点')
		event = ProfileEvent.objects.filter(user=self.user, event_type='learning_plan_refreshed').latest('id')
		self.assertEqual(event.payload['plan_id'], new_plan.id)
		self.assertIsNotNone(event.processed_at)

	def test_course_ai_chat_view_exposes_latest_adjusted_plan(self):
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
						'focus': '先把梯度方向这个薄弱点补齐。',
						'lessons': [{'title': '回看梯度方向', 'objectives': '重新理解梯度和负梯度的区别'}],
					}
				],
				'adjustment_meta': {'adjustment_type': 'weak-area-prioritized'},
			}, ensure_ascii=False),
			status='generated',
		)

		response = self.client.get(reverse('course_ai_chat', args=[self.course.id]) + f'?material={self.second_material.id}&current_page=7')

		self.assertEqual(response.status_code, 200)
		self.assertIn('latest_plan', response.context)
		self.assertIn('course_ai_focus', response.context)
		self.assertEqual(response.context['latest_plan']['weak_areas'][0], '梯度方向')
		self.assertEqual(response.context['course_ai_focus']['current_page'], '7')
		self.assertIn('梯度方向', response.context['course_ai_focus']['suggested_action'])
		self.assertTrue(response.context['course_ai_focus']['evidence_points'])
		self.assertEqual(response.context['course_ai_focus']['evidence_points'][0]['label'], '当前阶段')
		self.assertContains(response, '当前学习路径已同步到课程 AI')
		self.assertContains(response, '阶段0：先补当前薄弱点')
		self.assertContains(response, '当前个性化建议')
		self.assertContains(response, '直接问当前最优先动作')
		event = ProfileEvent.objects.filter(user=self.user, event_type='course_ai_opened').latest('id')
		self.assertEqual(event.course_id, self.course.id)
		self.assertEqual(event.material_id, self.second_material.id)
		self.assertIsNotNone(event.processed_at)

	def test_course_ai_chat_view_without_learning_plan_uses_material_context_only(self):
		LearningPlan.objects.filter(user=self.user).delete()

		response = self.client.get(reverse('course_ai_chat', args=[self.course.id]) + f'?material={self.second_material.id}&current_page=7')

		self.assertEqual(response.status_code, 200)
		focus = response.context['course_ai_focus']
		self.assertEqual(focus['top_module_name'], '')
		self.assertNotIn('当前优先阶段待生成', focus['suggested_action'])
		self.assertEqual(focus['suggested_question'], '请先结合当前资料，帮我判断这门课最值得优先看的部分。')
		self.assertTrue(focus['evidence_points'])
		self.assertEqual(focus['evidence_points'][0]['label'], '当前资料')

class TeacherCourseMaterialManagementTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='teacher1', email='teacher1@example.com', password='test1234')
		self.client.force_login(self.user)
		self.course = Course.objects.create(
			owner=self.user,
			title='线性代数',
			status='draft',
			visibility='login',
		)
		self.material = CourseMaterial.objects.create(
			course=self.course,
			uploaded_by=self.user,
			title='矩阵讲义',
			material_type='pdf',
			file=SimpleUploadedFile('matrix.pdf', b'fake-pdf-content', content_type='application/pdf'),
			processing_status='ready',
		)

	def test_teacher_can_delete_course_material(self):
		url = reverse('teacher_course_detail', args=[self.course.id])
		response = self.client.post(url, {
			'action': 'delete_material',
			'material_id': self.material.id,
		})

		self.assertEqual(response.status_code, 302)
		self.assertFalse(CourseMaterial.objects.filter(id=self.material.id).exists())

	@patch('curriculum_app.views.enqueue_course_material_processing')
	def test_teacher_can_reprocess_course_material(self, enqueue_mock):
		class DummyTask:
			id = 99
			output_data = {'launch_mode': 'subprocess'}

		enqueue_mock.return_value = DummyTask()
		self.material.processing_status = 'failed'
		self.material.metadata = {
			'parse_error': 'mock error',
			'chunk_count': 4,
			'agent_task_id': 12,
		}
		self.material.save(update_fields=['processing_status', 'metadata', 'updated_at'])

		url = reverse('teacher_course_detail', args=[self.course.id])
		response = self.client.post(url, {
			'action': 'reprocess_material',
			'material_id': self.material.id,
		})

		self.assertEqual(response.status_code, 302)
		self.material.refresh_from_db()
		enqueue_mock.assert_called_once()
		self.assertEqual(self.material.processing_status, 'pending')
		self.assertNotIn('parse_error', self.material.metadata)
		self.assertNotIn('chunk_count', self.material.metadata)


class MaterialParsingSanitizationTests(TestCase):
	def test_compute_embedding_ignores_invalid_surrogates(self):
		text = '向量空间\ud835矩阵'
		vector = compute_embedding(text)

		self.assertEqual(len(vector), 64)
		self.assertTrue(all(isinstance(value, float) for value in vector))


class SeedDemoCourseCommandTests(TestCase):
	"""种子命令：一键灌入可复现的示例课程 + 知识库(带 embedding)。"""

	def test_seed_creates_course_with_embedded_chunks(self):
		from django.core.management import call_command
		call_command('seed_demo_course', '--user', 'seed_test_u', verbosity=0)
		course = Course.objects.get(title='线性代数导论', owner__username='seed_test_u')
		chunks = MaterialChunk.objects.filter(material__course=course)
		self.assertEqual(chunks.count(), 8)
		self.assertTrue(all(len(c.embedding) == 64 for c in chunks))  # 均带 64 维向量
		self.assertTrue(all(c.keyword_summary for c in chunks))

	def test_seed_is_idempotent(self):
		from django.core.management import call_command
		call_command('seed_demo_course', '--user', 'seed_test_u2', verbosity=0)
		call_command('seed_demo_course', '--user', 'seed_test_u2', verbosity=0)  # 再跑一次不应重复建
		self.assertEqual(Course.objects.filter(title='线性代数导论', owner__username='seed_test_u2').count(), 1)
