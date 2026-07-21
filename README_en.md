<div align="center">

<img src="docs/banner.png" alt="Micro-Agent" width="100%">

<br>

[![Python](https://img.shields.io/badge/Python-≥3.11-blue?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![LiteLLM](https://img.shields.io/badge/LLM-litellm-orange)](https://github.com/BerriAI/litellm)
[![MCP](https://img.shields.io/badge/Tool-MCP-purple)](https://modelcontextprotocol.io)

[中文](README.md) | **English**

</div>

---

## Why Micro-Agent

> If you need to rapidly build a domain-specific Agent and deliver it as an API service, Micro-Agent is the shortest path.

<div align="center">

| Capability | Micro-Agent | LangGraph | AutoGen¹ | OpenClaw |
|:-----------|:---:|:---:|:---:|:---:|
| Out-of-the-box API Service | ✅ | ✅ | ❌ | ✅ |
| Domain Knowledge Injection (Skills) | ✅ | ❌ | ❌ | ✅ |
| Built-in RAG | ✅ | Ecosystem | Extension | ✅ |
| MCP Integration | ✅ | ✅ | ✅ | ✅ |
| Streaming SSE Output | ✅ | ✅ | DIY | ✅ |
| Multi-LLM Profile Config | ✅ | ❌ | ❌ | ❌ |
| Lightweight (core <3K LoC) | ✅ | ❌ | ❌ | ❌ |

</div>

> ¹ AutoGen has entered maintenance mode. For new projects consider [Microsoft Agent Framework](https://github.com/microsoft/autogen).

**Framework positioning:** Micro-Agent → domain-specific Agent service delivery · LangGraph → complex multi-step workflow orchestration · AutoGen → multi-role agent collaboration · OpenClaw → personal autonomous AI assistant

## Architecture

<div align="center">
<img src="docs/architecture.png" alt="Architecture" width="100%">
</div>

<br>

**Core Components:**

- **LLM Layer** — Unified interface via [litellm](https://github.com/BerriAI/litellm). One codebase, any model: OpenAI / DeepSeek / Claude / Ollama and more.
- **Agent Core** — ReAct execution engine (Think → Act → Observe loop) with SubAgent task delegation and REPL sandbox execution.
- **Memory** — Session memory system supporting short-term memory, file persistence, and cross-session recovery.
- **Skills** — Inject domain specifications, coding standards, and expert knowledge into the Agent's system prompt for professional capabilities.
- **RAG** — Retrieve relevant documents from a domain knowledge base to provide reasoning context.
- **MCP / Tools** — Connect to external tools and data sources via [Model Context Protocol](https://modelcontextprotocol.io).

## Quick Start

### Prerequisites

- Python ≥ 3.11
- Any LLM API key (OpenAI / DeepSeek / Claude / Ollama / OpenRouter, etc.)

### Installation

```bash
git clone https://github.com/fdueblab/Micro-Agent.git
cd Micro-Agent

pip install -e ".[dev]"
```

### Configuration

```bash
cp .env.example .env
```

Edit `.env` and fill in your API key:

```bash
LLM_MODEL=deepseek/deepseek-chat
LLM_API_KEY=sk-xxx
```

> Any [litellm-compatible model format](https://docs.litellm.ai/docs/providers) is supported, e.g. `openai/gpt-4o`, `ollama/qwen2.5`, `openrouter/qwen/qwen3-coder-flash`, etc.

### Launch

```bash
uvicorn api.app:app --host 0.0.0.0 --port 8010 --reload
```

Visit `http://localhost:8010/docs` for API documentation.

### Docker Deployment

```bash
docker-compose up -d
```

## Defining Domain Tasks

Three simple steps to turn a general-purpose Agent into a domain-specific professional:

### 1. Write a Prompt Template

```jinja2
{# task/templates/code_review.md.j2 #}
Please review the following code with a focus on security and performance:

Code path: {{ code_path }}
Review standards: {{ standards }}
```

### 2. Register the Task

```python
# task/builtin.py
register_task(TaskConfig(
    name="code_review",
    prompt_template="code_review.md.j2",
    system_prompt="You are a senior code review engineer.",
    llm_profile="reasoning",
    max_steps=20,
))
```

### 3. Call the API

```bash
curl -X POST http://localhost:8010/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Review src/main.py", "agent_name": "code_review"}'
```

## Multi-LLM Profiles

Configure different model strategies for different scenarios:

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

Specify in tasks via `llm_profile`:

```python
register_task(TaskConfig(
    name="my_task",
    llm_profile="reasoning",  # use reasoning model
    ...
))
```

## Built-in Example Tasks

The project ships with several real-world Agent tasks as reference implementations:

| Task | Description | Domain Components |
|------|-------------|-------------------|
| Code Analysis | Upload code → auto-analyze function structure | Tools |
| Service Wrapping | Upload code → auto-generate Docker + MCP service | Skills + RAG + Memory |
| Algorithm Generation | Describe requirements → generate algorithm code | Skills + RAG + Memory |
| MCP Service Testing | Connect to MCP server → auto-discover and test tools | MCP |
| Service Evaluation | Upload data → run evaluation and output report | Tools |
| AML Model Evaluation | Upload data → multi-metric security evaluation (with data adaptation) | MCP + Tools |

> These tasks demonstrate how to combine Skills, RAG, MCP, and other components to turn a general Agent into a domain-specific professional. Use them as references to build your own tasks.

## Extension Points

| Component | Interface | Built-in Implementations | Extension Directions |
|-----------|-----------|--------------------------|----------------------|
| Model | litellm | OpenAI, DeepSeek, Claude | Ollama, vLLM, any OpenAI-compatible API |
| Tool | `Tool` ABC | Bash, MCP, Terminate | Any custom tool |
| Memory | `MemoryProvider` | ShortTermMemory, FileMemory | Redis, vector databases |
| Retrieval | `Retriever` | EmbeddingRetriever | FAISS, ChromaDB, Milvus |
| Skill | `Skill` + `SkillRegistry` | SKILL.md directory discovery | Remote skill marketplace |

## Project Structure

```
Micro-Agent/
├── core/                 # Agent core engine
│   ├── agent.py          # ReAct loop execution engine
│   ├── llm.py            # Unified LLM call layer (litellm)
│   ├── config.py         # Configuration management (TOML + env vars)
│   ├── memory/           # Memory system (short-term / persistent)
│   ├── rag/              # Retrieval augmentation (Embedding)
│   ├── skill/            # Skill system (register / discover / inject)
│   └── schema.py         # Data models (Event / Message / ToolCall)
├── tool/                 # Tool layer
│   ├── base.py           # Tool abstract interface
│   ├── bash.py           # Bash command execution
│   ├── mcp/              # MCP tools (stdio / SSE)
│   └── registry.py       # Tool registry
├── task/                 # Task definitions
│   ├── base.py           # TaskConfig + template rendering
│   ├── builtin.py        # Built-in task registration
│   └── templates/        # Jinja2 prompt templates
├── api/                  # API service layer
│   ├── app.py            # FastAPI entry point
│   ├── routes/           # Routes (task management / Agent endpoints)
│   └── services/         # SSE streaming / file handling
├── workspace/            # Workspace
│   ├── knowledge/        # RAG knowledge base documents
│   └── skills/           # Skill definitions (SKILL.md)
├── config/               # Configuration files
├── tests/                # Tests
└── deploy/               # Docker deployment
```

## License

[MIT](LICENSE)
