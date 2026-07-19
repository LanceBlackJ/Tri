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

from django.contrib.auth import get_user_model
from curriculum_app.models import CourseOutline

User = get_user_model()
user = User.objects.filter(username='demo_test').first()
if not user:
    print('User demo_test not found')
    sys.exit(1)

outline = CourseOutline.objects.filter(user=user).order_by('-created_at').first()
if not outline:
    print('No CourseOutline found for demo_test')
    sys.exit(1)

print('CourseOutline id:', outline.id)
print('status:', outline.status)
print('progress:', outline.progress)
print('created_at:', outline.created_at.isoformat())
print('\n--- outline_data (JSON) ---')
try:
    od = json.loads(outline.outline_data)
    print(json.dumps(od, ensure_ascii=False, indent=2))
except Exception as e:
    print('Cannot parse outline_data as JSON:', str(e))
    print(outline.outline_data)
