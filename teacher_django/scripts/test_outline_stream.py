#!/usr/bin/env python
import os
import sys
import django
import json
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'teacher_django.settings')
django.setup()

from django.contrib.auth import get_user_model
from curriculum_app.models import CourseOutline
from agent_system.models import AgentTask
from agent_system.agents import orchestrate_generate_resources

User = get_user_model()
user = User.objects.filter(username='demo_test').first()
if not user:
    user = User.objects.create_user(username='demo_test', email='demo_test@example.com', password='TestPass123')

outline = CourseOutline.objects.create(user=user, title='测试流式大纲', description='用于测试流式更新', outline_data=json.dumps({}), status='generating', progress=0)
print('Created outline id', outline.id)

task = AgentTask.objects.create(user=user, name='test_outline_task', input_data={'outline_id': outline.id, 'topic': outline.title}, status='pending')
print('Created AgentTask id', task.id)

# run orchestrator in background
def run_orch():
    try:
        orchestrate_generate_resources(user, topic=outline.title, resource_types=None, task=task)
    except Exception as e:
        print('orchestrator error', e)

t = threading.Thread(target=run_orch, daemon=True)
t.start()

# poll CourseOutline and print events
last = None
start = time.time()
while True:
    outline.refresh_from_db()
    cur = {'progress': outline.progress, 'status': outline.status, 'outline_data': outline.outline_data}
    if cur != last:
        print('EVENT', json.dumps(cur, ensure_ascii=False, indent=2))
        last = cur
    if outline.status in ('completed', 'failed'):
        break
    if time.time() - start > 300:
        print('timeout')
        break
    time.sleep(1)

print('done')
