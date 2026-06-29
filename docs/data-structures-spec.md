# 仿真构建数据结构规格

更新：2026-06-28。本文定义当前最小构建链的对象边界。

## 一、输入边界

`POST /api/simulation/start` 当前请求模型为 `SimulationStartRequest`：

```json
{
  "appId": "",
  "appName": "元应用",
  "domain": "generic",
  "servicesMeta": [],
  "maxIterations": 5,
  "scenarioDescription": "",
  "scenarioParsed": {}
}
```

字段说明：

- `servicesMeta`：**元应用可调度服务边界**（推荐智能体据想定从服务池绑定后的 catalog）。仿真构建只在此集合内调度；不在此模块内查服务池。
- `scenarioParsed`：构建前形成的结构化想定；构建模块只规范化，不再次调用 LLM 解析。

**阶段划分**：服务池匹配与边界确定发生在构建前的 **服务推荐**（ioeb → `mcp_service_recommendation`）。本模块（仿真构建）接收 `servicesMeta` 后，在边界内做多轮调度与 Verifier 验收，并编译 GoldenPath（可为绑定集合的真子集）。详见 ioeb `design_docs/simulation-build-design.md` §二。

本模块不做服务发现、开源项目下载、算法开发、MCP 自动封装、MCP 自动部署、服务池数据库修改。

## 二、BuildBundle

每次构建只落一个目录：

```text
workspace/data/simulation_builds/{buildId}/
  manifest.json
  trace.json
  accepted_trajectory.json
  artifact.json
  experiment/                  # 运行实验后才创建
```

`manifest.json` 最后原子写入，是 Bundle 已就绪的提交标志；保存文件路径、`artifactHash`、`publishable` 和本地 API 引用。`artifact.json` 不反向引用 trace、accepted trajectory、verifier event 或 experiment result。

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

## 四、AcceptedTrajectory

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

注意：`accepted_trajectory.json` 当前只表达“最终成功轮次中被接受的业务调用事实”，不表达“最优轨迹”。它不会自动删除同一工具的无效重复调用、参数试错、失败调用或无产出的 discover/schema 探索。后续若引入轨迹优化，应新增显式编译阶段和字段/文件，例如 `optimized_trajectory` 或 GoldenPath 编译报告；优化阶段必须能说明每个删除动作的依据，不能只靠前端展示去重。

## 五、MetaAppArtifact

`artifact.json` 是最终最小运行产物。这里的“最小”不是把所有构建信息塞成一个 JSON，而是只保留运行闭包：

- `app`：元应用身份和领域。
- `taskContract`：运行期需要理解的任务目标、输入、输出和约束。
- `runtime`：运行所需服务绑定、回退策略和 Agent 执行策略。
- `goldenPaths`：可选快路径；没有可接受成功主干时为空。

`artifact.json` 顶层仅包含：`schemaVersion`、`artifactId`、`app`、`taskContract`、`runtime`、`goldenPaths`。构建事实、解释、审计和实验数据留在 BuildBundle 其它文件中，不写入 artifact。

`artifactId` 由除自身外的完整可执行内容计算，GoldenPath、绑定或运行策略变化都会产生新 ID；同一内容可用 `artifactHash` 做完整性校验。

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

## 六、运行与实验

平台临时运行入口：

```text
POST /api/simulation/{buildId}/run
```

科研实验入口：

```text
GET  /api/simulation/experiments/runners
POST /api/simulation/{buildId}/experiments/run
python -m micro_agent.simulation.experiments {buildId} --tasks tasks.json
```

第一版真实 MCP baseline：

- `no_reuse`
- `raw_trace_prompt`
- `workflow_memory`
- `golden_path`

所有 baseline 使用 Eval-time Verifier 判定 `taskSuccess`。当前 runner 已有，批量实验尚需补齐任务集与结果汇总。
