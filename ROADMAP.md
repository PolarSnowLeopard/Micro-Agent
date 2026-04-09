# Micro-Agent V2 开发路线图

> 本文件是跨会话协作的核心文档。任何 agent 接手开发前，必须先读此文件了解项目状态和架构约定。

## 项目概述

Micro-Agent V2 是对 `Micro-Agent/` 的完全重写，服务于 IoEB 众智工场平台。目标是提供统一的垂域大模型 Agent 框架，供课题组成员构建各自的垂域应用（算法生成、服务封装、元应用开发、心理健康分析等）。

**与旧版的关系**：`Micro-Agent/` 继续运行支撑线上业务，V2 独立开发，验证通过后切换。

---

## 架构决策（必须遵守）

以下决策已经过充分讨论，后续开发 **不得偏离**，除非在本文件中记录变更原因。

| # | 决策 | 原因 |
|---|------|------|
| D1 | `Agent.run()` 是 async generator，yield `AgentEvent` | 消除旧版 agent loop 被写两遍的问题（BaseAgent.run vs MCPRunner.run_stream），天然支持流式 |
| D2 | 统一 `Tool` 抽象接口，MCP-first | 旧版 function call 为主、MCP 硬适配导致 MCPClients 继承 ToolCollection 的尴尬设计 |
| D3 | `MCPConnectionManager` 独立于工具注册，用 `async with` 管理生命周期 | 旧版 5 层嵌套 fire-and-forget cleanup + 新线程新 event loop 必须根除 |
| D4 | litellm 统一模型调用 | 旧版自写 767 行 LLM 类有大量重复；litellm 一行支持 OpenAI/DeepSeek/Claude/Ollama/vLLM |
| D5 | Agent 类层次最多 2 层（Agent + MCPAgent） | 旧版 4 层继承中 ReActAgent 几乎无价值，MCPAgent 破坏了父类抽象 |
| D6 | API 路由按功能拆分 + 公共中间件 | 旧版 app.py 1511 行单文件，5 处重复的文件上传逻辑 |
| D7 | Prompt 用模板文件管理，不内嵌 Python 字符串 | 旧版 service_packaging.py 1075 行的 f-string 不可维护 |
| D8 | dataclass 优先，Pydantic 仅用于需要校验的地方 | Agent 是运行时对象不需要序列化，简化依赖 |
| D9 | 配置支持 TOML 文件 + 环境变量覆盖 | 旧版只支持 TOML，部署不灵活 |
| D10 | next_step_prompt 不写入 memory，仅作为临时消息传给 LLM | 保持 memory 干净，旧版每步都往 memory 写 next_step_prompt 导致膨胀 |

---

## 项目结构

