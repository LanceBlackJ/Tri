from __future__ import absolute_import
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'teacher_django.settings')

app = Celery('teacher_django')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
