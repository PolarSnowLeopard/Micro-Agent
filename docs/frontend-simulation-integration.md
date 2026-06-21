# ioeb 临时展示集成

更新：2026-06-21。本文件只描述当前临时展示接口；后续正式写回需要 ioeb_backend/数据库支持，当前不实现。

## 流程

```text
ioeb
-> POST /api/simulation/start
-> GET  /api/simulation/{buildId}/stream
-> SSE complete 后读取 BuildBundle JSON
```

`start` 返回：

```json
{
  "success": true,
  "sessionId": "build-...",
  "buildId": "build-...",
  "streamUrl": "/api/simulation/build-.../stream",
  "buildRef": {
    "artifactUrl": "/api/simulation/builds/build-.../artifact",
    "acceptedTrajectoryUrl": "/api/simulation/builds/build-.../accepted-trajectory",
    "serviceSelectionUrl": "/api/simulation/builds/build-.../service-selection"
  }
}
```

为兼容当前前端临时面板，以下旧 URL 会读取新 BuildBundle，不读取旧数据：

```text
GET  /api/simulation/{buildId}/trace
POST /api/simulation/{buildId}/evidence
GET  /api/simulation/{buildId}/artifact
GET  /api/simulation/{buildId}/frontend-state
```

## 展示原则

当前 ioeb 只需证明以下对象存在：

- `trace.json`
- `service_selection.json`
- `accepted_trajectory.json`
- `artifact.json`
- `frontend_state.json`
- `experiment/latest_result.json`（运行实验后）

可以直接用可展开 JSON 展示，不做复杂 UI 适配；该展示后续可删除。

## 运行入口

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

返回会标明：

- `mode`
- `fastPathSuccess`
- `fallbackUsed`
- `result`
- `bindingPlan`

## 实验入口

```text
GET  /api/simulation/experiments/runners
POST /api/simulation/builds/{buildId}/experiments/run
```

实验入口只属于 MicroAgent 本地科研系统，不写回 ioeb_backend。
