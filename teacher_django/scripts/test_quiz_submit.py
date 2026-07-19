#!/usr/bin/env python
import os
import sys
import django
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'teacher_django.settings')
django.setup()

from django.test import Client
from django.contrib.auth import get_user_model
from curriculum_app.models import CourseOutline, LearningProgress

User = get_user_model()
user = User.objects.filter(username='demo_test').first()
if not user:
    print('demo_test not found')
    sys.exit(1)

outline = CourseOutline.objects.filter(user=user).order_by('-created_at').first()
if not outline:
    print('no outline')
    sys.exit(1)

# parse quiz
try:
    od = json.loads(outline.outline_data)
except Exception:
    print('cannot parse outline_data')
    od = {}

quiz = (od.get('resources') or {}).get('quiz') or {}
preview = quiz.get('preview') or ''
try:
    qobj = json.loads(preview) if isinstance(preview, str) and preview.strip().startswith('{') else preview
except Exception:
    qobj = preview

questions = []
if isinstance(qobj, dict) and isinstance(qobj.get('questions'), list):
    questions = qobj.get('questions')
elif isinstance(qobj, list):
    questions = qobj
else:
    print('no questions found')
    sys.exit(1)

answers = {}
for i, q in enumerate(questions):
    key = f'q{i}'
    # try to find correct answer
    ans = None
    for k in ('answer', 'correct_answer', 'answer_text', 'correct'):
        if k in q:
            ans = q.get(k)
            break
    # if choices have 'correct' flag, pick it
    if ans is None and 'choices' in q and isinstance(q['choices'], list):
        for opt in q['choices']:
            if isinstance(opt, dict) and opt.get('correct'):
                ans = opt.get('value') or opt.get('label') or opt.get('text') or opt
                break
    if ans is None and 'choices' in q and len(q['choices'])>0:
        # fallback choose first
        o = q['choices'][0]
        ans = (o.get('value') if isinstance(o, dict) else o)
    answers[key] = ans

c = Client()
logged_in = c.login(username='demo_test', password='TestPass123')
print('login', logged_in)
url = f'/curriculum/outline/{outline.id}/quiz/submit/'
resp = c.post(url, json.dumps({'answers': answers}), content_type='application/json', HTTP_HOST='localhost')
print('status', resp.status_code)
try:
    print(resp.json())
except Exception:
    print(resp.content)

# check LearningProgress
lp = LearningProgress.objects.filter(user=user, course_outline=outline).order_by('-created_at').first()
print('LearningProgress:', lp and {'chapter_id': lp.chapter_id, 'quiz_score': lp.quiz_score, 'status': lp.status} or None)
