from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from django.utils import timezone

import logging
import requests

from .models import AgentTask

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def run_agent_task(self, task_id: int):
    """Celery wrapper: load AgentTask and delegate generation to GenerationManager.

    对于网络/请求异常会进行重试（最多 3 次），其他异常将标记任务失败并写回错误信息。
    """
    try:
        task = AgentTask.objects.get(pk=task_id)
    except AgentTask.DoesNotExist:
        logger.warning('AgentTask %s not found', task_id)
        return {'ok': False, 'error': 'task not found'}

    data = task.input_data or {}
    topic = data.get('topic') or data.get('topic_name') or '未指定主题'
    types = data.get('resource_types') or data.get('types') or None
    outline_id = data.get('outline_id')

    task.status = 'running'
    task.progress = 0
    task.save()

    try:
        from .generation import GenerationManager

        gm = GenerationManager(task.user, topic, outline_id=outline_id, task=task, resource_types=types)
        results = gm.generate()
        logger.info('run_agent_task %s completed successfully', task_id)
        return {'ok': True, 'task_id': task.id, 'results': results}

    except requests.exceptions.RequestException as e:
        logger.exception('Transient request error in run_agent_task %s: %s', task_id, e)
        try:
            self.retry(exc=e)
        except MaxRetriesExceededError:
            logger.exception('Max retries exceeded for task %s', task_id)
            task.status = 'failed'
            task.output_data = {'error': str(e)}
            task.updated_at = timezone.now()
            task.save()
            return {'ok': False, 'error': str(e)}

    except Exception as e:
        logger.exception('run_agent_task %s failed: %s', task_id, e)
        task.status = 'failed'
        task.output_data = {'error': str(e)}
        task.updated_at = timezone.now()
        task.save()
        return {'ok': False, 'error': str(e)}
