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
from curriculum_app.models import CourseOutline

User = get_user_model()
user = User.objects.filter(username='demo_test').first()
if not user:
    user = User.objects.create_user(username='demo_test', email='demo_test@example.com', password='TestPass123')

topic = 'C++'

# 创建 AgentTask
task = AgentTask.objects.create(user=user, name='test_cpp_task', input_data={'topic': topic}, status='pending')
print('Created AgentTask id:', task.id)

try:
    results = orchestrate_generate_resources(user, topic=topic, resource_types=None, task=task)
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

# print latest CourseOutline created for this user and topic
try:
    co = CourseOutline.objects.filter(user=user, title__icontains=topic).order_by('-id').first()
    if co:
        print('CourseOutline id:', co.id)
        print('status:', co.status)
        print('progress:', co.progress)
        print('outline_data:')
        print(co.outline_data)
    else:
        print('No CourseOutline found for topic')
except Exception:
    traceback.print_exc()