```
micro-agent-v2/
├── ROADMAP.md              # 本文件：开发路线图与协作规范
├── pyproject.toml          # 项目配置与依赖
├── .env.example            # 环境变量模板
├── config/
│   └── default.toml        # 默认配置
├── core/
│   ├── __init__.py
│   ├── config.py           # 配置加载（TOML + 环境变量）
│   ├── schema.py           # 核心数据模型（Message, Memory, AgentEvent, ToolCall）
│   ├── llm.py              # LLM 调用层（litellm 封装）
│   ├── agent.py            # Agent 基类（async generator 模式的 run loop）
│   ├── mcp_agent.py        # [Phase 2] MCPAgent（连接管理 + async with）
│   ├── meta_app_agent.py   # [Phase 4] MetaAppAgent（多服务编排）
│   ├── task.py             # [Phase 3] TaskManager（后台执行 + 事件 buffer）
│   ├── memory/             # [Phase 5] 可插拔记忆系统
│   │   ├── base.py         # MemoryProvider ABC
│   │   ├── short_term.py   # 内存短期记忆
│   │   └── persistent.py   # JSON 文件持久化
│   ├── skill/              # [Phase 5] 技能系统
│   │   └── base.py         # Skill + SkillRegistry + 目录发现
│   └── rag/                # [Phase 5/7] 检索增强
│       ├── base.py         # Retriever ABC + Document + SimpleRetriever
│       └── embedding.py    # [Phase 7] EmbeddingRetriever（litellm + numpy）
├── tool/
│   ├── __init__.py
│   ├── base.py             # Tool ABC + ToolResult
│   ├── registry.py         # ToolRegistry（工具注册中心）
│   ├── terminate.py        # Terminate 工具
│   ├── bash.py             # [Phase 2] Bash 工具
│   ├── simulated_mcp.py    # [Phase 4] 模拟 MCP 工具（元应用预览/验证）
│   ├── finalize.py         # [Phase 4] 元应用最终结果提交工具
│   └── mcp/                # [Phase 2] MCP 集成
│       ├── __init__.py
│       ├── connection.py   # MCPConnectionManager（async with 生命周期）
│       └── tool.py         # MCPTool 适配器
├── api/                    # [Phase 3] FastAPI 服务层
│   ├── __init__.py
│   ├── app.py              # FastAPI 实例 + 中间件
│   ├── deps.py             # 公共依赖（TaskManager 单例 + Agent 构建工厂）
│   ├── services/           # [Phase 6] 业务服务层
│   │   ├── files.py        # 文件处理（上传/解压/URL下载/ZIP打包/清理）
│   │   └── sse.py          # SSE 协议适配（event→legacy 格式 + cleanup 回调）
│   └── routes/
│       ├── __init__.py
│       ├── task.py         # 通用任务路由（提交/列表/状态/SSE/取消）
│       └── agent.py        # Agent 路由（11 个业务端点 + custom + tasks 列表）
├── task/                   # [Phase 4] 任务定义
│   ├── __init__.py
│   ├── base.py             # TaskConfig + Jinja2 渲染引擎 + 任务注册表
│   ├── builtin.py          # 内置任务注册（import 即注册）
│   └── templates/          # Prompt 模板文件（Jinja2）
│       ├── code_analysis.md.j2
│       ├── mcp_test.md.j2
│       ├── service_packaging.md.j2
│       ├── service_evaluation.md.j2
│       ├── mcp_service_recommendation.md.j2
│       ├── meta_app_validation.md.j2
│       ├── aml_report.md.j2
│       └── aml_model_evaluation.md.j2
└── tests/
    ├── __init__.py
    ├── test_smoke.py       # Phase 1 测试
    ├── test_mcp.py         # Phase 2 测试
    ├── test_api.py         # Phase 3 测试
    ├── test_task.py        # Phase 4 测试
    ├── test_meta_app.py    # Phase 4 测试（MetaApp + 兼容路由）
    └── test_phase5.py      # Phase 5 测试（profile/memory/skill/rag）
```

---

## 开发阶段

### Phase 1: 核心骨架 [DONE]

让 Agent loop 跑通：一个 Agent + 一个工具 + litellm 调用 + 流式输出。

| 任务 | 状态 | 文件 | 说明 |
|------|------|------|------|
| 项目配置 | ✅ done | `pyproject.toml`, `.env.example`, `config/default.toml` | 依赖声明、环境变量模板、默认配置 |
| 核心数据模型 | ✅ done | `core/schema.py` | Message, Memory, ToolCall, AgentEvent |
| 配置系统 | ✅ done | `core/config.py` | TOML + 环境变量覆盖 |
| LLM 层 | ✅ done | `core/llm.py` | litellm 封装，单方法接口 |
| Tool 接口 | ✅ done | `tool/base.py`, `tool/registry.py`, `tool/terminate.py` | Tool ABC, ToolRegistry, Terminate |
| Agent 基类 | ✅ done | `core/agent.py` | async generator 模式的 run loop |
| 冒烟测试 | ✅ done | `tests/test_smoke.py` | 验证 Agent + Terminate 能跑通 |

### Phase 2: MCP 接入 [DONE]

| 任务 | 状态 | 文件 | 说明 |
|------|------|------|------|
| Agent cancel 支持 | ✅ done | `core/agent.py` | cancel() + reset()，Phase 3 TaskManager 用 |
| MCPConnectionManager | ✅ done | `tool/mcp/connection.py` | async with 生命周期管理，替代旧版 5 层 fire-and-forget |
| MCPTool 适配器 | ✅ done | `tool/mcp/tool.py` | 远程 MCP 工具自动适配为 Tool 接口 |
| MCPAgent | ✅ done | `core/mcp_agent.py` | 不重写 run()，只增加 connect/disconnect + async with |
| Bash 工具迁移 | ✅ done | `tool/bash.py` | 从旧版移植 _BashSession，简化 Pydantic 依赖 |
| 测试 | ✅ done | `tests/test_mcp.py` | Bash + Registry namespace + MCPManager + MCPAgent + cancel |

