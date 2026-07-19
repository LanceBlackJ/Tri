from django.core.management.base import BaseCommand
from curriculum_app.models import CourseOutline


class Command(BaseCommand):
    help = 'Show CourseOutline progress and status for given id'

    def add_arguments(self, parser):
        parser.add_argument('outline_id', type=int)

    def handle(self, *args, **options):
        oid = options['outline_id']
        try:
            o = CourseOutline.objects.get(pk=oid)
            self.stdout.write(f'Outline id={o.id} title={o.title} status={o.status} progress={o.progress}')
            self.stdout.write('outline_data:')
            self.stdout.write(o.outline_data or '')
        except CourseOutline.DoesNotExist:
            self.stderr.write(f'Outline {oid} not found')
