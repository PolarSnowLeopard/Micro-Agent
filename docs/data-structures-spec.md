# 仿真构建数据结构规格

更新：2026-06-21。旧 `ArtifactSpec v0.x`、顶层 `serviceSelection` 产物、`solidificationReport`、`productAcceptance`、`offline_proxy` 实验包均已退出主线；不迁移、不兼容。

## 一、输入边界

`POST /api/simulation/start` 当前请求模型为 `SimulationStartRequest`：

```json
{
  "appId": "",
  "appName": "元应用",
  "domain": "generic",
  "serviceIds": [],
  "servicesMeta": [],
  "maxIterations": 5,
  "scenarioDescription": "",
  "scenarioSummary": "",
  "scenarioParsed": {},
  "mode": "production",
  "strategy": {}
}
```

字段说明：

- `serviceIds`：用户/前端已给定的候选服务 id，可作为 fallback 选择依据。
- `servicesMeta`：已知 MCP service catalog。当前服务选择只在这里面做，不访问后端服务池。
- `scenarioParsed`：如果已有结构化想定可直接传入；否则 MicroAgent 尝试解析自然语言。
- `strategy`：研究配置入口。当前慢模式仍以 ReAct/tool-calling 为主，部分策略只影响日志或 fallback 行为。

本模块不做服务发现、开源项目下载、算法开发、MCP 自动封装、MCP 自动部署、服务池数据库修改。

## 二、BuildBundle

每次构建只落一个目录：

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

`manifest.json` 是构建侧索引，保存各文件路径和 hash。最终 `artifact.json` 不反向引用 trace、accepted trajectory、service selection、verifier event 或 experiment result。

## 三、BuildTrace

`trace.json` 是完整事实链。`tool_call_record` 是唯一调用事实源。

关键字段：

```json
{
  "schemaVersion": "build_trace.v1",
  "build_id": "build-...",
  "session_id": "build-...",
  "app_name": "",
  "domain": "health",
  "mode": "production",
  "strategy": {},
  "events": [],
  "success": true,
  "iterations": 2,
  "elapsed_ms": 1234,
  "metadata": {}
}
```

`tool_call_record.data` 至少包含：

```json
{
  "call_id": "call-...",
  "tool_name": "medical-calc_calculate_batch",
  "service_id": "medical-calc",
  "service_name": "Medical Calc",
  "channel": "real_mcp",
  "transport": "sse",
  "source": "real_mcp",
  "phase": "slow_mode",
  "purpose": "react_action",
  "iteration": 2,
  "react_step_id": "iter2-step1",
  "action_id": "iter2-a1",
  "arguments": {},
  "result": "...",
  "result_hash": "...",
  "error": null,
  "latency_ms": 123,
  "timestamp": 0,
  "success": true
}
```

`service_calling`、`planner_decision`、SSE 展示事件都不是调用事实源。

## 四、ServiceSelectionReport

`service_selection.json` 是构建期中间数据，不进入最终产物。

它回答“为什么从已知服务池里选择这些服务”：

```json
{
  "schemaVersion": "service_selection_report.v1",
  "selectionId": "sel-...",
  "strategy": "llm_catalog_selection",
  "selectedServices": [
    {
      "serviceId": "medical-calc",
      "serviceName": "Medical Calc",
      "reason": "...",
      "matchedCapabilities": []
    }
  ],
  "rejectedServices": [],
  "missingCapabilities": [],
  "rationale": "",
  "confidence": null,
  "model": "",
  "createdAt": ""
}
```

当 LLM 服务选择失败时，`strategy` 会变为 `provided_catalog_fallback`。

## 五、AcceptedTrajectory

`accepted_trajectory.json` 是 Verifier 接受的成功主干，不入库、不进入最终产物。

它从最终 `PASSED` iteration 的实际 `tool_call_record` 提取：

```json
{
  "schemaVersion": "accepted_trajectory.v1",
  "trajectoryId": "traj-...",
  "buildId": "build-...",
  "status": "accepted",
  "acceptedIteration": 2,
  "verifier": {
    "role": "build_verifier",
    "status": "PASSED",
    "summary": "",
    "eventRef": "verifier_result#iter2"
  },
  "actionSequence": [
    {
      "stepId": "s1",
      "actionId": "iter2-a1",
      "callId": "call-...",
      "serviceId": "medical-calc",
      "serviceName": "Medical Calc",
      "toolName": "medical-calc_calculate_batch",
      "source": "real_mcp",
      "transport": "sse",
      "arguments": {},
      "argumentTemplate": {},
      "observation": {
        "success": true,
        "semanticSuccess": true,
        "result": "...",
        "error": null,
        "latencyMs": 123
      },
      "inputSlots": [],
      "dependsOn": []
    }
  ],
  "bindingGaps": [],
  "generatedArtifact": {
    "artifactId": "app-...",
    "artifactHash": "...",
    "recordedAt": ""
  }
}
```

如果没有最终 PASSED Verifier，`status` 为 `missing`，`actionSequence` 为空，artifact 仍可退化为 `agent_only`。

## 六、MetaAppArtifact

`artifact.json` 是最终最小运行产物。

不得包含：

- `serviceSelection`
- `solidificationReport`
- `parsedIntent`
- `productAcceptance`
- `writeBackDraft`
- trace / evidence / verifier / accepted trajectory / experiment 引用

结构：

```json
{
  "schemaVersion": "meta_app_artifact.v1",
  "artifactId": "app-...",
  "app": {
    "name": "元应用",
    "domain": "health",
    "description": ""
  },
  "taskContract": {
    "goal": "",
    "domain": "health",
    "inputSlots": [],
    "outputSlots": [],
    "constraints": [],
    "successCriteria": []
  },
  "runtime": {
    "mode": "agent_with_optional_golden_path",
    "serviceBindings": [],
    "fallbackPolicy": {
      "onApplicabilityMismatch": "run_slow_mode",
      "onBindingFailure": "run_slow_mode",
      "onToolFailure": "run_slow_mode",
      "onAssertionFailure": "run_slow_mode"
    },
    "agent": {
      "style": "react_slow_mode",
      "goldenPathDecision": "agent_internal"
    }
  },
  "goldenPaths": []
}
```

`goldenPaths` 支持多个，当前编译器只生成 primary path。

GoldenPath step 当前包含：

```json
{
  "stepId": "s1",
  "serviceId": "medical-calc",
  "toolName": "medical-calc_calculate_batch",
  "argumentTemplate": {},
  "inputMapping": {},
  "outputSlots": [{"name": "s1_output", "path": "$"}],
  "dependsOn": []
}
```

## 七、FrontendState

`frontend_state.json` 是当前 ioeb 临时展示投影，不是最终产物：

```json
{
  "schemaVersion": "simulation_frontend_state.v1",
  "buildId": "build-...",
  "app": {},
  "taskContract": {},
  "serviceSelection": {},
  "acceptedTrajectorySummary": {},
  "artifactSummary": {},
  "callChain": [],
  "events": {},
  "completion": {},
  "artifact": {}
}
```

## 八、运行与实验

平台临时运行入口：

```text
POST /api/simulation/builds/{buildId}/run
```

科研实验入口：

```text
GET  /api/simulation/experiments/runners
POST /api/simulation/builds/{buildId}/experiments/run
python -m micro_agent.simulation.experiments {buildId} --tasks tasks.json
```

第一版真实 MCP baseline：

- `no_reuse`
- `raw_trace_prompt`
- `workflow_memory`
- `golden_path`

所有 baseline 使用 Eval-time Verifier 判定 `taskSuccess`。当前 runner 已有，批量实验尚需补齐任务集与结果汇总。
