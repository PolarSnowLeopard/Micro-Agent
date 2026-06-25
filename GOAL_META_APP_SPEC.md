# 当前目标：元应用想定式仿真构建下一阶段

更新：2026-06-21。本文只记录当前代码已证明的状态、未闭合缺口和后续实验计划；项目长期记忆放在本地 `.codex/project-memory.md`，不入库。

## 一、真实性确认

“已超越最小闭环”不能只按目标口径判断，必须由当前代码和已执行验证支撑。按当前 `lyx` 分支代码，结论是：

> 工程上已形成可运行的最小真实闭环；科研上已形成实验入口和 baseline runner 雏形，但尚未形成有效批量对比实验。因此只能说“已超过原始最小打通目标”，不能说“研究闭环已经充分验证”。

### 1. 当前代码直接证明的能力

| 能力 | 当前证据 | 判断 |
| --- | --- | --- |
| 平台入口 | `api/routes/simulation.py` 提供 `/api/simulation/start`、`/{buildId}/stream`；ioeb `src/api/simulation_builder.js` 直连 `VUE_APP_AGENT_BASE_URL` | 已实现 |
| LLM 服务选择 | `SimulationOrchestrator._select_services_from_catalog()` 对传入 catalog 做 LLM 选择，失败回退 `serviceIds/catalog` | 已实现，但不是后端服务池检索 |
| 慢模式 | `SimulationOrchestrator._phase_intelligent_build()` 调用既有 `Agent.run()`，保持 ReAct/tool-calling 探索 | 已实现 |
| 真实 MCP 调用记录 | `LoggingMCPTool` 记录 real MCP 调用；`tool_call_record` 带 `source/phase/purpose/iteration/action_id` | 已实现 |
| Verifier 裁判 | 构建循环中 `_stream_verification()` 决定是否进入成功分支；AcceptedTrajectory 只取最终 PASSED iteration | 已实现 |
| BuildBundle | `BuildBundleStore.save_from_trace()` 写单 build 目录：trace、service selection、accepted trajectory、artifact、frontend state、manifest | 已实现 |
| 产物分层 | `artifact_compiler.py` 分离 ServiceSelectionReport、AcceptedTrajectory、MetaAppArtifact | 已实现 |
| 最小 MetaAppArtifact | `meta_app_artifact.v1` 只保留运行必要结构，不含 trace/evidence/verifier/serviceSelection | 已实现 |
| GoldenPath replay | `artifact_runtime.run_artifact()` 先尝试 GoldenPath，失败后回退慢模式 | 已实现 |
| 本地实验入口 | `experiments.py` 提供 `no_reuse/raw_trace_prompt/workflow_memory/golden_path` runner | 已实现入口 |
| ioeb 临时展示 | `simulation_builder.vue` 可展示 trace/evidence summary/artifact JSON 摘要 | 已实现临时展示 |

### 2. 已验证样例支撑的能力

以下来自此前真实本地 smoke run，不等价于批量实验结论：

- Build ID: `build-c731a074a75e`
- 服务：`medical-calc`，SSE 地址 `http://127.0.0.1:18000/sse`
- 构建过程：第一轮 Verifier FAILED，第二轮修正后 PASSED
- 产物：`meta_app_artifact.v1`，含 1 条 primary GoldenPath
- replay：4 次真实 MCP 调用，约 3.2s，`fastPathSuccess=true`，`fallbackUsed=false`
- 实验：`golden_path` 单任务跑通，`taskSuccess=true`，`verifierPassed=true`

### 3. 当前不能宣称的能力

- 不能宣称已完成后端服务池自动发现/语义检索；当前只在请求传入的 catalog 内选择。
- 不能宣称 CoW 沙箱已经实现；当前只有真实 MCP 与 demo fake MCP/SandboxTool 双通道。
- 不能宣称所有 baseline 已有效对比；当前 runner 存在，但只 smoke tested `golden_path`。
- 不能宣称 GoldenPath 泛化能力已验证；当前 `argumentTemplate` 主要支持重放与轻量槽位覆盖。
- 不能宣称 complete 事件后 Bundle 必定立即稳定可读；当前保存发生在 SSE generator `finally` 中，前端需要重试读取。

## 二、当前必须补齐的工程缺口

### P0：平台最小可用性补强

- 将 SSE `complete` 与 BuildBundle 保存时序收紧，或把 `artifact_ready`/轮询状态写清楚。当前前端靠重试规避竞态。
- 为 `/run` 构造失败用例，验证 GoldenPath 失败后自动 fallback 慢模式确实可用。
- 为 service binding 增加更可靠的 schema/version/hash 来源；当前 `schemaHash` 主要由 artifact compiler 基于工具列表计算。
- 补齐 token usage、LLM call count、成本统计；当前实验字段可能为 `null`。
- 标记 demo/fake MCP 运行的 `researchEligible=false`，避免演示成功被当成真实实验成功。

### P0：科研实验最小可用补强

- 固定 3-5 个真实 MCP 任务集，至少覆盖 AKI、sepsis、GI bleed、pre-op risk、ICU delirium 等 medical-calc 场景。
- 在同一任务集上跑通 `no_reuse`、`raw_trace_prompt`、`workflow_memory`、`golden_path`。
- 输出 JSONL/CSV 汇总，字段至少包括 success、latency、mcp_call_count、fallback_used、verifier_passed、error_type。
- 明确源任务/目标任务划分，否则无法验证 reuse/applicability。
- 稳定 Eval-time Verifier prompt/schema，并记录模型、时间、失败类型。

