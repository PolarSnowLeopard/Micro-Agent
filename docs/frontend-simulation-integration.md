# ioeb 临时展示集成

更新：2026-06-28。本文描述当前 ioeb 与 MicroAgent 的实验阶段接入；不涉及 ioeb_backend/数据库写回。

## 一、真实链路

```text
ioeb
-> POST /api/simulation/start
-> GET  /api/simulation/{buildId}/stream
-> BuildBundle/manifest 落盘
-> SSE complete（含 publishable / artifact 引用）
-> 读取 BuildBundle JSON
```

`start` 返回：

```json
{
  "success": true,
  "sessionId": "build-...",
  "buildId": "build-...",
  "streamUrl": "/api/simulation/build-.../stream",
  "buildRef": {
    "manifestUrl": "/api/simulation/build-.../manifest",
    "traceUrl": "/api/simulation/build-.../trace",
    "acceptedTrajectoryUrl": "/api/simulation/build-.../accepted-trajectory",
    "artifactUrl": "/api/simulation/build-.../artifact",
    "runUrl": "/api/simulation/build-.../run",
    "experimentUrl": "/api/simulation/build-.../experiments/run"
  }
}
```

## 二、终止时序

```text
trace saved -> artifact compiled -> manifest saved -> complete
```

`complete` 是终止事实；只有 `complete.publishable=true` 的构建才进入预发布。

## 三、临时展示 URL

```text
GET  /api/simulation/{buildId}/manifest       -> manifest.json
GET  /api/simulation/{buildId}/trace          -> trace.json
POST /api/simulation/{buildId}/evidence       -> build_evidence_summary.v1 派生摘要
GET  /api/simulation/{buildId}/artifact       -> artifact.json
GET /api/simulation/{buildId}/accepted-trajectory
```

## 四、展示原则

当前 ioeb 只需证明这些对象存在并可展开查看：

- `trace.json`
- `accepted_trajectory.json`
- `artifact.json`
- `experiment/latest_result.json`（运行实验后）

可以直接展示 JSON/摘要，不做正式 UI 适配；该展示后续可以删除。

## 五、运行入口

```text
POST /api/simulation/{buildId}/run
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
POST /api/simulation/{buildId}/experiments/run
```

实验入口只属于 MicroAgent 本地科研系统，不写回 ioeb_backend。

## 七、前端 mock 边界

ioeb 仍保留进程内演示 mock：展示名含“课题”时走 `simulation_builder_inmemory.js`。这条路可用于 demo，但不属于真实构建链路，也不计入科研实验。
