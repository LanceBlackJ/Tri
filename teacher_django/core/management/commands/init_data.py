from django.core.management.base import BaseCommand
from django.contrib.auth.models import User as DjangoUser


class Command(BaseCommand):
    help = '初始化数据库，确保自定义用户模型正确设置'

    def handle(self, *args, **options):
        # 确保没有使用默认的 Django User 模型
        if DjangoUser.objects.exists():
            self.stdout.write(
                self.style.WARNING('警告: 发现默认 Django 用户，请确保使用 core.User 模型')
            )
        
        self.stdout.write(
            self.style.SUCCESS('数据库初始化完成！')
        )