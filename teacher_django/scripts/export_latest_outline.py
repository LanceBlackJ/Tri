#!/usr/bin/env python
import os
import sys
import django

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
    print('No outline found')
    sys.exit(1)

print('Using outline id', outline.id)

try:
    from curriculum_app.utils.pptx_exporter import export_outline_to_pptx
except Exception as e:
    print('Failed to import exporter:', e)
    sys.exit(1)

try:
    path, name = export_outline_to_pptx(outline)
    print('Exported to', path)
except Exception as e:
    print('Export failed:', str(e))
    sys.exit(1)
