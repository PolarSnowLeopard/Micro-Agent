# Trace Evidence Infrastructure v1 Audit Report

> **开发中间产物**：不入库（见仓库根 `.gitignore`）；能力稳定后以 `trace_evidence/README.md` 为对外说明。

**Audit date**: 2026-06-06  
**Auditor mode**: read-only (no source changes)  
**Repository**: `Micro-Agent` @ `lyx` branch (commits `53a1194` → `5b8821a` → `24b9f9f`)  
**Unit tests**: `python -m unittest discover -s trace_evidence/tests` → **166 OK**

---

## 1. Verdict

**PASS_WITH_WARNINGS**

v1 的核心目标——在 **完整跑完的仿真** 上，用结构化事件（`tool_call_record` / `planner_decision` / `verifier_result`）驱动 evidence 管线，而非 log 正则回退——**已基本达成**。真实 MCP trace 的 tool envelope 相较 P0 有明显提升（`call_id`、完整 `arguments`/`result`、`channel`、`latency_ms` 等已落盘）。

但审计发现三类保留问题：(1) **未完成/失败的 run** 不会 emit planner/verifier，checker 正确 WARN，不能代表“任意 run 均可复盘”；(2) **Orchestrator 被修改**（事件发射 + verifier prompt），属必要耦合但超出纯后处理范围；(3) `planner_decision.reason` 仍是 Agent 自然语言摘要，**非纯结构化决策 trace**，存在与 chain-of-thought 边界模糊的风险。因此不能评为全量 PASS。

---

## 2. Changed Files Review

### 主要改动文件（v1 区间 `4ee5f55..HEAD`）

| 区域 | 文件 | 作用 |
|------|------|------|
| 运行时落盘 | `micro_agent/simulation/trace_records.py` | 统一 `tool_call_record` 事件 + `metadata.trace_version=v1.0.0` |
| 运行时落盘 | `micro_agent/simulation/logging_mcp_tool.py` | 真实 MCP 调用写入 v1 字段 |
| 运行时落盘 | `micro_agent/simulation/sandbox_tool.py` | Sandbox 调用写入 v1 字段 |
| **Orchestrator** | `micro_agent/simulation/orchestrator.py` | 发射 `planner_decision` / `verifier_result`；verifier prompt 调整 |
| 路由 | `api/routes/simulation.py` | 落盘时合并 `tool_call_record` 事件（小改） |
| 后处理 | `trace_evidence/trace_adapter.py` | **仅**读 v1 结构化事件；拒绝非 v1 trace |
| 后处理 | `trace_evidence/evidence_card.py` | 卡片渲染 + provenance |
| 后处理 | `trace_evidence/evidence_checker.py` | 21 项检查（含 data/logic category） |
| 后处理 | `trace_evidence/config_attachment.py` | `execution_evidence` 槽位 |
| 采集 | `trace_evidence/headless_run.py` | 无 UI 跑仿真并落盘 |
| 测试/夹具 | `trace_evidence/tests/*`, `fixtures/minimal_v1_trace.json` | 166 项单测 |

### 是否越界？

| 问题 | 结论 |
|------|------|
| 主要集中在 trace 持久化、adapter、checker、config attachment？ | **是**（~7k 行新增在 `trace_evidence/`） |
| 误改 Orchestrator 主流程 / Planner 策略 / MCP 行为？ | **部分**。Orchestrator **有改**：新增结构化事件发射、verifier prompt 注入 `scenarioDescription`；**未**改 Planner 核心策略或 MCP 连接逻辑。属 **audit warning：必要耦合，非纯后处理** |
| 引入 `minIterations=2`、假多轮、硬编码 case？ | **否（在 trace_evidence 内）**。`minIterations` 为 orchestrator **既有** strategy 字段；测试夹具 `minimal_v1_trace.json` 仅用于单测，不写入生产 trace |
| 伪造 evidence？ | **未发现**。adapter 明确标注 `confidence`；channel 可从 `unknown` **推断**为 `mcp`（`_enrich_tool_call_channels`），非伪造 |

---

## 3. Smoke Run Result

### 已执行命令

