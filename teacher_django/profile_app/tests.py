import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from agent_system.models import ProfileEvent, StudentProfile
from core.models import User

from .growth_report import (
	_compute_deltas,
	_compute_knowledge_deltas,
	_compute_profile_hash,
	_fallback_narrative,
	_truncate_narrative,
)
from .models import ProfileConversationSession, ProfileSnapshot
from .models import StudentProfile as ProfileAppStudentProfile


class ProfileBuildingEventTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='profile-user', email='profile@example.com', password='pass12345')
		self.client.login(username='profile-user', password='pass12345')
		self.session = ProfileConversationSession.objects.create(
			user=self.user,
			asked_dimensions=json.dumps(['motivation']),
			answered_dimensions='[]',
			skipped_dimensions='[]',
			conversation_history='[]',
			status='active',
		)

	def test_profile_building_answer_records_profile_event(self):
		response = self.client.post(
			reverse('profile_building_step'),
			data=json.dumps({
				'sessionId': self.session.id,
				'action': 'answer',
				'message': '我想系统学习机器学习，每周能学6小时，喜欢通过项目实践。',
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		event = ProfileEvent.objects.get(user=self.user, event_type='profile_building_answer')
		self.assertIsNotNone(event.processed_at)
		self.assertEqual(event.payload['dimension'], 'motivation')
		profile = StudentProfile.objects.get(user=self.user)
		self.assertIn('系统学习机器学习，每周能学6小时，喜欢通过项目实践', profile.learning_goals)
		self.assertEqual(profile.learning_preferences.get('preferred_mode'), '实践')
		self.assertEqual(profile.engagement.get('weekly_hours'), 6)


PROFILE_V1 = {
	'knowledge_profile': {'Python基础': 0.5, 'Web开发': 0.3},
	'learning_goals': ['学会Django'],
	'misconceptions': ['对装饰器有误解'],
	'engagement': {'score': 60},
	'learning_preferences': {
		'online_learning': True,
		'practical_application': False,
		'self_reflection': True,
	},
}
CONFIDENCE_V1 = {'knowledge': 0.6, 'engagement': 0.5}

PROFILE_V2 = {
	'knowledge_profile': {'Python基础': 0.8, 'Web开发': 0.3},
	'learning_goals': ['学会Django'],
	'misconceptions': [],
	'engagement': {'score': 40},
	'learning_preferences': {
		'online_learning': True,
		'practical_application': False,
		'self_reflection': True,
	},
}
CONFIDENCE_V2 = CONFIDENCE_V1


class GrowthReportTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='growth-user', email='growth@example.com', password='pass12345')
		self.client.login(username='growth-user', password='pass12345')

	def _set_profile(self, profile_data, confidence_scores):
		ProfileAppStudentProfile.objects.update_or_create(
			user=self.user,
			course_id='default',
			defaults={
				'profile_data': json.dumps(profile_data, ensure_ascii=False),
				'confidence_scores': json.dumps(confidence_scores, ensure_ascii=False),
			},
		)

	def test_first_visit_creates_one_snapshot_with_no_narrative(self):
		self._set_profile(PROFILE_V1, CONFIDENCE_V1)

		response = self.client.get(reverse('profile_dashboard'))

		self.assertEqual(response.status_code, 200)
		self.assertFalse(response.context['has_growth_history'])
		snapshots = ProfileSnapshot.objects.filter(user=self.user)
		self.assertEqual(snapshots.count(), 1)
		self.assertEqual(snapshots.first().ai_narrative, '')

	def test_second_visit_with_changed_profile_creates_second_snapshot_and_narrative(self):
		self._set_profile(PROFILE_V1, CONFIDENCE_V1)
		self.client.get(reverse('profile_dashboard'))

		self._set_profile(PROFILE_V2, CONFIDENCE_V2)

		with patch('profile_app.growth_report.spark_client') as mock_client:
			mock_client.get_response.return_value = (
				'知识掌握和概念清晰度进步明显，学习参与度有所下降，建议多安排固定学习时间。'
			)
			response = self.client.get(reverse('profile_dashboard'))

		self.assertEqual(ProfileSnapshot.objects.filter(user=self.user).count(), 2)
		self.assertTrue(response.context['has_growth_history'])

		deltas = response.context['growth_deltas']
		self.assertEqual(len(deltas), 6)
		for delta in deltas:
			self.assertIn('label', delta)
			self.assertIn('latest', delta)
			self.assertIn('previous', delta)
			self.assertIn('delta', delta)
			self.assertIn('direction', delta)

		self.assertIn('previous_values', response.context['radar_chart_data'])
		self.assertEqual(
			response.context['growth_narrative'],
			'知识掌握和概念清晰度进步明显，学习参与度有所下降，建议多安排固定学习时间。',
		)

	def test_second_visit_with_unchanged_profile_does_not_create_duplicate_snapshot(self):
		self._set_profile(PROFILE_V1, CONFIDENCE_V1)
		self.client.get(reverse('profile_dashboard'))

		response = self.client.get(reverse('profile_dashboard'))

		self.assertEqual(ProfileSnapshot.objects.filter(user=self.user).count(), 1)
		self.assertFalse(response.context['has_growth_history'])

	def test_ai_narrative_fallback_when_spark_client_none(self):
		self._set_profile(PROFILE_V1, CONFIDENCE_V1)
		self.client.get(reverse('profile_dashboard'))

		self._set_profile(PROFILE_V2, CONFIDENCE_V2)

		with patch('profile_app.growth_report.spark_client', None):
			response = self.client.get(reverse('profile_dashboard'))

		self.assertTrue(response.context['has_growth_history'])
		deltas = response.context['growth_deltas']
		sorted_deltas = sorted(deltas, key=lambda d: d['delta'])
		expected = _fallback_narrative(sorted_deltas[-1], sorted_deltas[0])

		narrative = response.context['growth_narrative']
		self.assertEqual(narrative, expected)
		latest_snapshot = ProfileSnapshot.objects.filter(user=self.user).latest('created_at')
		self.assertEqual(latest_snapshot.ai_narrative, narrative)

	def test_ai_narrative_fallback_when_spark_client_raises(self):
		self._set_profile(PROFILE_V1, CONFIDENCE_V1)
		self.client.get(reverse('profile_dashboard'))

		self._set_profile(PROFILE_V2, CONFIDENCE_V2)

		with patch('profile_app.growth_report.spark_client') as mock_client:
			mock_client.get_response.side_effect = Exception('boom')
			response = self.client.get(reverse('profile_dashboard'))

		self.assertTrue(response.context['has_growth_history'])
		narrative = response.context['growth_narrative']
		self.assertTrue(narrative)
		latest_snapshot = ProfileSnapshot.objects.filter(user=self.user).latest('created_at')
		self.assertEqual(latest_snapshot.ai_narrative, narrative)

	def test_compute_profile_hash_changes_with_content(self):
		profile_a = SimpleNamespace(profile_data='{"a": 1}', confidence_scores='{}')
		profile_b = SimpleNamespace(profile_data='{"a": 2}', confidence_scores='{}')
		profile_c = SimpleNamespace(profile_data='{"a": 1}', confidence_scores='{}')

		self.assertNotEqual(_compute_profile_hash(profile_a), _compute_profile_hash(profile_b))
		self.assertEqual(_compute_profile_hash(profile_a), _compute_profile_hash(profile_c))

	def test_compute_deltas_classifies_directions(self):
		labels = ['A', 'B', 'C', 'D', 'E']
		latest = [60, 40, 50, 51, 49]
		previous = [50, 50, 50, 50, 50]

		deltas = _compute_deltas(latest, previous, labels)
		directions = {d['label']: d['direction'] for d in deltas}

		self.assertEqual(directions['A'], 'up')
		self.assertEqual(directions['B'], 'down')
		self.assertEqual(directions['C'], 'flat')
		self.assertEqual(directions['D'], 'flat')
		self.assertEqual(directions['E'], 'flat')

	def test_fallback_narrative_when_all_flat_does_not_claim_decline(self):
		labels = ['A', 'B']
		latest = [50, 50.5]
		previous = [50, 50]

		deltas = _compute_deltas(latest, previous, labels)
		sorted_deltas = sorted(deltas, key=lambda d: d['delta'])
		narrative = _fallback_narrative(sorted_deltas[-1], sorted_deltas[0])

		self.assertNotIn('下滑', narrative)
		self.assertNotIn('进步明显', narrative)

	def test_truncate_narrative_keeps_short_text_unchanged(self):
		text = '最近知识掌握进步明显，继续保持！'
		self.assertEqual(_truncate_narrative(text), text)

	def test_truncate_narrative_cuts_at_sentence_boundary(self):
		text = '最近知识掌握进步明显。' + '建议每天多做几道相关练习巩固一下这个知识点。' * 5
		truncated = _truncate_narrative(text, limit=20)

		self.assertLessEqual(len(truncated), 20)
		self.assertTrue(truncated.endswith('。'))

	def test_compute_knowledge_deltas_returns_largest_changes_first(self):
		latest = SimpleNamespace(knowledge_snapshot=json.dumps({'A': 80.0, 'B': 40.0, 'C': 50.0}))
		previous = SimpleNamespace(knowledge_snapshot=json.dumps({'A': 50.0, 'B': 42.0, 'C': 50.0}))

		deltas = _compute_knowledge_deltas(latest, previous)

		self.assertEqual(len(deltas), 2)
		self.assertEqual(deltas[0]['tag'], 'A')
		self.assertEqual(deltas[0]['delta'], 30.0)
		self.assertEqual(deltas[1]['tag'], 'B')
		self.assertEqual(deltas[1]['delta'], -2.0)

	def test_growth_narrative_prompt_includes_specific_knowledge_point_deltas(self):
		self._set_profile(PROFILE_V1, CONFIDENCE_V1)
		self.client.get(reverse('profile_dashboard'))

		self._set_profile(PROFILE_V2, CONFIDENCE_V2)

		with patch('profile_app.growth_report.spark_client') as mock_client:
			mock_client.get_response.return_value = '反馈内容'
			self.client.get(reverse('profile_dashboard'))

		messages = mock_client.get_response.call_args[0][0]
		user_message = next(m['content'] for m in messages if m['role'] == 'user')
		self.assertIn('Python基础', user_message)
		self.assertIn('从50.0到80.0', user_message)

		system_message = next(m['content'] for m in messages if m['role'] == 'system')
		self.assertIn('知识点', system_message)


class ReviewQueueDashboardTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='review-user', email='review@example.com', password='pass12345')
		self.client.login(username='review-user', password='pass12345')

	def test_profile_dashboard_includes_review_queue_sorted_by_urgency(self):
		now = timezone.now()
		StudentProfile.objects.create(
			user=self.user,
			knowledge_profile={'A': 0.9, 'B': 0.9},
			knowledge_timestamps={
				'A': (now - timedelta(days=30)).isoformat(),
				'B': now.isoformat(),
			},
		)

		response = self.client.get(reverse('profile_dashboard'))

		review_queue = response.context['review_queue']
		by_tag = {item['tag']: item for item in review_queue}
		self.assertEqual(review_queue[0]['tag'], 'A')
		self.assertTrue(by_tag['A']['is_due'])
		self.assertFalse(by_tag['B']['is_due'])

	def test_profile_dashboard_review_queue_empty_without_agent_profile(self):
		response = self.client.get(reverse('profile_dashboard'))

		self.assertEqual(response.context['review_queue'], [])


class DialogueConversationalityTests(TestCase):
	"""对话构建要能真正对话：识别反问/求助并举例，而不是机械换题。"""

	def test_is_clarification_request(self):
		from .views import _is_clarification_request
		for t in ['你举个例子呗', '什么意思？', '不太懂', '能解释一下吗', '啥呀？']:
			self.assertTrue(_is_clarification_request(t), t)
		for t in ['我学过Python和线性代数', '我喜欢看视频学习', '每天学一小时']:
			self.assertFalse(_is_clarification_request(t), t)

	@patch('profile_app.views.spark_client', None)
	def test_user_asking_for_example_stays_and_clarifies(self):
		# 复现用户场景：问"你举个例子呗" -> 应停在当前维度举例，而不是跳到下一个维度
		from .views import generate_next_question
		conv = [
			{'role': 'assistant', 'content': '你有基础吗？', 'dim': 'knowledge_base', 'kind': 'question'},
			{'role': 'user', 'content': '有一定基础吧'},
			{'role': 'assistant', 'content': '具体学过啥？', 'dim': 'knowledge_base', 'kind': 'followup'},
			{'role': 'user', 'content': '你举个例子呗'},
		]
		msg, is_stay, target, kind = generate_next_question(
			conv, answered_dims=[], skipped_dims=[], asked_dims=['knowledge_base'])
		self.assertTrue(is_stay)
		self.assertEqual(kind, 'clarification')
		self.assertEqual(target, 'knowledge_base')  # 停在知识基础，不跳走
		self.assertTrue(msg.strip())

	@patch('profile_app.views.spark_client', None)
	def test_followup_still_works_on_later_dimension(self):
		# 旧计数器把整段对话都算进来，导致第一个维度之后再也不追问；这里验证后面维度仍能追问
		from .views import generate_next_question
		conv = [
			{'role': 'assistant', 'content': 'Q1', 'dim': 'knowledge_base', 'kind': 'question'},
			{'role': 'user', 'content': '嗯'},
			{'role': 'assistant', 'content': 'F1', 'dim': 'knowledge_base', 'kind': 'followup'},
			{'role': 'user', 'content': '学过Python和线性代数'},
			{'role': 'assistant', 'content': 'Q2', 'dim': 'cognitive_style', 'kind': 'question'},
			{'role': 'user', 'content': '还行'},  # 对认知风格的模糊回答
		]
		msg, is_stay, target, kind = generate_next_question(
			conv, answered_dims=['knowledge_base'], skipped_dims=[],
			asked_dims=['knowledge_base', 'cognitive_style'])
		self.assertTrue(is_stay)
		self.assertEqual(kind, 'followup')
		self.assertEqual(target, 'cognitive_style')

	@patch('profile_app.views.spark_client', None)
	def test_good_answer_advances_to_next_dimension(self):
		from .views import generate_next_question
		conv = [
			{'role': 'assistant', 'content': 'Q1', 'dim': 'knowledge_base', 'kind': 'question'},
			{'role': 'user', 'content': '我系统学过Python编程，也学过大学线性代数和概率论。'},
		]
		msg, is_stay, target, kind = generate_next_question(
			conv, answered_dims=[], skipped_dims=[], asked_dims=['knowledge_base'])
		self.assertFalse(is_stay)
		self.assertEqual(kind, 'question')
		self.assertNotEqual(target, 'knowledge_base')  # 推进到下一个维度


class DialogueKnowledgeSeedingTests(TestCase):
	"""对话构建的知识点作为冷启动初始值播种进权威画像 A（只补缺失、不覆盖做题数据）。"""

	def setUp(self):
		self.user = User.objects.create_user(username='seed-user', email='seed@example.com', password='pass12345')

	def test_dialogue_knowledge_seeds_missing_tags_as_conservative_prior(self):
		from .views import _bridge_dialogue_profile_to_agent

		profile_data = {'knowledge_profile': {'线性代数': '高级', 'Python': 85, '概率论': '入门'}}
		_bridge_dialogue_profile_to_agent(self.user, profile_data)

		ap = StudentProfile.objects.get(user=self.user)
		# 三个知识点都被播种，且是保守的 0-1 浮点先验（自我报告压到 <=0.6）
		self.assertIn('线性代数', ap.knowledge_profile)
		self.assertIn('Python', ap.knowledge_profile)
		self.assertLessEqual(ap.knowledge_profile['线性代数'], 0.6)
		self.assertLessEqual(ap.knowledge_profile['Python'], 0.6)
		self.assertLess(ap.knowledge_profile['概率论'], ap.knowledge_profile['线性代数'])
		# 播种时写入时间戳，供遗忘衰减/复习队列使用
		self.assertIn('线性代数', ap.knowledge_timestamps)

	def test_dialogue_knowledge_does_not_overwrite_existing_quiz_mastery(self):
		from .views import _bridge_dialogue_profile_to_agent

		# 模拟做题(BKT)已经给出的高掌握度
		StudentProfile.objects.create(user=self.user, knowledge_profile={'Python': 0.92})

		_bridge_dialogue_profile_to_agent(self.user, {'knowledge_profile': {'Python': '入门', '数据结构': '中级'}})

		ap = StudentProfile.objects.get(user=self.user)
		# 已有的做题掌握度不被对话自我报告覆盖
		self.assertEqual(ap.knowledge_profile['Python'], 0.92)
		# 缺失项照常播种
		self.assertIn('数据结构', ap.knowledge_profile)

	def test_schema_echo_is_stripped_from_parsed_knowledge(self):
		# 免费大模型偶尔把 JSON schema 原样回显，不能把 type/description 当成知识点
		from .views import _sanitize_parsed_profile, _bridge_dialogue_profile_to_agent

		parsed = _sanitize_parsed_profile({
			'knowledge_profile': {'type': 'object', 'description': '掌握程度（0-100）', 'Python': 80},
			'cognitive_style': '视觉型',
		})
		self.assertNotIn('type', parsed['knowledge_profile'])
		self.assertNotIn('description', parsed['knowledge_profile'])
		self.assertIn('Python', parsed['knowledge_profile'])

		# 桥接播种也不能把 schema 关键字写进权威画像 A
		_bridge_dialogue_profile_to_agent(self.user, parsed)
		ap = StudentProfile.objects.get(user=self.user)
		self.assertNotIn('type', ap.knowledge_profile)
		self.assertNotIn('description', ap.knowledge_profile)
		self.assertNotIn('overall', ap.knowledge_profile)
		self.assertIn('Python', ap.knowledge_profile)

	def test_seeded_knowledge_shows_on_dashboard_as_percentage(self):
		# 只有 A（含播种的知识点）、没有 B 对话画像，仪表盘也应把知识点格式化成百分比展示
		StudentProfile.objects.create(user=self.user, knowledge_profile={'线性代数': 0.4, '__internal__': 1})
		self.client.login(username='seed-user', password='pass12345')

		response = self.client.get(reverse('profile_dashboard'))

		items = dict(response.context['knowledge_items'])
		self.assertEqual(items.get('线性代数'), '40%')  # 浮点→百分比
		self.assertNotIn('__internal__', items)  # 内部键被过滤


class SpiresProfileExtractionTests(TestCase):
	"""SPIRES 式证据锚定画像抽取：schema 约束 + 每维 evidence/confidence + FSLSM 四轴。"""

	def setUp(self):
		self.user = User.objects.create_user(username='spires_u', email='spires_u@t.com', password='x', major='计算机科学与技术')
		self.conv = [
			{'role': 'assistant', 'content': '你哪些基础比较扎实？'},
			{'role': 'user', 'content': '数据结构学过，但指针和递归老出错。'},
			{'role': 'assistant', 'content': '为什么学？'},
			{'role': 'user', 'content': '主要为了考研。'},
		]
		self.fake_json = (
			'{"knowledge_profile":{"known_topics":{"数据结构":"入门"},"evidence":["数据结构学过，但指针和递归老出错。"],"confidence":0.8},'
			'"cognitive_style":{"summary":"偏动手实践","fslsm":{"active_reflective":"active","sensing_intuitive":"unknown","visual_verbal":"unknown","sequential_global":"unknown"},"evidence":["自己动手写代码"],"confidence":0.6},'
			'"learning_goals":{"goals":["考研"],"evidence":["主要为了考研。"],"confidence":0.7},'
			'"misconceptions":{"items":["指针和递归易错"],"evidence":["指针和递归老出错。"],"confidence":0.5},'
			'"engagement":{"score":50,"notes":"","evidence":[],"confidence":0.1},'
			'"learning_preferences":{"prefs":{},"evidence":[],"confidence":0.1}}'
		)

	def test_spires_extracts_flat_plus_meta_with_evidence_and_confidence(self):
		with patch('profile_app.views.spark_client') as mc:
			mc.get_response.return_value = self.fake_json
			from profile_app.views import _spires_extract_profile
			pd, conf = _spires_extract_profile(self.conv, major='计算机')
		# 扁平结构（下游桥接/雷达/播种沿用的形状）
		self.assertEqual(pd['knowledge_profile'], {'数据结构': '入门'})
		self.assertEqual(pd['cognitive_style'], '偏动手实践')
		self.assertEqual(pd['learning_goals'], ['考研'])
		self.assertEqual(pd['misconceptions'], ['指针和递归易错'])
		# _meta：证据 + 证据驱动置信度 + FSLSM
		self.assertEqual(conf['knowledge_profile'], 0.8)
		self.assertIn('数据结构学过', pd['_meta']['knowledge_profile']['evidence'][0])
		self.assertEqual(pd['_meta']['cognitive_style']['fslsm']['active_reflective'], 'active')
		# 没证据的维度置信度低（诚实、不编造）
		self.assertLessEqual(conf['engagement'], 0.2)

	def test_confidence_from_profile_prefers_meta(self):
		from profile_app.views import _confidence_from_profile
		parsed = {'knowledge_profile': {'x': 'y'}, '_meta': {'knowledge_profile': {'confidence': 0.42}}}
		self.assertEqual(_confidence_from_profile(parsed)['knowledge_profile'], 0.42)

	def test_dimension_evidence_builds_olm_display(self):
		from profile_app.views import _dimension_evidence
		parsed = {'_meta': {
			'knowledge_profile': {'confidence': 0.8, 'evidence': ['我数据结构学过']},
			'cognitive_style': {'confidence': 0.6, 'evidence': ['动手写代码'], 'fslsm': {'active_reflective': 'active'}},
			'engagement': {'confidence': 0.1, 'evidence': []},
		}}
		out = {d['label']: d for d in _dimension_evidence(parsed)}
		self.assertEqual(out['知识基础']['confidence_pct'], 80)
		self.assertIn('我数据结构学过', out['知识基础']['evidence'][0])
		self.assertTrue(out['学习参与']['weak'])  # 低置信 → 待补充
		self.assertTrue(any(a['pole'] for a in out['认知风格']['fslsm']))