### P1：GoldenPath 与数据流

- 当前 GoldenPath 的 `argumentTemplate` 能支撑最小 replay，但动态输入泛化较弱。
- BindingPlan 只做轻量槽位绑定，缺少强可执行数据流图。
- L2 断言需要从“参数存在/已绑定”扩展到“参数来自 runtime slot、step output 或白名单 transform”。
- 多工具/多服务路径的 `dependsOn` 目前主要按顺序归纳，需要从真实数据依赖中归纳。
- observation 失败判定已覆盖 `success=false/all_success=false/error`，但更多 MCP 返回形态需要统一。

### P1：服务选择与服务契约

- 当前服务选择只消费前端/请求传入 catalog；尚未连接 ioeb_backend 服务池检索。
- 需要在服务封装/服务池层标准化 `service_id`、`tool_name`、`tool_key`、input schema、output schema、version/hash、source。
- 服务选择解释必须保持为 build-time data，不进入 final artifact。
- 正式平台复现需要 ioeb_backend 增加服务契约、schema version/hash 字段。

### P1：安全与隐私

- 医疗场景中的 scenario、arguments、result 可能含患者信息。
- BuildTrace、AcceptedTrajectory、experiment result 只应保存在 MicroAgent 本地科研数据中，不进 artifact、不进 ioeb_backend、不进 git。
- 后续需要脱敏规则、访问控制、保存周期和实验导出过滤。

### P2：正式平台化断点

以下需要修改 ioeb_backend/数据库，本阶段不做：

- MetaAppArtifact 正式入库。
- BuildBundle 索引入库。
- 平台发布链路携带 artifact。
- 元应用列表长期恢复 GoldenPath。
- 服务池正式管理标准化 MCP schema/hash/version。

科研实验结果任何版本都不应写入 ioeb_backend。

## 三、下一阶段计划

### Step 1：稳定最小真实流程

目标：同一套真实 MCP 服务和 3-5 个任务能重复构建、运行、实验。

- 构造 medical-calc 任务集。
- 每个任务记录 expected tools、expected params、expected bounded output traits。
- 批量运行 build，检查每个 build 是否生成 artifact/goldenPath。
- 批量运行 `/run`，记录 fast/fallback/slow 结果。
- 修复 determinism、bundle timing、MCP observation parsing 问题。

### Step 2：补齐 baseline 实验

目标：让四类 baseline 在相同任务集上可比。

- `no_reuse`：只用慢模式，不注入历史材料。
- `raw_trace_prompt`：注入原始成功轨迹片段。
- `workflow_memory`：注入从 AcceptedTrajectory/GoldenPath 归纳出的工作流记忆。
- `golden_path`：使用 MetaAppArtifact 内部 GoldenPath，失败回退慢模式。

### Step 3：做第一轮消融

目标：证明不是“字段更多所以看起来更好”。

- 无 GoldenPath，仅慢模式。
- GoldenPath 无 `argumentTemplate`。
- GoldenPath 有模板但无 observation semantic failure 判定。
- GoldenPath 有模板但无 Eval-time Verifier。
- Workflow memory vs executable artifact。
- 不同 applicability prompt / BindingPlan prompt。

### Step 4：扩展数据流与断言

目标：让 GoldenPath 从“可 replay”走向“可泛化 replay”。

- 显式记录 slot 来源、step output 来源、transform。
- 引入可执行 L2 assertion。
- 将工具 schema 转换为参数绑定约束。
- 增加多服务路径的 output dependency 检测。

### Step 5：论文实验设计

大论文：元应用想定式仿真构建方法及系统。

- 贡献 1：从想定到元应用产物的 LLM+MCP 构建链路。
- 贡献 2：BuildTrace/AcceptedTrajectory/MetaAppArtifact 分层对象模型。
- 贡献 3：快慢模式运行与可复用 GoldenPath。
- 贡献 4：平台展示与科研实验双入口系统。

小论文：轨迹固化、复用、优化。

- 研究问题：何时可以从成功 ReAct 轨迹固化为可执行 artifact？
- 方法：verified executable artifact（服务绑定、参数模板、断言、适用条件、回退策略）。
- 对照：no reuse、raw trajectory retrieval、ReAct exemplar、reflection/workflow memory、golden path/executable artifact。
- 指标：成功率、延迟、成本、MCP 调用数、fallback 率、Verifier 通过率、错误类型。

## 四、当前文档分工

- `.codex/project-memory.md`：Codex 本地项目记忆、工作方式、设计约定，不入库。
- `GOAL_META_APP_SPEC.md`：当前阶段目标、代码真实性确认、缺口、计划、实验设计。
- `docs/data-structures-spec.md`：BuildBundle / BuildTrace / ServiceSelectionReport / AcceptedTrajectory / MetaAppArtifact / experiment 的结构规格。
- `docs/frontend-simulation-integration.md`：ioeb 临时展示和 API 对接说明。
- `docs/simulation-build-roadmap.md`：较短路线图和能力概览。
- `workspace/RECON_META_APP_READ_WRITE_CHAIN.md`：ioeb/ioeb_backend 读写链与后端断点。
- `dev/simulation/headless_build.py`：基于 service_catalog 的 headless BuildBundle 验收脚本（开发期手动，非 CI）。