### Phase 3: API 层 [DONE]

| 任务 | 状态 | 文件 | 说明 |
|------|------|------|------|
| TaskManager | ✅ done | `core/task.py` | 任务后台执行 + 事件 buffer + subscribe 订阅机制 |
| FastAPI 应用 | ✅ done | `api/app.py` | FastAPI 入口 + CORS 中间件 |
| 任务路由 | ✅ done | `api/routes/task.py` | POST 提交 / GET 列表 / GET 状态 / GET SSE 流 / POST 取消 |
| SSE 断线续传 | ✅ done | `api/routes/task.py` | 支持 Last-Event-ID header |
| 公共依赖 | ✅ done | `api/deps.py` | TaskManager 单例 + Agent 构建工厂 |
| 测试 | ✅ done | `tests/test_api.py` | TaskContext 订阅 + 实时推送 + cancel + FastAPI 导入 |
| 前端接口兼容 | ✅ done | `api/routes/agent.py` | Phase 6 完成：旧版 Form/File 签名 + SSE 格式适配，已合并入 agent 路由 |

### Phase 4: 任务迁移 [DONE]

| 任务 | 状态 | 文件 | 说明 |
|------|------|------|------|
| TaskConfig + 注册表 | ✅ done | `task/base.py` | TaskConfig dataclass + Jinja2 渲染 + register/get/list API |
| Prompt 模板化 | ✅ done | `task/templates/*.md.j2` | 8 个模板（+aml_report / aml_model_evaluation） |
| 内置任务注册 | ✅ done | `task/builtin.py` | import 即注册 8 个预定义任务 |
| Agent 路由 | ✅ done | `api/routes/agent.py` | 11 个业务端点 + POST /custom + GET /tasks |
| SimulatedMCPTool | ✅ done | `tool/simulated_mcp.py` | 模拟 MCP 工具，元应用预览/验证场景 |
| FinalizeResult | ✅ done | `tool/finalize.py` | Agent 提交元应用最终结果的工具 |
| MetaAppAgent | ✅ done | `core/meta_app_agent.py` | 基于 MCPAgent，支持 sim/real 双模式，配置化初始化 |
| 前端兼容路由 | ✅ done | `api/routes/agent.py` | 旧版 Form/File 签名 + 旧版 SSE 事件格式适配（已合并） |
| 测试 | ✅ done | `tests/test_task.py`, `tests/test_meta_app.py` | 33 个测试全通过 |

### Phase 5: 增强能力 [DONE]

| 任务 | 状态 | 文件 | 说明 |
|------|------|------|------|
| 多 LLM profile | ✅ done | `core/config.py`, `config/default.toml` | [llm.default]/[llm.fast]/[llm.reasoning]，build_agent 选 profile |
| Memory 可插拔 | ✅ done | `core/memory/` | MemoryProvider ABC + ShortTermMemory + FileMemory（JSON 持久化） |
| Skill 系统 | ✅ done | `core/skill/` | Skill 定义 + SkillRegistry + 目录自动发现 + Agent.load_skill() |
| RAG 接口 | ✅ done | `core/rag/` | Retriever ABC + Document + SimpleRetriever + Agent._think() 自动注入 |
| 测试 | ✅ done | `tests/test_phase5.py` | 18 个测试：profile/memory/skill/rag 全覆盖 |

### Phase 6: 接口层分层重构 + 端点补齐 [DONE]

| 任务 | 状态 | 文件 | 说明 |
|------|------|------|------|
| FileService | ✅ done | `api/services/files.py` | 统一文件处理：上传/解压/URL下载/ZIP打包/清理 |
| SSE 协议适配 | ✅ done | `api/services/sse.py` | event→legacy 格式转换 + cleanup 回调 + output_files 读取 + ZIP base64 返回 |
| 缺失端点补齐 | ✅ done | `api/routes/agent.py` | 补齐 mcp_service_recommendation / meta_app_validation / aml_report / aml_model_evaluation |
| 缺失模板补齐 | ✅ done | `task/templates/` | 新增 aml_report.md.j2 / aml_model_evaluation.md.j2 |
| 路由薄化 | ✅ done | `api/routes/agent.py` | 路由只做参数解析，业务逻辑委托 services |
| deps 精简 | ✅ done | `api/deps.py` | 移除已迁移到 services 的文件处理逻辑 |
| 临时文件清理 | ✅ done | `api/services/files.py` | cleanup_paths() + SSE response finally 回调 |
| 最终结果格式 | ✅ done | `api/services/sse.py` | is_final_result + final_results 字段，兼容旧版前端 |

