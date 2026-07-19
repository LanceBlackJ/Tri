#!/usr/bin/env python
import os
import sys
import django
import json
import traceback

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'teacher_django.settings')
django.setup()

from django.contrib.auth import get_user_model
from agent_system.agents import orchestrate_generate_resources
from agent_system.models import AgentTask

User = get_user_model()
user = User.objects.filter(username='demo_test').first()
if not user:
    user = User.objects.create_user(username='demo_test', email='demo_test@example.com', password='TestPass123')
    print('Created user:', user.username)
else:
    print('Using existing user:', user.username)

# 创建 AgentTask
task = AgentTask.objects.create(user=user, name='test_orch_task', input_data={'topic': '测试课程：线性代数入门'}, status='pending')
print('Created AgentTask id:', task.id)

try:
    results = orchestrate_generate_resources(user, topic='测试课程：线性代数入门', resource_types=None, task=task)
    print('orchestrate_generate_resources returned:')
    print(json.dumps(results, ensure_ascii=False, indent=2))
except Exception as e:
    print('Exception during orchestrate_generate_resources:')
    print(str(e))
    traceback.print_exc()

# reload task and print output_data
try:
    t = AgentTask.objects.get(pk=task.id)
    print('Task status:', t.status)
    print('Task progress:', t.progress)
    print('Task output_data:')
    print(json.dumps(t.output_data, ensure_ascii=False, indent=2))
except Exception:
    traceback.print_exc()
