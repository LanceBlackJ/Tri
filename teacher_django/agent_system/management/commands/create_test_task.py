from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

from curriculum_app.models import CourseOutline
from agent_system.models import AgentTask


class Command(BaseCommand):
    help = 'Create a test user, CourseOutline and AgentTask for local testing'

    def add_arguments(self, parser):
        parser.add_argument('--username', type=str, default='testuser')
        parser.add_argument('--password', type=str, default='password123')

    def handle(self, *args, **options):
        username = options['username']
        password = options['password']
        User = get_user_model()

        user, created = User.objects.get_or_create(username=username, defaults={'email': f'{username}@example.com'})
        if created:
            try:
                # Try to use manager create_user if available
                if hasattr(User.objects, 'create_user'):
                    User.objects.create_user(username=username, email=f'{username}@example.com', password=password)
                    user = User.objects.get(username=username)
                else:
                    user.set_password(password)
                    user.save()
                self.stdout.write(self.style.SUCCESS(f'Created user {username}'))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'Failed to create user: {e}'))
        else:
            self.stdout.write(self.style.WARNING(f'User {username} exists'))

        outline = CourseOutline.objects.create(
            user=user,
            title='测试课程：线性代数入门',
            description='自动创建用于测试的课程大纲',
            outline_data='{}',
            status='pending',
            progress=0,
        )
        task = AgentTask.objects.create(
            user=user,
            name=f'Generate course: {outline.title}',
            input_data={'outline_id': outline.id, 'topic': outline.title},
            status='pending',
            progress=0,
        )
        self.stdout.write(self.style.SUCCESS(f'Created CourseOutline id={outline.id} and AgentTask id={task.id}'))
