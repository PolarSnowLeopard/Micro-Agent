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
-> MicroAgent /builds/{buildId}/experiments/run 本地运行科研实验
```

## MicroAgent API 读写边界

写入本地 BuildBundle：

- `POST /api/simulation/start` 创建 build/session。
- `GET /api/simulation/{buildId}/stream` 执行 LLM + MCP + Verifier 构建，并在结束后写 `workspace/data/simulation_builds/{buildId}`。
- `POST /api/simulation/builds/{buildId}/experiments/run` 写 `experiment/latest_result.json`。

读取本地 BuildBundle：

- `GET /api/simulation/builds/{buildId}/manifest`
- `GET /api/simulation/builds/{buildId}/trace`
- `GET /api/simulation/builds/{buildId}/service-selection`
- `GET /api/simulation/builds/{buildId}/accepted-trajectory`
- `GET /api/simulation/builds/{buildId}/artifact`
- `GET /api/simulation/builds/{buildId}/frontend-state`
- `POST /api/simulation/builds/{buildId}/run`

兼容展示 URL 只读取新 BuildBundle，不支持旧 trace/artifact/evidence 存储。

## 后端现状

`ioeb_backend` 仍主要通过 `ServiceApi` 表承载平台元应用相关字段，如名称、描述、服务 ID 列表、输入输出名、工具节点等。它没有正式承载：

- `MetaAppArtifact`
- `BuildBundle` 索引
- `AcceptedTrajectory`
- GoldenPath
- 科研实验结果
- 标准化 MCP service schema/version/hash

## 数据不得进入后端或最终产物

以下内容只属于 MicroAgent 本地构建/科研中间数据：

- BuildTrace 原文；
- ServiceSelectionReport；
- AcceptedTrajectory；
- BindingPlan；
- Eval-time Verifier 详细结果；
- experiment trial/result；
- 原始医疗输入、工具 arguments、完整 MCP result。

最终 `MetaAppArtifact` 只保留运行必要结构；可溯源关系在 BuildBundle manifest / acceptedTrajectory.generatedArtifact 中保存，不反向写入 artifact。

## 当前断点

以下能力需要未来修改 `ioeb_backend` / 数据库后才能闭合：

- artifact 正式入库；
- BuildBundle 索引入库；
- 平台正式发布链路携带 artifact；
- 平台元应用列表可长期恢复 GoldenPath；
- 服务池中标准化 MCP schema/version/hash 的后端管理。

科研实验结果不应进入 `ioeb_backend`，任何版本都应由 MicroAgent 本地科研文件或独立实验存储管理。
