# ioeb/ioeb_backend 读写链当前断点

更新：2026-06-21。

当前实现不写回 `ioeb_backend`，不修改数据库。MicroAgent 侧 `BuildBundle` 是仿真构建与科研实验的唯一落盘单位。

## 当前可用链路

```text
ioeb 前端
-> MicroAgent /api/simulation/start
-> MicroAgent SSE 构建
-> workspace/data/simulation_builds/{buildId}
-> ioeb 临时读取 JSON 展示
-> MicroAgent /builds/{buildId}/run 本地运行 artifact
```

## 后端现状

`ioeb_backend` 仍主要通过 `ServiceApi` 表承载平台元应用相关字段，如名称、描述、服务 ID 列表、输入输出名、工具节点等。它没有正式承载：

- `MetaAppArtifact`
- `BuildBundle` 索引
- `AcceptedTrajectory`
- GoldenPath
- 科研实验结果


## 当前真实运行状态

截至 2026-06-21：

- MicroAgent 真实工作目录：`/home/lyx/workspace/fdueblab/Micro-Agent`，分支 `lyx`，HEAD `af67000`。
- ioeb 真实工作目录：`/home/lyx/workspace/fdueblab/ioeb`，分支 `lyx`，HEAD `b3ce72c`。
- Codex 临时 worktree 已删除，不再作为修改入口。
- 后台服务建议由 user systemd 管理：
  - `fdueblab-micro-agent.service`
  - `fdueblab-ioeb.service`
- 健康检查：
  - `http://127.0.0.1:9017/docs`
  - `http://127.0.0.1:9017/api/simulation/experiments/runners`
  - `http://127.0.0.1:6173/`

## 当前 API 读写边界

MicroAgent 新主链路：

- `POST /api/simulation/start` 创建 build/session。
- `GET /api/simulation/{buildId}/stream` 执行 LLM + MCP + Verifier 构建并最终写 BuildBundle。
- `GET /api/simulation/builds/{buildId}/manifest`
- `GET /api/simulation/builds/{buildId}/trace`
- `GET /api/simulation/builds/{buildId}/service-selection`
- `GET /api/simulation/builds/{buildId}/accepted-trajectory`
- `GET /api/simulation/builds/{buildId}/artifact`
- `GET /api/simulation/builds/{buildId}/frontend-state`
- `POST /api/simulation/builds/{buildId}/run`
- `POST /api/simulation/builds/{buildId}/experiments/run`

兼容展示 URL 只读取新 BuildBundle，不支持旧 trace/artifact/evidence 存储。

## 数据不得入库原则

不得进入 git、ioeb_backend 或最终 artifact 的内容：

- BuildTrace 原文；
- ServiceSelectionReport；
- AcceptedTrajectory；
- BindingPlan；
- Eval-time Verifier 详细结果；
- experiment trial/result；
- 原始医疗输入、工具 arguments、完整 MCP result；
- 本地 `.cursor/.codex/.agents/.claude` 配置。

最终 artifact 只保留运行必要结构；可溯源关系在 BuildBundle manifest / acceptedTrajectory.generatedArtifact 中保存，不反向写入 artifact。

## 已知工程注意点

- 前端 `VUE_APP_LOCAL_MCP_REWRITE=true` 是既有本地开发功能，不是仿真构建模块新逻辑。它只服务本机 MCP proxy 地址改写。
- 当前 ioeb 前端对 MetaAppArtifact v1 是临时 JSON/摘要展示，低耦合，后续可删除或重做正式 UI。
- 如果后续要正式平台化，必须新增后端/数据库 schema；当前不做。
- 如果服务池 schema/version/hash 要可复现管理，需要 ioeb_backend 增加标准化 MCP 服务契约字段；当前由前端/请求传入 `servicesMeta`。

## 当前断点

以下能力需要未来修改 `ioeb_backend` / 数据库后才能闭合：

- artifact 正式入库；
- BuildBundle 索引入库；
- 平台正式发布链路携带 artifact；
- 平台元应用列表可长期恢复 GoldenPath；
- 服务池中标准化 MCP schema/version/hash 的后端管理。

科研实验结果不应进入 `ioeb_backend`，任何版本都应由 MicroAgent 本地科研文件或独立实验存储管理。
