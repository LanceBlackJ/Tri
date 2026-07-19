# Docker 部署指南（Django + MySQL + Celery/Redis）

在服务器（Ubuntu 22.04，公网 IP `8.145.62.165`）上用 docker-compose 部署四个服务：
`web`（Django + gunicorn）+ `db`（MySQL 8）+ `redis`（Celery broker）+
`worker`（Celery worker，后台执行课程生成/资料解析/PPTX 导出），对外只开 **6066** 端口。

> 说明：容器里自带一套 MySQL（compose 内网），与服务器上原来那套原生 MySQL 互不相关。
> 部署后用的是**容器里的** MySQL，数据用 `datadump.json` 导入。
>
> `worker` 和 `web` 用的是**同一个镜像**，只是 `SERVICE_ROLE=worker` 让 entrypoint 跳过
> migrate/collectstatic、直接起 `celery worker` 进程——课程生成这类耗时任务由它执行，
> 不会因为 web 的 gunicorn worker 被回收/重启而中途丢失。

---

## 1. 装 Docker（服务器上，只需一次）

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
docker compose version   # 确认 compose 插件可用
```

## 2. 把代码弄到服务器

```bash
git clone <你的仓库地址> Teacher      # 或已在服务器上则 git pull
cd Teacher
```

## 3. 配置环境变量

```bash
cp .env.docker.example .env.docker
vim .env.docker            # 把所有「改成...」填成真实值
```
重点：
- `MYSQL_PASSWORD` 与 `DB_PASSWORD` **必须相同**；`MYSQL_DATABASE` 与 `DB_NAME` 相同
- `SECRET_KEY` 换随机串：`python3 -c "import secrets;print(secrets.token_urlsafe(50))"`
- `ALLOWED_HOSTS` 必须含 `8.145.62.165`
- `XUNFEI_*` 从你本地 `teacher_django/.env` 复制过来（AI 模型地址/key）

## 4. 构建并启动

```bash
docker compose up -d --build
docker compose ps                    # 四个服务都 healthy/running
docker compose logs -f web           # 看 web 启动日志（迁移->收集静态->gunicorn）
docker compose logs -f worker        # 看 worker 启动日志（应看到 "celery@... ready"）
```
首次启动 web 会自动 `migrate` + `collectstatic`；db/redis 都健康后 web 与 worker 才会启动
（healthcheck 保证）。触发一次课程生成后，`docker compose logs -f worker` 能看到任务被
消费的日志（`Task agent_system.tasks.run_agent_task[...] received`）。

## 5. 导入数据

把本地导出的 `datadump.json` 传到服务器（它不在 git 里）：
```bash
# 在你本地机器执行：
scp teacher_django/datadump.json 用户名@8.145.62.165:~/Teacher/
```
然后在服务器上导入到容器 MySQL：
```bash
docker compose cp datadump.json web:/app/datadump.json
docker compose exec web python manage.py loaddata datadump.json
# 期望输出：Installed 1845 object(s) ...
```

## 6. 开放端口 & 访问

- 阿里云**安全组**放行入方向 TCP **6066**
- 浏览器打开：**http://8.145.62.165:6066**
- 用导入数据里的账号登录（如 `admin`）

---

## 常用运维命令

```bash
docker compose logs -f web           # 实时日志
docker compose logs -f worker        # Celery worker 日志（任务执行/失败都在这）
docker compose restart web           # 重启 web
docker compose restart worker        # 重启 worker（改了任务代码后需要）
docker compose up -d --build         # 改代码后重建（web/worker 用同一镜像，一起重建）
docker compose down                  # 停止（保留数据卷）
docker compose down -v               # 停止并删数据卷（⚠️ 清空数据库/Redis）
docker compose exec web python manage.py createsuperuser   # 建管理员
docker compose exec db mysql -ulance -p tri                # 进 MySQL
docker compose exec redis redis-cli                          # 进 Redis（查队列/调试）
docker compose up -d --scale worker=2                         # 需要更大吞吐时加开一个 worker
```

## 排错

| 现象 | 排查 |
|---|---|
| 页面 400 Bad Request | `ALLOWED_HOSTS` 没含服务器 IP |
| web 连不上 db | 确认 `.env.docker` 里 `MYSQL_PASSWORD` == `DB_PASSWORD`；`docker compose logs db` |
| 浏览器打不开 | 安全组没放行 6066；或端口写错（6066 不是 6000/6006） |
| AI 不出话 | `XUNFEI_*` 没填对，或服务器无法出网访问模型地址 |
| 静态样式丢失 | 看 web 日志 collectstatic 是否成功；确认 WhiteNoise 中间件在位 |
| 构建时某包装不上 | 在 `teacher_django/Dockerfile` 的 pip 前加 `apt-get install -y build-essential` |
| 课程生成/资料解析一直卡在 pending | `docker compose logs -f worker` 看 worker 是不是没起来/报错；`docker compose ps` 确认 worker 是 running |
| worker 起不来 | 多半是 redis 没 healthy——`docker compose logs redis`；或 `.env.docker` 里没让 compose 正确覆盖 CELERY_BROKER_URL（compose 已固定注入，不用手填） |

## 切回本地开发

本地不用 Docker 时，Django 仍按 `teacher_django/.env` 走（可指向 SQLite 或那台原生 MySQL），
与容器部署互不影响。