### Phase 7: 垂域智能体增强（Skills + RAG + Session Memory） [DONE]

| 任务 | 状态 | 文件 | 说明 |
|------|------|------|------|
| EmbeddingRetriever | ✅ done | `core/rag/embedding.py` | litellm embedding + numpy 余弦相似度，支持目录批量加载和自动分块 |
| 配置扩展 | ✅ done | `core/config.py`, `config/default.toml` | 新增 MemoryConfig / RAGConfig / SkillsConfig |
| Session Memory 框架 | ✅ done | `api/deps.py`, `core/task.py`, `api/services/sse.py` | build_agent 支持 enable_session/session_id，TaskManager 自动 persist，SSE header 返回 session_id |
| Skill 自动发现 | ✅ done | `api/app.py` | lifespan startup 时扫描 skills 目录 |
| 领域 Skill | ✅ done | `workspace/skills/` | mcp_protocol / docker_packaging / code_analysis_patterns 三个 SKILL.md |
| 领域知识库 | ✅ done | `workspace/knowledge/service_packaging/` | MCP 规范 / FastMCP 用法 / Flask封装案例 / CLI封装案例 / 常见错误 / IoEB约定 |
| service_packaging 增强 | ✅ done | `api/routes/agent.py` | 接入 session memory + skills + RAG retriever |
| build_agent 升级 | ✅ done | `api/deps.py` | async 函数，返回 (agent, session_id)，支持 skills/retriever/session |
| 测试 | ✅ done | `tests/test_phase7.py` | 16 个测试：embedding/config/skill/session/persist/endpoints |

---

## 核心接口契约

以下接口是框架的骨干，修改需谨慎。

### Agent.run() — async generator

```python
async def run(self, request: str) -> AsyncIterator[AgentEvent]:
    """主循环，yield 事件流。消费方式：
    
    # 流式消费（SSE 端点）
    async for event in agent.run(prompt):
        yield event.to_sse()
    
    # 批量消费（测试/CLI）
    results = [event async for event in agent.run(prompt)]
    """
```

### AgentEvent — 事件类型

```python
@dataclass
class AgentEvent:
    type: "think" | "tool_call" | "tool_result" | "error" | "done"
    step: int
    data: dict
    timestamp: float
```

前端 AgentExecutionPanel 应按 `type` 字段分类展示。

### Tool — 统一工具接口

```python
class Tool(ABC):
    name: str
    description: str
    parameters: dict  # JSON Schema
    
    async def execute(self, **kwargs) -> ToolResult
```

所有工具（本地/MCP 远程）实现此接口。ToolRegistry 统一管理注册。

### LLM — 单方法接口

```python
class LLM:
    async def complete(self, messages, tools=None, tool_choice="auto") -> LLMResponse
```

通过 litellm 支持所有主流模型。model 名称格式：`provider/model`（如 `deepseek/deepseek-chat`、`openai/gpt-4o`、`ollama/qwen2.5`）。

---

## 旧版 → 新版映射

