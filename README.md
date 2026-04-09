<div align="center">

<img src="docs/banner.png" alt="Micro-Agent" width="100%">

<br>

[![Python](https://img.shields.io/badge/Python-≥3.11-blue?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![LiteLLM](https://img.shields.io/badge/LLM-litellm-orange)](https://github.com/BerriAI/litellm)
[![MCP](https://img.shields.io/badge/Tool-MCP-purple)](https://modelcontextprotocol.io)

</div>

---

## 为什么选择 Micro-Agent

| | Micro-Agent | LangGraph | AutoGen | OpenClaw |
|---|---|---|---|---|
| **定位** | 垂域 Agent 服务 | 通用工作流编排 | 多智能体协作 | 个人自治助手 |
| **架构** | ReAct Loop + 可插拔组件 | 有向图状态机 | 会话驱动消息传递 | 3 层技能 + 持久自治 |
| **部署** | Docker 一键启动，API 即用 | 需自行搭建服务层 | 需自行搭建服务层 | 独立进程，24/7 常驻 |
| **体量** | 核心 <3000 行，依赖精简 | 依赖 LangChain 全家桶 | 中等，微软维护 | 插件生态庞大但安全隐患多 |
| **模型** | litellm 统一接口，一行切换 | 依赖 LangChain 适配层 | 原生多模型支持 | 多模型支持 |
| **垂域能力** | Skills + RAG + KG 原生支持 | 需自行组装 | 需自行组装 | 通用自动化，非垂域 |
| **适合场景** | 行业知识驱动的专业 Agent | 复杂多步工作流 | 多角色协作 | 个人效率自动化 |

## 架构

<div align="center">
<img src="docs/architecture.png" alt="Architecture" width="100%">
</div>

<br>

**核心组件：**

- **LLM Layer** — 通过 [litellm](https://github.com/BerriAI/litellm) 统一接口，一套代码切换 OpenAI / DeepSeek / Claude / Ollama 等任意模型
- **Agent Core** — ReAct 执行引擎（Think → Act → Observe 循环），支持 SubAgent 子任务分发与 REPL 沙箱执行
- **Memory** — 会话记忆系统，支持短期记忆、文件持久化、跨会话恢复
- **Skills** — 将领域规范、编码标准等知识注入 Agent 的 system prompt，使其具备专业能力
- **RAG** — 从领域知识库中检索相关文档，为推理提供上下文
- **Knowledge Graph** — 基于图结构的领域知识表示与关联推理
- **MCP / Tools** — 通过 [Model Context Protocol](https://modelcontextprotocol.io) 连接外部工具和数据源

## 快速开始

### 环境要求

- Python ≥ 3.11
- 任意 LLM API Key（OpenAI / DeepSeek / Claude / Ollama / OpenRouter 等）

### 安装

```bash
git clone https://github.com/fdueblab/Micro-Agent.git
cd Micro-Agent

pip install -e ".[dev]"
```

### 配置

```bash
cp .env.example .env
```

编辑 `.env`，填入 API Key：

```bash
LLM_MODEL=deepseek/deepseek-chat
LLM_API_KEY=sk-xxx
```

> 支持任何 [litellm 兼容的模型格式](https://docs.litellm.ai/docs/providers)，如 `openai/gpt-4o`、`ollama/qwen2.5`、`openrouter/qwen/qwen3-coder-flash` 等。

### 启动

```bash
uvicorn api.app:app --host 0.0.0.0 --port 8010 --reload
```

访问 `http://localhost:8010/docs` 查看 API 文档。

### Docker 部署

```bash
docker-compose up -d
```

## 定义垂域任务

只需三步，即可将通用 Agent 转化为面向特定领域的专业智能体：

### 1. 编写 Prompt 模板

```jinja2
{# task/templates/code_review.md.j2 #}
请对以下代码进行审查，重点关注安全性和性能：

代码路径: {{ code_path }}
审查标准: {{ standards }}
```

### 2. 注册任务

```python
# task/builtin.py
register_task(TaskConfig(
    name="code_review",
    prompt_template="code_review.md.j2",
    system_prompt="你是一名资深代码审查工程师。",
    llm_profile="reasoning",
    max_steps=20,
))
```

### 3. 调用

```bash
curl -X POST http://localhost:8010/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt": "审查 src/main.py", "agent_name": "code_review"}'
```

## 多 LLM Profile

为不同场景配置不同的模型策略：

```toml
# config/config.toml

[llm.default]
model = "deepseek/deepseek-chat"
temperature = 0.0
max_tokens = 8192

[llm.fast]
model = "deepseek/deepseek-chat"
max_tokens = 4096
timeout = 30

[llm.reasoning]
model = "openai/o1-mini"
max_tokens = 16384
timeout = 120
```

任务中通过 `llm_profile` 指定：

```python
register_task(TaskConfig(
    name="my_task",
    llm_profile="reasoning",  # 使用推理模型
    ...
))
```

## 内置示例任务

项目内置了多个真实场景的 Agent 任务作为参考实现：

| 任务 | 说明 | 垂域组件 |
|------|------|----------|
| 代码分析 | 上传代码 → 自动分析函数结构 | Tools |
| 服务封装 | 上传代码 → 自动生成 Docker + MCP 服务 | Skills + RAG + Memory |
| 算法模型生成 | 描述需求 → 生成算法模型代码 | Skills + RAG + Memory |
| MCP 服务测试 | 连接 MCP 服务器 → 自动发现并测试工具 | MCP |
| 服务评测 | 上传数据 → 自动执行评测并输出报告 | Tools |
| AML 模型评测 | 上传数据 → 多指标安全评测（支持数据适配） | MCP + Tools |

> 这些任务展示了如何通过组合 Skills、RAG、MCP 等组件，将通用 Agent 打造为垂域专业智能体。你可以参考它们的实现来构建自己的任务。

## 扩展点

| 组件 | 接口 | 内置实现 | 可扩展方向 |
|------|------|----------|------------|
| 模型 | litellm | OpenAI, DeepSeek, Claude | Ollama, vLLM, 任意 OpenAI 兼容 API |
| 工具 | `Tool` ABC | Bash, MCP, Terminate | 任意自定义工具 |
| 记忆 | `MemoryProvider` | ShortTermMemory, FileMemory | Redis, 向量数据库 |
| 检索 | `Retriever` | EmbeddingRetriever | FAISS, ChromaDB, Milvus |
| 技能 | `Skill` + `SkillRegistry` | SKILL.md 目录发现 | 远程技能市场 |

## 项目结构

```
Micro-Agent/
├── core/                 # Agent 核心引擎
│   ├── agent.py          # ReAct 循环执行引擎
│   ├── llm.py            # LLM 统一调用层 (litellm)
│   ├── config.py         # 配置管理 (TOML + 环境变量)
│   ├── memory/           # 记忆系统 (短期 / 持久化)
│   ├── rag/              # 检索增强 (Embedding)
│   ├── skill/            # 技能系统 (注册 / 发现 / 注入)
│   └── schema.py         # 数据模型 (Event / Message / ToolCall)
├── tool/                 # 工具层
│   ├── base.py           # Tool 抽象接口
│   ├── bash.py           # Bash 命令执行
│   ├── mcp/              # MCP 工具 (stdio / SSE)
│   └── registry.py       # 工具注册表
├── task/                 # 任务定义
│   ├── base.py           # TaskConfig + 模板渲染
│   ├── builtin.py        # 内置任务注册
│   └── templates/        # Jinja2 Prompt 模板
├── api/                  # API 服务层
│   ├── app.py            # FastAPI 入口
│   ├── routes/           # 路由 (任务管理 / Agent 端点)
│   └── services/         # SSE 流 / 文件处理
├── workspace/            # 工作区
│   ├── knowledge/        # RAG 知识库文档
│   └── skills/           # Skill 定义 (SKILL.md)
├── config/               # 配置文件
├── tests/                # 测试
└── deploy/               # Docker 部署
```

## 许可

[MIT](LICENSE)
