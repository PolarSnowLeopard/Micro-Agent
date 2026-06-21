# 当前目标：元应用想定式仿真构建下一阶段

更新：2026-06-21。

## 当前状态判断

第一阶段“最小闭环”已经达到并超过原始目标：

- 平台入口已能通过 ioeb 调用 MicroAgent 仿真构建接口。
- MicroAgent 已能用 LLM 在给定 MCP 服务池内做服务选择。
- 慢模式保持 ReAct/tool-calling，真实调用 MCP 服务。
- Verifier 能作为构建期最终裁判。
- BuildBundle 已按单 build 目录落盘。
- AcceptedTrajectory、ServiceSelectionReport、MetaAppArtifact 已分离。
- MetaAppArtifact v1 已能作为最小元应用产物被本地运行。
- GoldenPath replay 已真实跑通，并能进入科研 experiment runner。
- ioeb 已有临时 JSON/摘要展示，不依赖后端改库。

因此，本文件不再记录“项目理解/操作记忆”，只记录接下来必须解决的缺口、后续计划和实验设计。

## 已验证基线

真实验证样例：

- Build ID: `build-c731a074a75e`
- 服务：`medical-calc` (`http://127.0.0.1:18000/sse`)
- 构建过程：第一轮 Verifier FAILED，第二轮修正后 PASSED
- 产物：`meta_app_artifact.v1`，含 1 条 primary GoldenPath
- replay：4 次真实 MCP 调用，约 3.2s，`fastPathSuccess=true`，`fallbackUsed=false`
- 实验：`golden_path` baseline 单任务跑通，`taskSuccess=true`，`verifierPassed=true`

## 当前必须补齐的工程缺口

### P0：真实平台可用性补强

- SSE complete 与 BuildBundle 写入时序仍需更严格验证：前端收到完成后，所有 bundle 文件必须稳定可读。
- 前端展示目前是临时 JSON/摘要面板，只能证明存在性；正式 UI 后续需要重新设计，但本阶段不要过度耦合。
- `/run` 快路径成功路径已验证，但快路径失败后自动 fallback 慢模式还需要专门构造失败用例验证。
- `serviceContracts` 仍主要来自请求侧 `servicesMeta` 与观察到的工具调用；缺正式 schema hash/version 管理。
- token usage、LLM call count、成本统计目前不完整，实验指标中可能为 null。

### P0：科研实验最小可用补强

- 当前只 smoke tested `golden_path` baseline；`no_reuse`、`raw_trace_prompt`、`workflow_memory` 需要同一任务集批量跑通。
- 需要固定任务集格式和源任务/目标任务划分，否则无法检验 reuse/applicability。
- 需要批量任务运行脚本和结果汇总表，至少输出 CSV/JSONL 方便论文画表。
- Eval-time Verifier 需要稳定 prompt 与输出 schema，并记录模型、时间、失败类型。
- 需要将 demo/fake MCP case 明确排除出 researchEligible 统计。

### P1：GoldenPath 与数据流

- 当前 GoldenPath 依赖 `argumentTemplate` 保留最终成功参数；这是可运行的最小实现，但动态输入泛化较弱。
- BindingPlan 只做轻量槽位绑定，缺少强可执行数据流图。
- L2 断言需要从“参数存在”扩展到“参数来自 runtime slot / step output / whitelist transform”。
- 多工具/多服务路径的 `dependsOn` 和 output slot 应从真实数据依赖中归纳，而不是只按顺序。
- observation 内部业务失败已可判定，但更多 MCP 返回形态需要扩展统一判定规则。

### P1：服务选择与服务契约

- 当前服务选择只在前端/请求传入的已知 catalog 内做 LLM 选择；尚未连接后端服务池检索。
- 需要标准化服务描述字段：`service_id`、`tool_name`、`tool_key=service_id:tool_name`、input schema、output schema、version/hash、source。
- 服务匹配解释应保留为 build-time data，不进入 artifact。
- 后续若要正式平台复现，需要 ioeb_backend 增加服务契约与 schema version/hash 字段。

### P1：安全与隐私

- 医疗场景中的 scenario、arguments、result 可能含患者信息。
- BuildTrace/AcceptedTrajectory/experiment result 应保持本地科研数据，不进 artifact、不进后端。
- 后续需要脱敏规则、访问控制、保存周期和实验导出过滤。

### P2：正式平台化断点

这些需要修改 ioeb_backend/数据库，本阶段不做：

- MetaAppArtifact 正式入库。
- BuildBundle 索引入库。
- 平台发布链路携带 artifact。
- 元应用列表长期恢复 GoldenPath。
- 服务池正式管理标准化 MCP schema/hash/version。

科研实验结果任何版本都不应写入 ioeb_backend。

## 下一阶段计划

### Step 1：稳定最小真实流程

目标：同一套真实 MCP 服务和 3-5 个任务能重复构建、运行、实验。

- 构造 medical-calc 任务集：AKI、sepsis、GI bleed、pre-op risk、ICU delirium。
- 每个任务记录 expected tools、expected params、expected bounded output traits。
- 批量运行 build，检查每个 build 是否生成 artifact/goldenPath。
- 批量运行 `/run`，记录 fast/fallback/slow 结果。
- 修复所有 determinism、bundle timing、MCP observation parsing 问题。

### Step 2：补齐 baseline 实验

目标：让四类 baseline 在相同任务集上可比。

- `no_reuse`：只用慢模式，不注入历史材料。
- `raw_trace_prompt`：检索并注入原始成功 trace 摘要/片段。
- `workflow_memory`：注入从 AcceptedTrajectory 归纳的自然语言/半结构化 workflow。
- `golden_path`：使用 MetaAppArtifact 内部 GoldenPath，失败回退慢模式。

统一记录：

- task_success
- fast_path_success
- fallback_success
- overall_success
- fallback_used/fallback_rate
- latency_ms
- llm_call_count
- mcp_call_count
- token_usage
- planner_iterations
- verifier_passed
- error_type

### Step 3：做第一轮消融

目标：证明不是“字段更多所以看起来更好”。

候选消融：

- 无 GoldenPath，仅慢模式。
- GoldenPath 无 `argumentTemplate`。
- GoldenPath 有模板但无 observation semantic failure 判定。
- GoldenPath 有模板但无 Verifier eval。
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

## 当前文档分工

- `.codex/project-memory.md`：Codex 本地项目记忆、工作方式、设计约定，不入库。
- `GOAL_META_APP_SPEC.md`：当前阶段目标、缺口、计划、实验设计。
- `docs/data-structures-spec.md`：BuildBundle / BuildTrace / ServiceSelectionReport / AcceptedTrajectory / MetaAppArtifact / experiment 的结构规格。
- `docs/frontend-simulation-integration.md`：ioeb 临时展示和 API 对接说明。
- `docs/simulation-build-roadmap.md`：较短路线图和能力概览。
- `workspace/RECON_META_APP_READ_WRITE_CHAIN.md`：ioeb/ioeb_backend 读写链与后端断点。
- `trace_evidence/README.md`：旧 evidence pipeline 说明，只作为 legacy diagnostic 工具。