| 旧版文件 | 新版对应 | 处理方式 |
|----------|----------|----------|
| `app/agent/base.py` | `core/agent.py` | 重写，保留状态机和 stuck 检测概念 |
| `app/agent/react.py` | 删除 | 合并入 Agent 基类 |
| `app/agent/toolcall.py` | `core/agent.py` | act/execute_tool 逻辑并入 Agent |
| `app/agent/mcp.py` | `core/mcp_agent.py` [Phase 2] | 重写，cleanup 用 async with |
| `app/agent/meta_app.py` | `core/meta_app_agent.py` | 重写，sim/real 双模式，复用 Agent.run() |
| `app/tool/mcp_sim.py` | `tool/simulated_mcp.py` | 迁移为 Tool dataclass |
| `app/tool/finalize_meta_result.py` | `tool/finalize.py` | 迁移为 Tool dataclass |
| `run_meta_app.py` (70行) | 删除 | MetaAppAgent 直接用 TaskManager 执行 |
| `app/schema.py` | `core/schema.py` | 重写，dataclass 替代 Pydantic |
| `app/llm.py` (767行) | `core/llm.py` (~70行) | 重写，litellm 替代手写 |
| `app/config.py` | `core/config.py` | 重写，加环境变量覆盖 |
| `app/tool/base.py` | `tool/base.py` | 简化 |
| `app/tool/tool_collection.py` | `tool/registry.py` | 重写 |
| `app/tool/mcp.py` (342行) | `tool/mcp/` [Phase 2] | 拆分为 connection + tool |
| `app/tool/terminate.py` | `tool/terminate.py` | 简化 |
| `app/tool/bash.py` | `tool/bash.py` [Phase 2] | 移植 _BashSession |
| `app.py` (1511行) | `api/` [Phase 3] | 拆分为路由模块 |
| `run_mcp.py` (461行) | 删除 | Agent.run() 天然流式，不需要 Runner |
| `app/task/*.py` | `task/` [Phase 4] | Prompt 模板化 |
| `app/logger.py` (98行) | 直接 `from loguru import logger` | 删除无意义包装 |

---

## 开发环境设置

```bash
cd micro-agent-v2
pip install -e ".[dev]"
cp .env.example .env
# 编辑 .env 填入 API Key
python tests/test_smoke.py
```

---

## Agent 交接须知

1. **开始工作前**：读本文件，了解当前进度和架构约定
2. **开发过程中**：完成一个任务后，更新本文件对应任务的状态
3. **结束工作时**：在下方「开发日志」追加一条记录，说明做了什么、遇到什么问题
4. **不确定的决策**：记录在「待讨论」区域，不要自行决定

---

## 开发日志

| 日期 | 操作人 | 内容 |
|------|--------|------|
| 2026-04-04 | Agent (首次搭建) | Phase 1 核心骨架：项目配置、schema、config、llm、tool、agent 基类 |
| 2026-04-04 | Agent (续) | Phase 2 MCP 接入：MCPConnectionManager、MCPTool、MCPAgent、Bash、cancel 机制 |
| 2026-04-04 | Agent (续) | Phase 3 API 层：TaskManager、FastAPI 路由、SSE 断线续传、任务取消 |
| 2026-04-04 | Agent (续) | Phase 4 任务迁移：TaskConfig + Jinja2 模板引擎、6 个 Prompt 模板、builtin 注册、Agent 路由 |
| 2026-04-04 | Agent (续) | Phase 4 续：MetaAppAgent（sim/real 双模式）、SimulatedMCPTool、FinalizeResult、前端兼容路由 |
| 2026-04-04 | Agent (续) | Phase 5 增强：多 LLM profile、Memory 可插拔（ShortTerm + File）、Skill 系统、RAG 接口 |
| 2026-04-04 | Agent (续) | Phase 6 接口层重构：提取 services/files.py + services/sse.py，补齐 4 个缺失端点（aml_report / aml_model_evaluation / mcp_service_recommendation / meta_app_validation），加入临时文件清理和旧版 SSE 格式兼容 |
| 2026-04-04 | Agent (续) | Phase 7 垂域智能体增强：EmbeddingRetriever（向量检索）、Session Memory（跨调用记忆）、Skill 自动发现、3 个领域 Skill + 知识库文档、service_packaging 端点串联所有组件 |

---

## 待讨论

- [ ] Phase 2 的 MCPConnectionManager 是否需要支持连接池/自动重连？
- [ ] Phase 3 API 层是否需要认证机制？
- [ ] Phase 3 TaskManager 的事件 buffer 是否需要持久化（Redis）？当前为内存存储，重启丢失
- [ ] Phase 5 Memory 持久化用什么向量数据库？（Milvus / ChromaDB / FAISS）
- [ ] 是否需要支持 LLM 响应的 token 级别流式（当前只支持 step 级别流式）？
- [ ] 是否需要支持多用户同时观看同一任务进度（当前 TaskContext.subscribe 支持多订阅者）？