```bash
cd Micro-Agent
.venv/bin/python -m unittest discover -s trace_evidence/tests -p "test_*.py" -q
# → Ran 166 tests in 1.538s — OK

.venv/bin/python trace_evidence/run_pipeline.py workspace/data/traces/sim-c407cb5becbf.json -o trace_evidence/output_audit
# → Overall: PASS

.venv/bin/python trace_evidence/run_pipeline.py workspace/data/traces/sim-5ccbc720f0e0.json -o trace_evidence/output_audit_mcp_fail
# → Overall: WARN (no planner/verifier — build 未完成)

.venv/bin/python trace_evidence/run_pipeline.py workspace/data/traces/sim-2baa8874b389.json -o trace_evidence/output_audit_sandbox
# → Overall: WARN (同上)

.venv/bin/python trace_evidence/run_pipeline.py workspace/data/traces/sim-headless-e842553ed843.json -o trace_evidence/output_audit_headless_new
# → Overall: WARN (final_result success=False)
```

### 未能完整执行

| 命令 | 结果 |
|------|------|
| `trace_evidence/headless_run.py --help` | 误触发完整 headless run（无 `--help`），运行 ~86s 后 **exit 120**（被 `head -20` 截断）；仍落盘 `sim-headless-e842553ed843.json` |
| `trace_evidence/scripts/http_mcp_smoke.py` | **未跑**（需 9017 + 25013 MCP；9017 当前可用但未执行端到端脚本） |
| 历史 trace `sim-c407cb5becbf.json` | 管道 PASS，但文件已从 `workspace/data/traces/` 移除；产物见 `output_audit/` |

### 产物位置（本轮审计生成）

| Case | 产物目录 |
|------|----------|
| HTTP 冒烟成功样例（历史 trace 重放） | `trace_evidence/output_audit/` |
| 5 服务 MCP 未完成 run | `trace_evidence/output_audit_mcp_fail/` |
| 单服务 sandbox/未完成 | `trace_evidence/output_audit_sandbox/` |
| 新鲜 headless 3 服务 | `trace_evidence/output_audit_headless_new/` |
| 历史 headless PASS 样例 | `trace_evidence/output_headless/` |

---

## 4. Artifact Evidence Matrix

| 验收项 | 当前状态 | 证据文件/字段 | 结论 |
|--------|----------|---------------|------|
| tool_calls 落盘 | **PASS**（完整 run）/ **WARN**（中断 run 仅有 partial records） | Trace: `events[].type=="tool_call_record"`；例 `sim-5ccbc720f0e0.json` 6 条；`sim-headless-e842553ed843.json` 18 条 | 结构化事件已落盘；bundle 内拆为 call/return 对（adapter 生成） |
| tool_call 参数/结果 | **PASS** | `tool_call_record.data.arguments`, `.result`, `.result_hash`, `.success`, `.error`, `.latency_ms` | 真实 MCP 样例含完整 JSON args/result（见 `sim-5ccbc720f0e0.json` call-ac3b00ac6f8f） |
| channel 区分 | **PASS** | `.channel`: `real_mcp` / `sandbox` / `in_process`；checker `channel_classification` PASS | 同一 trace 内 sandbox+real_mcp 可区分 |
| verifier_result 结构化 | **PASS**（到达 verify 阶段）/ **MISSING**（未到达） | `events[].type=="verifier_result"`；`status`, `summary`, `checks[].evidence_refs` | 完整 run 有；`sim-5ccbc720f0e0` 无 verifier 事件 |
| planner_decision | **PASS**（到达规划轮次）/ **MISSING**（中断） | `planner_decision.data`: `candidate_tools`, `selected_tools`, `executionPath`, `dispatch`, `tool_call_details` | 结构化字段齐全；`reason` 为 Agent 文本摘要（≤200 字） |
| executionEvidence 挂载 | **PASS** | `config_attachment_draft.json` → `execution_evidence` | 含 `traceSessionId`, `evidenceId`, `dispatchSequence`, `integrity`；**无** `evidence_card_path` 字段 |
| checker 有效性 | **PASS_WITH_WARNINGS** | 21 checks；完整 run PASS；失败/中断 run WARN/MISSING | 能区分质量档位；`verification_presence` 缺事件时标 MISSING（文案仍提 log，已过时） |

---

## 5. Evidence Source Classification

### original / persisted（运行时落盘）

- `tool_call_record` 全字段（`micro_agent/simulation/trace_records.build_tool_call_record_events`）
- `planner_decision` / `verifier_result` 事件（`orchestrator.py` yield）
- `service` / `phase` / `iteration` / `complete` 事件
- `metadata.trace_version = "v1.0.0"`, `tool_call_count`, `config_snapshot`

