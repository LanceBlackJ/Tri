import logging
import os
import subprocess
import sys
import threading
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

from agent_system.models import AgentTask
from agent_system.services.embeddings import compute_embedding

from .models import CourseOutline, OutlineExport, CourseMaterial, MaterialChunk
from .utils.pptx_exporter import export_outline_to_pptx
from .utils.material_parser import parse_material_file, MaterialParseError


def process_course_material_task_sync(material_id, task_id=None):
    try:
        material = CourseMaterial.objects.select_related('course', 'uploaded_by').get(pk=material_id)
    except CourseMaterial.DoesNotExist:
        logger.exception('process_course_material_task_sync: CourseMaterial %s not found', material_id)
        return {'success': False, 'error': 'material_not_found'}

    task = None
    if task_id:
        try:
            task = AgentTask.objects.get(pk=task_id)
        except AgentTask.DoesNotExist:
            task = None

    try:
        material.processing_status = 'processing'
        metadata = material.metadata or {}
        metadata['parse_started_at'] = timezone.now().isoformat()
        material.metadata = metadata
        material.save(update_fields=['processing_status', 'metadata', 'updated_at'])
        if task:
            task.status = 'running'
            task.progress = 10
            task.result_summary = '正在解析资料文件'
            task.save(update_fields=['status', 'progress', 'result_summary', 'updated_at'])

        parsed = parse_material_file(material.file.path, material.material_type, material.file.name)

        MaterialChunk.objects.filter(material=material).delete()
        chunk_models = []
        for chunk in parsed.get('chunks', []):
            chunk_models.append(MaterialChunk(
                material=material,
                chunk_index=chunk.get('chunk_index', len(chunk_models)),
                source_page=chunk.get('source_page', ''),
                heading=chunk.get('heading', ''),
                content=chunk.get('content', ''),
                keyword_summary=chunk.get('keyword_summary', ''),
                embedding=compute_embedding(chunk.get('content', '')),
                metadata=chunk.get('metadata', {}),
            ))
        if chunk_models:
            MaterialChunk.objects.bulk_create(chunk_models)

        metadata = material.metadata or {}
        metadata['parse_completed_at'] = timezone.now().isoformat()
        metadata['chunk_count'] = len(chunk_models)
        material.page_count = parsed.get('page_count') or len(chunk_models)
        material.extracted_text = parsed.get('full_text', '')[:50000]
        material.processing_status = 'ready'
        material.metadata = metadata
        material.save(update_fields=['page_count', 'extracted_text', 'processing_status', 'metadata', 'updated_at'])

        if task:
            task.status = 'done'
            task.progress = 100
            task.result_summary = f'资料解析完成，生成 {len(chunk_models)} 个片段'
            task.output_data = {
                'material_id': material.id,
                'chunk_count': len(chunk_models),
                'page_count': material.page_count,
            }
            task.save(update_fields=['status', 'progress', 'result_summary', 'output_data', 'updated_at'])
        return {'success': True, 'material_id': material.id, 'chunk_count': len(chunk_models)}
    except MaterialParseError as exc:
        error_message = str(exc)
    except Exception as exc:
        logger.exception('process_course_material_task_sync failed for %s', material_id)
        error_message = str(exc)

    metadata = material.metadata or {}
    metadata['parse_failed_at'] = timezone.now().isoformat()
    metadata['parse_error'] = error_message
    material.processing_status = 'failed'
    material.metadata = metadata
    material.save(update_fields=['processing_status', 'metadata', 'updated_at'])
    if task:
        task.status = 'failed'
        task.progress = 0
        task.result_summary = '资料解析失败'
        task.output_data = {'error': error_message, 'material_id': material.id}
        task.save(update_fields=['status', 'progress', 'result_summary', 'output_data', 'updated_at'])
    return {'success': False, 'error': error_message, 'material_id': material.id}


def _spawn_material_parse_subprocess(material_id, task_id):
    manage_py = settings.BASE_DIR / 'manage.py'
    if not manage_py.exists():
        logger.error('manage.py not found, cannot spawn subprocess parser for material %s', material_id)
        return False

    command = [
        sys.executable,
        str(manage_py),
        'process_course_material',
        '--material-id',
        str(material_id),
        '--task-id',
        str(task_id),
    ]

    kwargs = {
        'cwd': str(settings.BASE_DIR),
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
        'stdin': subprocess.DEVNULL,
    }
    if os.name == 'nt':
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    else:
        kwargs['start_new_session'] = True

    try:
        subprocess.Popen(command, **kwargs)
        return True
    except Exception:
        logger.exception('Failed to spawn subprocess parser for material %s', material_id)
        return False


