# Django web 镜像。构建上下文 = teacher_django/（Django 项目根，manage.py 所在处）。
FROM python:3.11-slim

# PYTHONUNBUFFERED：日志实时输出（gunicorn/print 不被缓冲）
# PYTHONDONTWRITEBYTECODE：不生成 .pyc
# PYTHONUTF8：强制 UTF-8，避免中文编码问题
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 所有依赖（Django/Pillow/cryptography/PyMySQL 等）在 cp311 都有 manylinux wheel，
# slim 镜像无需编译工具即可安装。用清华 pip 源提升国内构建稳定性。
# 若日后有包需要现场编译，再在此加 apt build-essential。
COPY requirements.txt .
RUN pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple \
 && pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 拷贝项目代码。.dockerignore 已排除 .env / *.db / media / datadump.json 等，
# 尤其 .env 必须排除，否则镜像里的开发配置会覆盖 compose 注入的容器环境变量。
COPY . .

# 去掉可能的 Windows CRLF 换行（否则 Linux 下 /bin/sh 报 bad interpreter），再赋可执行权限
RUN sed -i 's/\r$//' docker/entrypoint.sh && chmod +x docker/entrypoint.sh

# gunicorn 监听容器内 8000；对外端口由 docker-compose 映射（6066:8000）。
EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
