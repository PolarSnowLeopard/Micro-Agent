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

> 如果你需要为特定行业快速构建一个专业 Agent 并以 API 服务形式交付，Micro-Agent 是最短路径。

<div align="center">

| 能力 | Micro-Agent | LangGraph | AutoGen¹ | OpenClaw |
|:-----|:---:|:---:|:---:|:---:|
| 开箱即用 API 服务 | ✅ | ✅ | ❌ | ✅ |
| 垂域知识注入 (Skills) | ✅ | ❌ | ❌ | ✅ |
| 内置 RAG 检索增强 | ✅ | 生态 | 扩展 | ✅ |
| MCP 集成 | ✅ | ✅ | ✅ | ✅ |
| 流式 SSE 输出 | ✅ | ✅ | 需自建 | ✅ |
| 多 LLM Profile 配置 | ✅ | ❌ | ❌ | ❌ |
| 轻量（核心 <3K 行） | ✅ | ❌ | ❌ | ❌ |

</div>

> ¹ AutoGen 已进入维护模式，新项目建议使用 [Microsoft Agent Framework](https://github.com/microsoft/autogen)。

**各框架定位：** Micro-Agent 面向垂域专业 Agent 服务交付 · LangGraph 面向复杂多步工作流编排 · AutoGen 面向多角色智能体协作 · OpenClaw 面向个人自主 AI 助手

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


## FAQ

### 通用问题

**Micro-Agent 是什么？**

Micro-Agent 是面向垂域应用的轻量级 AI Agent 框架。它基于 ReAct 执行引擎，集成 MCP 工具、RAG 检索增强、Skill 技能系统，可快速构建专业领域智能体。

**Micro-Agent 与 LangChain/CrewAI 有什么区别？**

Micro-Agent 的特点：
- **轻量**: 核心代码精简，易于理解和定制
- **垂域导向**: 内置示例任务覆盖金融分析、运维监控、安全测试、医疗问答、法律文书、模型评测
- **Skill 系统**: SKILL.md 目录发现，技能可热加载
- **MCP 集成**: 支持 stdio/SSE 协议，无缝对接外部工具
- **可扩展**: 模型、工具、记忆、检索、技能五大组件均可扩展

### 安装与配置

**如何安装 Micro-Agent？**

```bash
pip install -e .
# 或从 PyPI 安装（发布后）
pip install micro-agent
```

**需要什么 Python 版本？**

Python 3.10+ 推荐。

**如何配置 LLM？**

编辑 `config/settings.toml` 或设置环境变量：
```bash
export LLM_MODEL=deepseek-chat
export LLM_API_KEY=your_api_key
export LLM_BASE_URL=https://api.deepseek.com
```

### LLM 提供商

**支持哪些 LLM 提供商？**

通过 LiteLLM 支持：
- OpenAI (GPT-4)
- DeepSeek (deepseek-chat, deepseek-reasoner)
- Claude (Anthropic)
- 本地模型 (Ollama, vLLM)
- 任意 OpenAI 兼容 API

**如何使用本地模型？**

配置 Ollama/vLLM：
```bash
export LLM_MODEL=ollama/llama3
export LLM_BASE_URL=http://localhost:11434
```

### Skills 与记忆

**什么是 Skill？**

Skill 是可复用的能力模块，定义在 `workspace/skills/SKILL.md` 文件中。框架自动发现并注入到 Agent。

**如何创建自定义 Skill？**

在 `workspace/skills/` 下创建目录，编写 `SKILL.md` 描述技能的用途、工具依赖、执行流程。

**记忆系统如何工作？**

- **ShortTermMemory**: 会话内上下文记忆
- **FileMemory**: 持久化文件记忆
- 可扩展 Redis、向量数据库等后端

### RAG 检索增强

**如何使用 RAG？**

将文档放入 `workspace/knowledge/`，EmbeddingRetriever 自动构建索引。Agent 执行时可检索相关上下文。

**支持哪些检索后端？**

默认 EmbeddingRetriever，可扩展 FAISS、ChromaDB、Milvus。

### MCP 工具

**如何配置 MCP 工具？**

在 `config/mcp_servers.toml` 定义 MCP 服务器，支持 stdio 和 SSE 协议。

**内置哪些工具？**

- `BashTool`: 执行 shell 命令
- `MCPTool`: 调用 MCP 服务器工具
- `TerminateTool`: 结束 Agent 执行

### API 服务

**如何启动 API 服务？**

```bash
python -m api.app
# 或 Docker
docker-compose up
```

**API 支持哪些功能？**

- 任务管理接口
- Agent 执行端点
- SSE 流式响应
- 文件上传处理

### 故障排查

**Agent 不调用工具**

- 检查工具定义是否包含正确的 type hints
- 确认工具已注册到 `ToolRegistry`
- 验证模型支持 tool calling

**RAG 检索不生效**

- 确认文档已放入 `workspace/knowledge/`
- 检查 Embedding 模型配置
- 验证检索参数设置

**MCP 连接失败**

- 使用 `pentestagent mcp test <name>` 检测连接
- 检查 MCP 服务器配置的 command/args
- 确认 MCP 包已正确安装

**Skill 未加载**

- 确认 `SKILL.md` 文件在正确目录
- 检查 Skill 目录结构
- 查看 SkillRegistry 日志

**如何调试？**

启用 debug 配置，查看详细日志：
```bash
export DEBUG=true
python -m core.agent
```

### 参考资源

**示例任务在哪？**

`task/builtin.py` 包含金融分析、运维监控、安全测试、医疗问答、法律文书、模型评测等示例。

**如何贡献？**

欢迎提交 Issue 和 PR。代码风格参考项目结构，测试覆盖核心组件。

## 许可

[MIT](LICENSE)
