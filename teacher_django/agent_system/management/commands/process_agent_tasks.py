from django.core.management.base import BaseCommand
from time import sleep
from django.utils import timezone

from agent_system.models import AgentTask
from agent_system.agents import orchestrate_generate_resources


class Command(BaseCommand):
    help = 'Process pending AgentTask entries (generate resources)'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=int, default=5, help='Idle sleep seconds between polls')
        parser.add_argument('--once', action='store_true', help='Process tasks once then exit')
        parser.add_argument('--limit', type=int, default=1, help='Max tasks to process per loop')

    def handle(self, *args, **options):
        interval = options['interval']
        once = options['once']
        limit = options['limit']
        self.stdout.write('Starting AgentTask processor')
        while True:
            # 尝试按优先级降序、创建时间升序调度任务
            candidates = list(AgentTask.objects.filter(status='pending').order_by('-priority', 'created_at')[: max(limit*5, 50)])
            tasks = []
            for t in candidates:
                deps = t.depends_on or []
                if not deps:
                    tasks.append(t)
                else:
                    # 检查所有依赖任务是否已完成
                    all_done = True
                    for dep_id in deps:
                        try:
                            dep = AgentTask.objects.get(pk=dep_id)
                            if dep.status != 'done':
                                all_done = False
                                break
                        except AgentTask.DoesNotExist:
                            # 如果依赖不存在，忽略该依赖
                            continue
                    if all_done:
                        tasks.append(t)
                if len(tasks) >= limit:
                    break
            if not tasks:
                if once:
                    break
                self.stdout.write(f'No pending tasks, sleeping {interval}s')
                sleep(interval)
                continue
            processed_any = False
            for task in tasks:
                try:
                    data = task.input_data or {}
                    topic = data.get('topic') or data.get('topic_name') or '未指定主题'
                    types = data.get('resource_types') or data.get('types') or None
                    self.stdout.write(f'Processing task {task.id} topic={topic}')
                    orchestrate_generate_resources(task.user, topic, resource_types=types, task=task)
                    processed_any = True
                except Exception as e:
                    task.status = 'failed'
                    task.output_data = {'error': str(e)}
                    task.updated_at = timezone.now()
                    task.save()
                    self.stderr.write(f'Task {task.id} failed: {e}')
            if not processed_any and not tasks:
                # 没有合适的任务可以处理
                if once:
                    break
                self.stdout.write(f'No runnable pending tasks, sleeping {interval}s')
                sleep(interval)
                continue
            if once:
                break
