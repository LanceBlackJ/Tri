from django.core.management.base import BaseCommand

from curriculum_app.models import CourseMaterial
from curriculum_app.utils.material_parser import infer_material_type_from_filename


class Command(BaseCommand):
    help = (
        '按文件扩展名重新识别所有课程资料的 material_type，'
        '修复历史数据里遗留的手选错误/默认值（例如大批 PDF 被误存成 other）。'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='只打印将要修改的记录，不实际写库',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        materials = CourseMaterial.objects.all()
        total = materials.count()
        updated = 0

        for material in materials:
            filename = material.file.name if material.file else ''
            correct_type = infer_material_type_from_filename(filename)
            if material.material_type != correct_type:
                self.stdout.write(
                    f'  #{material.id} 《{material.title}》: '
                    f'{material.material_type} -> {correct_type}  ({filename})'
                )
                if not dry_run:
                    material.material_type = correct_type
                    material.save(update_fields=['material_type'])
                updated += 1

        verb = '将更正' if dry_run else '已更正'
        self.stdout.write(self.style.SUCCESS(
            f'共检查 {total} 份资料，{verb} {updated} 份的类型。'
        ))
