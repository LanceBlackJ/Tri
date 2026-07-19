# 设计概览（简要）

系统目标：基于讯飞星火构建高等教育个性化学习资源生成与推送系统，支持多智能体协同、多模态资源生成、画像驱动推荐与安全合规检查。

主要模块：
- `agent_system` 应用：画像、资源、任务模型；agents 编排；API 与管理命令。
- `services`：讯飞星火客户端、内容安全、embedding 占位实现、画像构建器。
- 后台：可通过 `process_agent_tasks` 管理命令或 Celery 进行异步任务执行。

关键设计点：
- 学生画像：六维（`knowledge_profile`, `cognitive_style`, `learning_goals`, `misconceptions`, `engagement`, `learning_preferences`）。
- 资源元数据：`resource_type`, `tags`, `metadata`, `embedding`。
- 任务编排：`AgentTask` 支持 `priority` 和 `depends_on`，支持 Celery 或轮询 Worker 执行。
- 安全：`services/safety.py` 提供词表与讯飞合规占位调用的混合检查策略。

运行（本地）示例命令：
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r teacher_django/requirements.txt
python teacher_django/manage.py makemigrations
python teacher_django/manage.py migrate
python teacher_django/manage.py runserver
# 可在另一个终端运行后台轮询器：
python teacher_django/manage.py process_agent_tasks --interval 5
```

注：Embedding 当前为占位实现，应在生产环境替换为真实 embedding 服务或向量数据库。
