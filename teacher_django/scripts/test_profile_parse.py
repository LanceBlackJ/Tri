#!/usr/bin/env python
import os
import django
import json
import sys

# 确保项目根路径在 sys.path 中（当直接执行脚本时）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'teacher_django.settings')
django.setup()

from core.models import User
from profile_app.models import ProfileConversationSession, StudentProfile
from profile_app.views import parse_profile_from_conversation

# 确保有一个测试用户
user = User.objects.filter(username='demo_test').first()
if not user:
    user = User.objects.create_user(username='demo_test', email='demo_test@example.com', password='TestPass123')
    print('Created test user:', user.username)
else:
    print('Using existing user:', user.username)

# 构造对话历史以便 parse_profile_from_conversation 使用启发式映射
asked_dims = ['knowledge_base', 'cognitive_style', 'learning_goals', 'error_patterns', 'engagement', 'learning_preferences']
conv = [
    {'role': 'assistant', 'content': '请描述一下您在该领域的基础知识水平？'},
    {'role': 'user', 'content': '掌握程度90，熟悉线性代数与概率。'},
    {'role': 'assistant', 'content': '您更喜欢什么样的学习方式？'},
    {'role': 'user', 'content': '主动学习，喜欢阅读与实操。'},
    {'role': 'assistant', 'content': '您的学习目标是什么？'},
    {'role': 'user', 'content': '好奇心驱动，想深入研究机器学习。'},
    {'role': 'assistant', 'content': '学习中常见的错误或困难？'},
    {'role': 'user', 'content': '一般没有明显错误。'},
    {'role': 'assistant', 'content': '请描述您的学习投入情况。'},
    {'role': 'user', 'content': '每天大约2小时。'},
    {'role': 'assistant', 'content': '您的学习偏好是什么？'},
    {'role': 'user', 'content': '在线学习，注重自我反思。'},
]

session = ProfileConversationSession.objects.create(
    user=user,
    course_id='default',
    asked_dimensions=json.dumps(asked_dims, ensure_ascii=False),
    answered_dimensions=json.dumps(asked_dims, ensure_ascii=False),
    skipped_dimensions=json.dumps([], ensure_ascii=False),
    conversation_history=json.dumps(conv, ensure_ascii=False),
)
print('Created session id:', session.id)

# 调用解析函数
profile_data, confidence = parse_profile_from_conversation(session)
print('Parsed profile_data:')
print(json.dumps(profile_data, ensure_ascii=False, indent=2))
print('Confidence:')
print(json.dumps(confidence, ensure_ascii=False, indent=2))

# 保存为 StudentProfile
sp, created = StudentProfile.objects.update_or_create(
    user=user,
    course_id='default',
    defaults={
        'profile_data': json.dumps(profile_data, ensure_ascii=False),
        'confidence_scores': json.dumps(confidence, ensure_ascii=False)
    }
)
print('Saved StudentProfile id:', sp.id, 'created:', created)
