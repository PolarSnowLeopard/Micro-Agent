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

## 当前断点

只剩需要 ioeb_backend / 数据库支持的正式平台持久化断点：

- artifact 正式入库；
- BuildBundle 索引入库；
- 发布链路携带 artifact；
- 平台正式元应用列表持久化 GoldenPath。
