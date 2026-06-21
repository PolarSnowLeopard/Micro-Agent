# 当前目标：元应用想定式仿真构建

更新：2026-06-21。

## 一句话目标

只重建 MicroAgent 的“元应用想定式仿真构建”模块：给定结构化想定和已标准化 MCP 服务池，复用既有 LLM tool-calling / MCP / Verifier 能力，产出平台可运行的最小元应用产物，并形成真实 MCP 科研实验闭环。

## 架构边界

本模块负责：

- 已知服务池内 LLM 服务选择；
- ReAct 慢模式 MCP 调度；
- Verifier 构建期裁判；
- BuildBundle 落盘；
- AcceptedTrajectory 提取；
- MetaAppArtifact 编译；
- GoldenPath 快路径运行与失败回退；
- 真实 MCP baseline 实验。

本模块不负责：

- 自动发现服务；
- 下载/开发开源算法；
- MCP 自动封装；
- MCP 自动部署；
- 修改服务池数据库；
- 写回 ioeb_backend。

## 新产物分层

```text
BuildTrace              完整事实链
ServiceSelectionReport  构建期服务选择解释
AcceptedTrajectory      Verifier 接受的成功主干
MetaAppArtifact         最小运行产物
ExperimentRun           科研实验结果
```

只有 `MetaAppArtifact` 是最终元应用产物。其它都是 MicroAgent 本地构建/科研中间数据。

## BuildBundle

```text
workspace/data/simulation_builds/{buildId}/
  manifest.json
  trace.json
  service_selection.json
  accepted_trajectory.json
  artifact.json
  frontend_state.json
  experiment/
```

不读取、不迁移旧 `traces/artifacts/evidence` 目录。

## MetaAppArtifact v1

最终产物只保留运行必要字段：

- app
- taskContract
- runtime.serviceBindings
- runtime.fallbackPolicy
- runtime.agent
- goldenPaths[]

不包含：

- serviceSelection
- solidificationReport
- parsedIntent
- productAcceptance
- writeBackDraft
- trace/evidence/acceptedTrajectory 引用

## GoldenPath

GoldenPath 是 MetaAppAgent 内部快路径资产：

```text
Agent 判断适用
-> LLM 生成 BindingPlan
-> GoldenPathExecutor 确定性调 MCP
-> L1/L2 断言
-> 失败回退慢模式
-> Eval-time Verifier 判质量
```

第一版只处理 primary path；schema 允许多个 path。

## 科研实验

第一版真实 MCP baseline：

- `no_reuse`
- `raw_trace_prompt`
- `workflow_memory`
- `golden_path`

质量由统一 Eval-time Verifier 判定。实验结果只落 MicroAgent 本地文件系统，不进入平台后端。


## 设计第一性原理与已确认结论

- 不用末端 gate 补丁制造语义。语义应来自分层对象本身：BuildTrace 是事实，AcceptedTrajectory 是 Verifier 接受的成功主干，MetaAppArtifact 是最小可运行产物，ExperimentRun 是科研评价结果。
- 慢模式必须是 ReAct/tool-calling 探索范式，而不是预生成结构化 plan 的固定执行器。执行结果可以收敛为调用轨迹，但 Planner 不应被改成纯 step graph runner。
- GoldenPath 是单个元应用内部的快路径资产，不是平台外部路由器。运行时由 MetaAppAgent/LLM 先判断当前任务是否适用，再生成 BindingPlan；若快路径失败，回退慢模式。
- 构建期 Verifier 是最终裁判。Eval-time Verifier 可复用同一实现，但必须记录角色为 `eval_verifier`，避免混淆构建验收和实验评价。
- 轨迹复用第一阶段只面向单个元应用内部：存入可复用黄金轨迹，实现快慢模式运行，提高简单任务响应速度，并保留复杂任务的 ReAct 柔性。
- GoldenPath 可以不存在；“什么任务可以存在 GoldenPath”本身是后续实验与优化点。
- L1/L2 优先规则化：必调工具、工具顺序、工具调用成功、参数绑定、上一步输出进入下一步、最终输出来源。L3 业务语义由 Verifier 判定。
- GoldenPath 参数模板只允许历史参数、runtime slot、step_output 引用和小白名单转换。不要把完整自然语言 trace 塞进产物，也不要让 LLM 重猜工具控制参数。
- 工具协议成功不等于业务成功。MCP `call_tool` 成功但 observation JSON 内 `success=false`、`all_success=false` 或带业务 error 时，应视为快路径失败并触发回退。
- AcceptedTrajectory v1 只从最终 PASSED iteration 抽取，不做跨 iteration 成功片段拼接。失败尝试留在 BuildTrace，不进入 GoldenPath。
- `tool_call_record` 是唯一调用事实源；planner/verifier/SSE/front-end 均是事实投影或解释。调用记录应尽量在源头标注 `source`、`phase`、`purpose`、`iteration`、`react_step_id`、`action_id`。
- 服务选择是构建期中间数据，不进入最终 artifact。当前目标只做基于结构化想定和 LLM 的“从给定服务池选择相关服务列表”；不做自动发现、自动封装或数据库服务池变更。
- 默认假设实验 MCP 已由上游“想定式服务自动封装”标准化；本模块消费标准化服务描述和 io schema，不负责生成服务。
- fake MCP 只能用于 demo，最终研究链路使用真实标准化 MCP。若出现 fake/demo source，应标记 `researchEligible=false`。
- 医疗/生物医学案例要注意数据风险：trace/arguments/result 可能含患者信息，应只保存在本地中间数据，不进入最终 artifact 或后端；正式版本需要脱敏、访问控制和保存周期策略。

