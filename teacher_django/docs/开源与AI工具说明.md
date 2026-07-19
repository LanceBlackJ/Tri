# 开源项目与前沿 AI 工具使用说明

> 本文件按赛题要求，在显著位置集中标注本系统所使用的**开源项目、前沿 AI 技术/工具、AI Coding 工具**的名称、来源与相关协议。所有第三方组件均遵循其开源/使用协议使用。

## 一、后端开源依赖（见 `requirements.txt`）

| 组件 | 用途 | 许可证 | 来源 |
|---|---|---|---|
| Django (>=4.2,<5.0) | Web 框架 | BSD-3-Clause | https://www.djangoproject.com |
| Celery (>=5.2) | 异步任务队列（资源生成后台化） | BSD-3-Clause | https://docs.celeryq.dev |
| redis-py (>=4.5) | Redis 客户端（Celery broker / 缓存） | MIT | https://github.com/redis/redis-py |
| requests (>=2.28) | HTTP 客户端（调用讯飞星火 API） | Apache-2.0 | https://requests.readthedocs.io |
| websocket-client (>=1.6) | WebSocket 客户端 | Apache-2.0 | https://github.com/websocket-client/websocket-client |
| bcrypt (>=4.0) | 密码哈希 | Apache-2.0 | https://github.com/pyca/bcrypt |
| python-pptx (>=0.6.21) | 导出 PPTX 课件 | MIT | https://github.com/scanny/python-pptx |
| pypdf (>=4.2) | 解析上传的 PDF 课程资料 | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| Pillow (>=9.0) | 图像处理 | MIT-CMU (HPND) | https://python-pillow.org |

## 二、前端开源库（通过 CDN 引入）

| 组件 | 用途 | 许可证 | 来源 |
|---|---|---|---|
| Tailwind CSS | 原子化样式 | MIT | https://tailwindcss.com |
| Font Awesome (Free) | 图标 | 图标 CC BY 4.0 / 字体 SIL OFL 1.1 / 代码 MIT | https://fontawesome.com |
| KaTeX | 数学公式渲染 | MIT | https://katex.org |
| highlight.js | 代码高亮 | BSD-3-Clause | https://highlightjs.org |
| marked | Markdown 渲染 | MIT | https://marked.js.org |
| DOMPurify | XSS 净化（渲染前清理 HTML） | Apache-2.0 / MPL-2.0（二选一） | https://github.com/cure53/DOMPurify |
| Chart.js | 学习画像雷达图/成长趋势图 | MIT | https://www.chartjs.org |

## 三、前沿 AI 技术 / 大模型平台

| 名称 | 在系统中的用途 | 使用协议 | 来源 |
|---|---|---|---|
| **科大讯飞·星火认知大模型**（Spark，OpenAI 兼容接口 `spark-api-open.xf-yun.com`） | 全系统的自然语言理解与生成核心：对话式画像构建、多智能体资源生成（讲义/PPT/练习题/代码/思维导图/拓展阅读）、学习路径编排、智能答疑、内容安全分级、事实性校验 | 讯飞开放平台 API 服务条款（需在讯飞开放平台申请 APIKey，商用/参赛须遵循其 API 使用与内容合规协议） | https://xinghuo.xfyun.cn ／ https://www.xfyun.cn |

> 说明：赛题要求"开发过程中使用的 AI 辅助工具需选用科大讯飞相关工具"，本系统的**全部大模型能力均基于科大讯飞星火**，未使用其他厂商的推理大模型。

## 四、AI Coding 工具（开发过程使用）

| 工具 | 用途 | 说明 |
|---|---|---|
| **Claude Code（Anthropic Claude）** | 辅助代码开发、重构、测试、缺陷定位与文档撰写 | 商业 AI 编程助手，遵循 Anthropic 使用条款；仅用于本团队开发提效，所有代码经团队审阅与测试（`manage.py test` 覆盖 200+ 用例）。来源：https://www.anthropic.com |

## 五、多智能体协同框架说明

本系统的"多智能体协同框架"为**自研**（位于 `agent_system/`），不依赖第三方 Agent 框架，核心角色：

- **PlannerAgent**：按学生画像规划课程大纲与学习路径阶段；
- **CriticAgent / DebateCriticAgent**：COGENT 六维审核 + 双评审"辩论"，把关内容准确性（防幻觉第一道）；
- **ReflectionController**：反思-修订循环，低分内容自动重写；
- **StudentSimulatorAgent**：模拟目标学生阅读，按画像做个性化适配改写；
- **CodeAgent / MindMapAgent / ReadingAgent**：分别产出代码实操案例 / 思维导图 / 拓展阅读；

各智能体的协作过程通过 `collaboration_log` 记录，并在课程页"Agent 协作过程"时间线可视化。

---

*若后续新增任何开源组件或 AI 工具，请同步更新本文件与根目录 `NOTICE`。*
