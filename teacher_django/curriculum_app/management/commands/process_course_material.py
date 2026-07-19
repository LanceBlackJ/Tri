from django.core.management.base import BaseCommand, CommandError

from curriculum_app.tasks import process_course_material_task_sync


class Command(BaseCommand):
    help = '在独立进程中解析课程资料，避免上传请求所在进程被重度占用。'

    def add_arguments(self, parser):
        parser.add_argument('--material-id', type=int, required=True)
        parser.add_argument('--task-id', type=int, required=False)

    def handle(self, *args, **options):
        material_id = options['material_id']
        task_id = options.get('task_id')
        result = process_course_material_task_sync(material_id, task_id=task_id)
        if not result.get('success'):
            raise CommandError(result.get('error') or 'material_parse_failed')
        self.stdout.write(self.style.SUCCESS(f"processed material {material_id}"))