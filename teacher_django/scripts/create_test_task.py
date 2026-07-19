import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'teacher_django.settings')
django.setup()

from django.contrib.auth import get_user_model
from curriculum_app.models import CourseOutline
from agent_system.models import AgentTask

User = get_user_model()

username = 'testuser'
email = 'test@example.com'
password = 'password123'

user, created = None, False
try:
    user = User.objects.get(username=username)
    print('User exists:', user.username)
except User.DoesNotExist:
    user = User.objects.create_user(username=username, email=email, password=password)
    print('Created user:', user.username)

# 创建 CourseOutline
outline = CourseOutline.objects.create(
    user=user,
    title='测试课程：线性代数入门',
    description='自动创建用于测试的课程大纲',
    outline_data='{}',
    status='pending',
    progress=0,
)
print('Created CourseOutline id=', outline.id)

# 创建 AgentTask
task = AgentTask.objects.create(
    user=user,
    name=f'Generate course: {outline.title}',
    input_data={'outline_id': outline.id, 'topic': outline.title},
    status='pending',
    progress=0,
)
print('Created AgentTask id=', task.id)