def enqueue_course_material_processing(material):
    task = AgentTask.objects.create(
        user=material.uploaded_by,
        name=f'parse_material:{material.title}',
        input_data={
            'task_kind': 'material_parse',
            'material_id': material.id,
            'course_id': material.course_id,
        },
        status='pending',
        progress=0,
    )
    metadata = material.metadata or {}
    metadata['agent_task_id'] = task.id
    material.metadata = metadata
    material.processing_status = 'pending'
    material.save(update_fields=['metadata', 'processing_status', 'updated_at'])

    # 优先入队 Celery；失败退子进程解析；再失败退后台线程
    if process_course_material_task:
        try:
            job = process_course_material_task.delay(material.id, task.id)
            output = task.output_data or {}
            output['celery_job_id'] = getattr(job, 'id', str(job))
            task.output_data = output
            task.save(update_fields=['output_data', 'updated_at'])
            return task
        except Exception:
            logger.exception('Failed to enqueue material parse task with Celery for material %s', material.id)

    if _spawn_material_parse_subprocess(material.id, task.id):
        output = task.output_data or {}
        output['launch_mode'] = 'subprocess'
        task.output_data = output
        task.save(update_fields=['output_data', 'updated_at'])
        return task

    def _run_local():
        process_course_material_task_sync(material.id, task.id)

    threading.Thread(target=_run_local, daemon=True).start()
    output = task.output_data or {}
    output['launch_mode'] = 'thread_fallback'
    task.output_data = output
    task.save(update_fields=['output_data', 'updated_at'])
    return task

def export_outline_task_sync(outline_id, export_id=None):
    """同步导出任务，直接在当前进程中执行。"""
    try:
        outline = CourseOutline.objects.get(pk=outline_id)
    except CourseOutline.DoesNotExist:
        logger.exception('export_outline_task_sync: CourseOutline %s not found', outline_id)
        return {'success': False, 'error': 'not_found'}
    # prepare or fetch export record
    export_record = None
    try:
        if export_id:
            try:
                export_record = OutlineExport.objects.get(pk=export_id)
            except OutlineExport.DoesNotExist:
                export_record = None
        if not export_record:
            export_record = OutlineExport.objects.create(
                course_outline=outline,
                user=outline.user,
                status='pending'
            )
    except Exception:
        logger.exception('Failed to create/fetch OutlineExport record')
        export_record = None

    try:
        # mark running
        try:
            outline.export_status = 'running'
            outline.export_progress = 0
            outline.save(update_fields=['export_status', 'export_progress'])
            if export_record:
                export_record.status = 'running'
                export_record.save(update_fields=['status'])
        except Exception:
            pass

        def _progress_cb(pct):
            try:
                outline.export_progress = int(pct)
                outline.export_status = 'running'
                outline.save(update_fields=['export_progress', 'export_status'])
                if export_record:
                    # no per-percent field on export_record, keep status
                    pass
            except Exception:
                logger.exception('Failed to update export progress for %s', outline_id)

        file_path, filename = export_outline_to_pptx(outline, progress_callback=_progress_cb)
        # store relative path to MEDIA_ROOT
        rel = os.path.relpath(file_path, settings.MEDIA_ROOT)
        rel = rel.replace('\\', '/')
        outline.exported_pptx = rel
        outline.export_progress = 100
        outline.export_status = 'completed'
        outline.save(update_fields=['exported_pptx', 'export_progress', 'export_status'])

        if export_record:
            try:
                export_record.file_path = rel
                export_record.filename = filename
                try:
                    export_record.filesize = os.path.getsize(file_path)
                except Exception:
                    export_record.filesize = None
                export_record.status = 'completed'
                export_record.completed_at = timezone.now()
                export_record.save()
            except Exception:
                logger.exception('Failed to update OutlineExport record after export')

        return {'success': True, 'path': rel}
    except Exception as e:
        logger.exception('export_outline_task_sync failed for %s', outline_id)
        try:
            outline.export_status = 'failed'
            outline.export_progress = 0
            outline.save(update_fields=['export_status', 'export_progress'])
            if export_record:
                export_record.status = 'failed'
                export_record.message = str(e)
                export_record.completed_at = timezone.now()
                export_record.save()
        except Exception:
            logger.exception('Failed to mark export failure for %s', outline_id)
        return {'success': False, 'error': 'export_failed'}

# 如果 Celery 可用，注册异步任务包装器；否则保留同步函数以供回退调用
try:
    from celery import shared_task
except Exception:
    shared_task = None

if shared_task:
    @shared_task(bind=True, max_retries=3, default_retry_delay=60)
    def export_outline_task(self, outline_id, export_id=None):
        try:
            return export_outline_task_sync(outline_id, export_id=export_id)
        except Exception as e:
            logger.exception('export_outline_task celery wrapper failed for %s', outline_id)
            raise self.retry(exc=e)

    @shared_task(bind=True, max_retries=2, default_retry_delay=30)
    def process_course_material_task(self, material_id, task_id=None):
        try:
            return process_course_material_task_sync(material_id, task_id=task_id)
        except Exception as e:
            logger.exception('process_course_material_task celery wrapper failed for %s', material_id)
            raise self.retry(exc=e)
else:
    export_outline_task = None
    process_course_material_task = None
