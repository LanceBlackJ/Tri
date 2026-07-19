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
from agent_system.models import AgentTask
from agent_system.agents import orchestrate_generate_resources

User = get_user_model()
user = User.objects.filter(username='demo_test').first()
if not user:
    user = User.objects.create_user(username='demo_test', email='demo_test@example.com', password='TestPass123')

# create task
task = AgentTask.objects.create(user=user, name='simulate_stream_task', input_data={'topic': '测试：流式课程生成'}, status='pending')
print('Created task id', task.id)

# start background thread to run orchestrator
def run_orch():
    try:
        orchestrate_generate_resources(user, topic='测试：流式课程生成', resource_types=None, task=task)
    except Exception as e:
        print('orchestrator error', e)

t = threading.Thread(target=run_orch, daemon=True)
t.start()

# simulate SSE subscriber: poll task and print events
last_output = None
last_progress = None
start = time.time()
while True:
    task.refresh_from_db()
    output = task.output_data or {}
    progress = getattr(task, 'progress', 0)
    if output != last_output:
        prev_resources = (last_output or {}).get('resources') if isinstance(last_output, dict) else None
        curr_resources = output.get('resources') if isinstance(output, dict) else None
        new_resources = {}
        if curr_resources:
            if not prev_resources:
                new_resources = curr_resources
            else:
                for k, v in curr_resources.items():
                    if not prev_resources.get(k) or prev_resources.get(k) != v:
                        new_resources[k] = v
        if new_resources:
            print('EVENT resources', json.dumps(new_resources, ensure_ascii=False))
        else:
            print('EVENT output', json.dumps(output, ensure_ascii=False))
        last_output = output
    if progress != last_progress:
        print('EVENT progress', progress)
        last_progress = progress
    if task.status in ('done', 'failed'):
        print('EVENT completed', json.dumps({'status': task.status, 'output': output}, ensure_ascii=False))
        break
    if time.time() - start > 300:
        print('EVENT timeout')
        break
    time.sleep(1)

print('Subscriber exiting')
