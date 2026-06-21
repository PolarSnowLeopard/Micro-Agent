# 仿真构建数据结构规格

更新：2026-06-21。旧 `ArtifactSpec v0.x`、`serviceSelection` 顶层产物、`solidificationReport`、`offline_proxy` 实验包均已废弃；不迁移、不兼容。

## 一、核心边界

元应用想定式仿真构建模块只消费：

- `ScenarioParsed` 或自然语言场景；
- 已知、标准化的 MCP service catalog；
- 既有 LLM tool-calling、MCP wrapper、Verifier。

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

`manifest.json` 是构建侧索引，保存各文件 hash 和路径。最终 `artifact.json` 不反向引用 trace、accepted trajectory 或 verifier event。

## 三、BuildTrace

`trace.json` 是完整事实链。`tool_call_record` 是唯一调用事实源。

关键字段：

```json
{
  "schemaVersion": "build_trace.v1",
  "build_id": "build-...",
  "events": [
    {"type": "scenario_parsed", "data": {}},
    {"type": "service_selection", "data": {}},
    {"type": "planner_decision", "data": {}},
    {"type": "tool_call_record", "data": {}},
    {"type": "verifier_result", "data": {}}
  ]
}
```

`tool_call_record.data` 至少包含：

```json
{
  "call_id": "call-...",
  "tool_name": "linezolid_calculate_dose",
  "service_id": "linezolid",
  "source": "real_mcp",
  "phase": "slow_mode",
  "purpose": "react_action",
  "iteration": 1,
  "react_step_id": "iter1-step2",
  "action_id": "iter1-a1",
  "arguments": {},
  "result": "...",
  "success": true
}
```

## 四、ServiceSelectionReport

`service_selection.json` 是构建期中间数据，不进入最终产物。

它回答“为什么从已知服务池里选择这些服务”：

```json
{
  "schemaVersion": "service_selection_report.v1",
  "strategy": "llm_catalog_selection",
  "selectedServices": [],
  "rejectedServices": [],
  "missingCapabilities": [],
  "rationale": ""
}
```

## 五、AcceptedTrajectory

`accepted_trajectory.json` 是 Verifier 接受的成功主干，不入库、不进入最终产物。

它从最终 `PASSED` iteration 的实际 `tool_call_record` 提取：

```json
{
  "schemaVersion": "accepted_trajectory.v1",
  "status": "accepted",
  "acceptedIteration": 1,
  "actionSequence": [
    {
      "stepId": "s1",
      "callId": "call-...",
      "serviceId": "linezolid",
      "toolName": "linezolid_calculate_dose",
      "arguments": {},
      "observation": {},
      "inputSlots": [],
      "dependsOn": []
    }
  ],
  "generatedArtifact": {
    "artifactId": "app-...",
    "artifactHash": "..."
  }
}
```

## 六、MetaAppArtifact

`artifact.json` 是最终最小运行产物。

不得包含：

- `serviceSelection`
- `solidificationReport`
- `parsedIntent`
- `productAcceptance`
- `writeBackDraft`
- trace / evidence / verifier / accepted trajectory 引用

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
    "fallbackPolicy": {},
    "agent": {
      "style": "react_slow_mode",
      "goldenPathDecision": "agent_internal"
    }
  },
  "goldenPaths": []
}
```

`goldenPaths` 支持多个，第一版只生成并运行 primary path。

## 七、运行与实验

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

所有 baseline 使用 Eval-time Verifier 判定 `task_success`。`offline_proxy` 已删除，不属于实验体系。
