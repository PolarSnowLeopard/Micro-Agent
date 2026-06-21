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

## 当前断点

以下能力需要未来修改 `ioeb_backend` / 数据库后才能闭合：

- artifact 正式入库；
- BuildBundle 索引入库；
- 平台正式发布链路携带 artifact；
- 平台元应用列表可长期恢复 GoldenPath；
- 服务池中标准化 MCP schema/version/hash 的后端管理。

科研实验结果不应进入 `ioeb_backend`，任何版本都应由 MicroAgent 本地科研文件或独立实验存储管理。
