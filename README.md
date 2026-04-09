# Micro-Agent V2

IoEB 众智工场垂域大模型 Agent 框架。

## 快速开始

```bash
# 环境要求：Python >= 3.11
conda create -n micro-agent-v2 python=3.12 -y
conda activate micro-agent-v2

# 安装
cd micro-agent-v2
pip install -e ".[dev]"

# 配置
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY

# 验证
python -m pytest tests/ -v

# 启动服务
uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload
```

## 项目结构

```
core/           Agent 核心（agent、llm、config、memory、skill、rag）
tool/           工具层（Tool ABC、ToolRegistry、MCP、Bash）
task/           任务定义（TaskConfig、Jinja2 模板、内置任务注册）
api/            FastAPI 服务（路由、SSE 流、前端兼容接口）
tests/          测试（51 个，覆盖全部 5 个阶段）
config/         TOML 配置文件
```

## 多 LLM Profile

`config/default.toml` 支持多命名模型 profile：

```toml
[llm.default]
model = "deepseek/deepseek-chat"

[llm.fast]
model = "deepseek/deepseek-chat"
max_tokens = 4096

[llm.reasoning]
model = "openai/o1-mini"
```

通过环境变量覆盖 default profile：
```bash
LLM_MODEL=openai/gpt-4o
LLM_API_KEY=sk-xxx
```

## API 端点

### 新版接口（JSON body）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/agent/{task_name}` | 执行预定义任务，返回 SSE 流 |
| POST | `/api/agent/custom` | 执行自定义 prompt 任务 |
| GET  | `/api/agent/tasks` | 列出可用预定义任务 |
| POST | `/api/tasks` | 提交通用任务 |
| GET  | `/api/tasks/{id}/stream` | SSE 流（支持断线续传） |
| POST | `/api/tasks/{id}/cancel` | 取消任务 |

### 兼容接口（Form/File，对齐旧版前端）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/agent/code_analysis` | 代码分析（文件上传） |
| POST | `/api/agent/service_packaging` | 服务封装（文件上传） |
| POST | `/api/agent/mcp_test` | MCP 服务测试 |
| POST | `/api/agent/service_evaluation` | 服务评测 |
| POST | `/api/agent/meta_app/run` | 元应用执行 |
| POST | `/api/agent/capability_describe` | 能力描述翻译 |
| POST | `/api/agent/capability_chat` | 引导式问答 |

## 构建新 Agent

以"软著说明书生成"为例：

**1. 创建 Prompt 模板** `task/templates/software_copyright.md.j2`：
```jinja2
请根据以下项目信息生成软著说明书：
项目名称：{{ project_name }}
...
```

**2. 注册任务**（在 `task/builtin.py` 中追加）：
```python
register_task(TaskConfig(
    name="software_copyright",
    prompt_template="software_copyright.md.j2",
    system_prompt="你是软著文档撰写专家。",
    llm_profile="reasoning",
    max_steps=20,
))
```

**3. 调用**：
```bash
curl -X POST http://localhost:8000/api/agent/software_copyright \
  -H "Content-Type: application/json" \
  -d '{"params": {"project_name": "IoEB 众智工场"}}'
```

## 扩展点

| 扩展 | 接口 | 当前实现 | 可选实现 |
|------|------|----------|----------|
| 记忆 | `MemoryProvider` | ShortTermMemory, FileMemory | Redis, 向量数据库 |
| 检索 | `Retriever` | SimpleRetriever (关键词) | FAISS, ChromaDB, Milvus |
| 技能 | `Skill` + `SkillRegistry` | 目录发现 (SKILL.md) | 远程技能市场 |
| 工具 | `Tool` ABC | Bash, MCP, Simulated | 任意自定义工具 |
| 模型 | litellm | DeepSeek, OpenAI | Claude, Ollama, vLLM |

## 与旧版的关系

`Micro-Agent/`（旧版）继续运行支撑线上业务。`micro-agent-v2/` 独立开发，验证通过后切换。

详细的架构决策、开发阶段和交接规范见 [ROADMAP.md](ROADMAP.md)。
