# 元应用想定式仿真构建路线图

更新：2026-06-21。

## 当前目标

只重建“元应用想定式仿真构建”这一段，使它同时满足：

- 平台可用：ioeb 能启动构建、查看 BuildBundle/MetaAppArtifact、触发 MicroAgent 本地运行。
- 科研可实验：真实 MCP baseline 可批量运行、可消融、可对比。
- 语义不靠末端 gate 补丁：对象模型直接区分 trace、中间数据、最终产物、实验结果。

## 已实现基线

| 能力 | 状态 |
| --- | --- |
| 慢模式 | 复用既有 LLM tool-calling Agent / MCP wrapper / Verifier，保持 ReAct 探索范式 |
| 服务匹配 | 已知服务池内 LLM 选择，输出 `service_selection.json`，不进 artifact |
| 调用事实源 | `tool_call_record` 增加 `phase/source/purpose/iteration/action_id` |
| BuildBundle | 新构建落盘到 `workspace/data/simulation_builds/{buildId}` |
| AcceptedTrajectory | 从最终 PASSED iteration 的实际 tool calls 提取，不进 artifact |
| MetaAppArtifact | `meta_app_artifact.v1`，最小运行产物，不含构建诊断 |
| GoldenPath | 作为 artifact 内部可用快路径资产，运行期由 Agent/LLM 判断适用并生成 BindingPlan |
| 快慢模式 | GoldenPathExecutor 确定性执行，失败回退 MetaAppAgent 慢模式 |
| 科研 runner | `real_mcp_reuse`，baseline 为 `no_reuse/raw_trace_prompt/workflow_memory/golden_path` |
| ioeb 展示 | 临时 JSON/摘要展示，不改 ioeb_backend |

## 删除内容

以下内容视为开发期旧产物，已不再维护：

- `ArtifactSpec v0.x`
- `solidificationReport` 顶层产物字段
- `serviceSelection` 顶层产物字段
- `productAcceptance`
- `writeBackDraft`
- `offline_proxy` / 字段完整性实验 runner
- `workspace/data/traces`、`workspace/data/artifacts`、`workspace/data/evidence` 旧语义

## 剩余断点

本轮不改 `ioeb_backend` / 数据库，因此断点只保留在平台正式持久化层：

- ioeb_backend 不保存 `MetaAppArtifact`。
- 数据库不保存 `BuildBundle` 索引。
- 平台正式元应用列表不持久化 GoldenPath。
- 预发布/发布链路不携带 artifact。
- 跨会话长期复用依赖 MicroAgent 本地文件系统。

科研实验结果任何一版都不进入 ioeb_backend。

## 后续研究优化点

- GoldenPath 是否存在/是否值得固化的预测机制。
- 数据依赖和 BindingPlan 的更强归纳、验证、消融。
- L3 业务语义断言从 Verifier 蒸馏为部分可执行规则。
- 同一元应用内多 GoldenPath 管理与选择。
- 更大规模医疗/生物医药标准化 MCP 服务池实验。