### adapter-generated（后处理）

- Bundle 内 `tool_calls` 的 **call/return 拆分**（单条 `tool_call_record` → 两条 `ToolCallEvidence`）
- `dispatch_sequence`（从 tool_calls 顺序去重）
- `execution_evidence` 聚合块
- Evidence card timeline / summary 统计

### inferred（同 trace 内推导）

- `channel`: `unknown` → `mcp`（`_enrich_tool_call_channels` 由 service 事件传播）
- `service_id`: tool_name 前缀匹配（`_infer_service_id`）
- `internal` channel → `local`

### missing（常见）

- 构建**未进入规划/验证阶段**：`no_planner_decision_events`, `no_verifier_result_event`（`sim-5ccbc720f0e0`, `sim-2baa8874b389`）
- `execution_evidence` 中无独立 `tool_call_evidence[]` / `planner_decision_evidence[]` id 列表（仅有 dispatchSequence + card 摘要）
- Evidence card MD **不展示** 完整 arguments/result 正文（仅 ✅ + latency）

### 文档声称但 artifact 未充分支撑

- 库内 `infrastructure_v1_report.md` 声称稳定 **20/20 PASS**：新鲜 headless run 为 **WARN**（`final_result success=False`）
- “不从 log 解析”：`adapter` 已移除；checker `verification_presence` MISSING 文案仍写 “only extractable from log text”

---

## 6. Remaining Gaps

### P0（进入下一阶段实验前建议补）

1. **中断 run 的期望行为**：Orchestrator 在 cancel/fail 早停时是否应仍 emit 部分 `planner_decision`/`verifier_result`？当前缺失导致 WARN，实验汇总需过滤“完成态 trace”。
2. **`planner_decision.reason` 边界**：现为 Agent `done.result` 截断，非结构化 binding reason；论文中不应称为 “non-CoT decision trace”。
3. **executionEvidence 与 evidence_card 链接**：缺 `evidence_card_path` / 显式 `tool_call_evidence` id 数组，下游配置系统只能凭 `evidenceId` 反查。

### P1（论文说服力）

4. Evidence card MD 缺 tool I/O 详情列（JSON 在 trace 有，卡片未展示）。
5. Verifier 语义一致性：headless 样例 `verification.status=FAILED` 但 `reason` 文本含“验证通过”（最后一轮 verifier_result 与 complete 不一致）。
6. Sandbox channel 端到端：有 `sandbox` 落盘样例（`sim-5ccbc720f0e0` opentargets），但无 “sandbox-only 完成态 PASS” 归档。

### P2（多轮固化后）

7. `minIterations` 强制多轮与 evidence 质量关系未评估。
8. Cross-run 对比 / experience solidification 管线未接。

---

## 7. Research Meaning

### 已可支持

- **Trace-grounded Meta-application Artifact Definition 的单次 probe 实验**：在 **complete v1 trace** 上，可机械提取 tool / planner / verifier 证据，生成 evidence_card + config_attachment_draft，checker 可给出 PASS。
- 相对 P0：**真实 MCP tool_call 证据质量提升**有 artifact 支撑（字段从 ~5 增至 11，含 channel/transport/call_id）。

### 尚不可支持

- **Verified Trace Compilation 完整实验**：需稳定“完成 + 验证通过”的 run 集；当前 LLM 方差大，失败 run 比例高。
- **Experience Solidification / 多轮固化**：无跨 run 经验写入与复用证据链。
- 将 planner `reason` 当作可审计 **决策逻辑证据**（偏 NL 摘要，非结构化 binding）。

---

## 8. Next Recommended Step

**2–4 小时优先事项**：

1. 用 `http_mcp_smoke.py` 或 `headless_run.py` 跑 **1 条完整 PASS trace**，归档到 `workspace/data/traces/` 并跑 pipeline，固化 “golden artifact set”。
2. 在 checker 中修正 `verification_presence` MISSING 文案；增加 “build_completed” 前置检查，区分 **incomplete trace** vs **evidence regression**。
3. 在 `execution_evidence` 增加 `evidenceCardPath` + `toolCallEvidenceIds[]`（从 bundle 直接列出 `call_id`），便于配置草稿挂载验收。
4. 库内 `infrastructure_v1_report.md` 若保留，应引用 **可复现 commit + trace 文件名**，而非 “20/20 稳定 PASS” 的绝对表述。

---

*End of audit report.*
