# ioeb 临时展示集成

更新：2026-06-21。本文描述当前 ioeb 与 MicroAgent 的临时展示接入；不涉及 ioeb_backend/数据库写回。

## 一、真实链路

```text
ioeb
-> POST /api/simulation/start
-> GET  /api/simulation/{buildId}/stream
-> SSE complete
-> 读取 BuildBundle JSON（带重试）
```

`start` 返回：

```json
{
  "success": true,
  "sessionId": "build-...",
  "buildId": "build-...",
  "streamUrl": "/api/simulation/build-.../stream",
  "buildRef": {
    "manifestUrl": "/api/simulation/builds/build-.../manifest",
    "traceUrl": "/api/simulation/builds/build-.../trace",
    "serviceSelectionUrl": "/api/simulation/builds/build-.../service-selection",
    "acceptedTrajectoryUrl": "/api/simulation/builds/build-.../accepted-trajectory",
    "artifactUrl": "/api/simulation/builds/build-.../artifact",
    "frontendStateUrl": "/api/simulation/builds/build-.../frontend-state",
    "runUrl": "/api/simulation/builds/build-.../run",
    "experimentUrl": "/api/simulation/builds/build-.../experiments/run"
  }
}
```

## 二、当前时序风险

后端当前在 SSE generator 的 `finally` 中保存 BuildBundle。因此前端收到 `complete` 时，bundle 文件可能刚开始写入。当前 ioeb 用 `fetchTraceWithRetry()` 重试读取来规避竞态。

后续更稳的方案应是：

```text
trace saved -> artifact compiled -> artifact_ready/complete
```

或提供明确的 build status 轮询端点。本阶段暂不改 ioeb_backend。

## 三、临时展示 URL

当前前端仍使用部分旧 URL 名称，但后端只读取新 BuildBundle，不读旧 trace/artifact/evidence 目录：

```text
GET  /api/simulation/{buildId}/trace          -> trace.json
POST /api/simulation/{buildId}/evidence       -> build_evidence_summary.v1 派生摘要
GET  /api/simulation/{buildId}/artifact       -> artifact.json
GET  /api/simulation/{buildId}/frontend-state -> frontend_state.json
```

新的直接 URL：

```text
GET /api/simulation/builds/{buildId}/manifest
GET /api/simulation/builds/{buildId}/trace
GET /api/simulation/builds/{buildId}/service-selection
GET /api/simulation/builds/{buildId}/accepted-trajectory
GET /api/simulation/builds/{buildId}/artifact
GET /api/simulation/builds/{buildId}/frontend-state
```

## 四、展示原则

当前 ioeb 只需证明这些对象存在并可展开查看：

- `trace.json`
- `service_selection.json`
- `accepted_trajectory.json`
- `artifact.json`
- `frontend_state.json`
- `experiment/latest_result.json`（运行实验后）

可以直接展示 JSON/摘要，不做正式 UI 适配；该展示后续可以删除。

## 五、运行入口

```text
POST /api/simulation/builds/{buildId}/run
```

请求：

```json
{
  "message": "当前用户任务",
  "preferGoldenPath": true
}
```

返回关注字段：

- `mode`
- `success`
- `fastPathSuccess`
- `fallbackUsed`
- `fastPathError`
- `result`
- `bindingPlan`
- `toolCalls`

## 六、实验入口

```text
GET  /api/simulation/experiments/runners
POST /api/simulation/builds/{buildId}/experiments/run
```

实验入口只属于 MicroAgent 本地科研系统，不写回 ioeb_backend。

## 七、前端 mock 边界

ioeb 仍保留进程内演示 mock：展示名含“课题”时走 `simulation_builder_inmemory.js`。这条路可用于 demo，但不属于真实构建链路，也不计入科研实验。
