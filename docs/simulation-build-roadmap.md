# 元应用想定式仿真构建路线图

更新：2026-06-21。本文是短路线图；详细结构见 `docs/data-structures-spec.md`，详细目标缺口见 `GOAL_META_APP_SPEC.md`。

## 当前边界

只重建“元应用想定式仿真构建”这一段：

- 平台入口：ioeb 启动构建、读取 BuildBundle/MetaAppArtifact、触发 MicroAgent 本地运行。
- 科研入口：MicroAgent 本地 runner 对比轨迹复用 baseline。
- 语义来源：对象分层和唯一事实源，而不是末端 gate 补丁。

本模块不做算法开发、MCP 自动封装、服务池入库、ioeb_backend 改库、旧数据迁移、无 LLM/无 MCP fallback。

## 当前实现状态

| 主题 | 状态 |
| --- | --- |
| 慢模式 | 复用既有 LLM Agent/ReAct/tool-calling/MCP wrapper/Verifier |
| 服务选择 | LLM 在请求传入的已知 catalog 内选择服务；失败回退 serviceIds/catalog |
| 调用事实源 | `tool_call_record` 记录真实 MCP/Sandbox 调用，带 source/phase/purpose/iteration/action_id |
| BuildBundle | `workspace/data/simulation_builds/{buildId}` 单目录落盘 |
| AcceptedTrajectory | 从最终 PASSED iteration 的实际业务 tool calls 提取，不进 artifact |
| MetaAppArtifact | `meta_app_artifact.v1` 最小运行产物，不含构建诊断 |
| GoldenPath | artifact 内部快路径资产；运行期由 LLM 判断适用并生成 BindingPlan |
| 快慢模式 | GoldenPath 执行失败后回退慢模式 |
| 实验 runner | `real_mcp_reuse` 入口已实现，baseline 为 `no_reuse/raw_trace_prompt/workflow_memory/golden_path` |
| ioeb 展示 | 临时 JSON/摘要展示，不改 ioeb_backend |

## 当前断点

- SSE `complete` 先到达，BuildBundle 保存发生在后端 generator `finally`，前端当前需要重试读取。
- baseline runner 已有，但尚未在同一任务集上批量验证四个 baseline。
- GoldenPath 主要依赖 `argumentTemplate` 和轻量 BindingPlan，泛化数据流仍弱。
- AcceptedTrajectory 目前是“通过主干”，不是“优化主干”；无效重复调用、参数试错、失败调用和无产出 discover/schema 探索还没有剪枝。
- service schema/version/hash 主要来自请求侧元数据和工具列表摘要，未接正式服务池契约。
- token/cost/LLM call count 指标未完整采集。
- ioeb_backend 不保存 artifact、BuildBundle、GoldenPath、实验结果。

## 下一步优先级

1. 稳定真实 MCP 任务集，重复构建、运行、实验。
2. 同任务集跑通四类 baseline 并输出 JSONL/CSV 汇总。
3. 验证 GoldenPath 失败后 fallback 慢模式。
4. 增强 BindingPlan、L2 数据流断言和 observation 失败解析。
5. 增加轨迹优化编译阶段：先对重复调用、失败试错、无效 discover/schema 探索做可解释剪枝，再生成可复用 GoldenPath。
6. 设计正式平台持久化字段，但等 ioeb_backend 阶段再实现。
