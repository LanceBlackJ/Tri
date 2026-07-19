#!/bin/sh
# web 容器启动流程：迁移 -> 收集静态 -> 起 gunicorn。
# 数据库由 compose 的 depends_on(service_healthy) 保证已就绪。
# worker 容器（SERVICE_ROLE=worker）复用同一个镜像，跳过 migrate/collectstatic
# （由 web 负责，避免两边并发抢着 migrate），直接起 Celery worker。
set -e

ROLE="${SERVICE_ROLE:-web}"
if [ "$ROLE" = "worker" ]; then
    echo "[entrypoint] worker 角色：启动 Celery worker（容器内不跑 migrate/collectstatic）..."
    exec celery -A teacher_django worker -l info --concurrency="${CELERY_CONCURRENCY:-2}"
fi

echo "[entrypoint] 应用数据库迁移..."
python manage.py migrate --noinput

echo "[entrypoint] 收集静态文件..."
python manage.py collectstatic --noinput

echo "[entrypoint] 启动 gunicorn (gthread worker, 容器内 8000)..."
# gthread + 多线程：扛住 SSE 流式对话/生成的长连接，避免单个流阻塞整个 worker。
# timeout 默认 600s：AI 生成课件/流式回答可能耗时较久。
exec gunicorn teacher_django.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-3}" \
    --threads "${GUNICORN_THREADS:-8}" \
    --worker-class gthread \
    --timeout "${GUNICORN_TIMEOUT:-600}" \
    --access-logfile - \
    --error-logfile -