## 平台入口与科研入口

平台入口：

```text
ioeb 前端 /api/simulation/start
-> MicroAgent SSE 构建
-> BuildBundle 本地落盘
-> ioeb 低耦合 JSON/摘要展示
-> /api/simulation/builds/{buildId}/run 本地运行 artifact
```

科研入口：

```text
BuildBundle / artifact
-> /api/simulation/builds/{buildId}/experiments/run
-> baseline runner: no_reuse / raw_trace_prompt / workflow_memory / golden_path
-> Eval-time Verifier
-> 本地 experiment/latest_result.json
```

两个入口共享同一 runner core；平台展示不应承担科研逻辑，科研结果不写后端。

## 当前真实验证记录

2026-06-21 已在真实路径完成验证：

- MicroAgent commit: `af67000` (`origin/lyx`)
- ioeb commit: `b3ce72c` (`origin/lyx`)
- 真实构建 Build ID: `build-c731a074a75e`
- MCP: `medical-calc` via `http://127.0.0.1:18000/sse`
- 构建结果：第 1 轮 Verifier FAILED，第 2 轮修正后 PASSED
- Artifact: `meta_app_artifact.v1`，含 1 条 primary GoldenPath
- GoldenPath replay: 4 次真实 MCP 调用，约 3.2s，`fastPathSuccess=true`，`fallbackUsed=false`
- 实验入口：`golden_path` baseline，1 条任务，`taskSuccess=true`，`verifierPassed=true`

## 环境与工作方式约定

- 后续直接在真实路径工作：`/home/lyx/workspace/fdueblab/Micro-Agent` 与 `/home/lyx/workspace/fdueblab/ioeb`。
- 不再通过 Codex worktree 间接修改；当前已删除 `/home/lyx/.codex/worktrees/b4f9/Micro-Agent`。
- MicroAgent 运行端口是 `9017`，ioeb dev 端口是 `6173`。
- 推荐用 user systemd transient service 保持服务：`fdueblab-micro-agent.service` 与 `fdueblab-ioeb.service`。普通 `nohup`/`setsid` 在当前工具执行器里可能被回收。
- `VUE_APP_LOCAL_MCP_REWRITE=true` 是既有 ioeb 本地开发逻辑，用于把 `fdueblab.cn/mcp-proxy/PORT` 改写到本机同端口；不是本次新增机制。
- `ioeb_backend` 本阶段只读，不修改数据库，不写回 artifact。
- `.cursor/`、`.codex/`、`.agents/`、`.claude/`、`workspace/data/`、trace/evidence/artifact/experiment 运行产物不得入库。

## 当前断点

只剩需要 ioeb_backend / 数据库支持的正式平台持久化断点：

- artifact 正式入库；
- BuildBundle 索引入库；
- 发布链路携带 artifact；
- 平台正式元应用列表持久化 GoldenPath。
