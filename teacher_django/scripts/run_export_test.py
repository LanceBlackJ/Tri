#!/usr/bin/env python
import os
import sys
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'teacher_django.settings')
import django
django.setup()

from django.contrib.auth import get_user_model
from curriculum_app.models import CourseOutline
from curriculum_app.tasks import export_outline_task_sync
from django.conf import settings

User = get_user_model()
user, created = User.objects.get_or_create(username='impl_export_test', defaults={'email': 'impl_export_test@example.com'})
if created:
    user.set_password('TestPass123')
    user.save()

print('Using user:', user.username)

sample_outline = {
    'title': '自动导出测试 PPT',
    'resources': {
        'ppt': {
            'preview': '封面\n- 要点 A\n- 要点 B\n- 要点 C'
        }
    }
}

outline = CourseOutline.objects.create(user=user, title='Test Export PPT', outline_data=json.dumps(sample_outline, ensure_ascii=False), status='completed', progress=100)
print('Created outline id', outline.id)

res = export_outline_task_sync(outline.id)
print('export result:', res)

outline.refresh_from_db()
print('exported_pptx (db):', outline.exported_pptx)
if res.get('success') and outline.exported_pptx:
    abs_path = os.path.join(settings.MEDIA_ROOT, outline.exported_pptx)
    print('file exists:', os.path.exists(abs_path), abs_path)
else:
    print('export did not produce a file')
