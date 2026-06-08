# ioeb/ioeb_backend 元应用配置读写全链路侦察报告

> 侦察日期：2025-06-08 | 基于代码静态分析，未做运行时验证  
> **成果缺口总表**（PPT §7–10）：`GOAL_META_APP_SPEC.md` §8  
> **演进排期**：`docs/simulation-build-roadmap.md`

---

## 1. 前端 (ioeb) 元应用数据形态

### 1.1 Mock 数据 schema (`ioeb/src/mock/data/meta_apps_data.js`)

两层结构：

```js
// 元应用信封
{ preName, preDes, preInputName, preOutputName, inputType, outputType,
  nodeList: [...] }

// 节点 (nodeList 元素)
{ id, name, url?, mcpMethod?, type, domain, industry, scenario,
  technology, status, isFake?, tools: [{id, name, description}] }
```

**当前没有任何场景信息、状态机轨迹、溯源哈希字段。**

### 1.2 仿真构建后发布流 (`simulation_builder.vue`)

**complete.result 当前字段**（后端 `orchestrator.py` l.302-311 产出）：
```json
{
  "executionPath": "...",
  "strategy": {...},
  "appName": "...",
  "domain": "...",
  "toolChannels": [...]
}
```

**handlePrePublish** (l.1629-1640) 向上 emit：
```js
{
  appName, appId,
  servicesCount, iterations, executionTime,
  metrics: {...finalMetrics},
  result: this.finalResult    // ← 来自 complete.result
}
// + 触发 'prePublish' 事件
```

**发布桥接关键路径**：`complete.result` → `this.finalResult` → `handlePrePublish.$emit('success', { result })` → 父组件接收 → 调用 `createService()` / `prepublishService()` / `updateService()`。

**ArtifactSpec v0 插入点**：在 `orchestrator.py` 的 `complete.result` 中新增 `compiledApp` / `artifactRef` 字段，前端 `simulation_builder.vue` 的 `handlePrePublish` 携带该字段到父组件。

### 1.3 API 客户端 (`ioeb/src/api/service.js`)

| 函数 | HTTP | 用途 |
|---|---|---|
| `createService(data)` | POST /services | 新建服务/元应用 |
| `prepublishService(data)` | POST /services/prepublish | 新建+部署 |
| `updateService(id, data)` | POST /services/{id} | 更新 |
| `uploadScenarioGeneratedAlgorithm(fd)` | POST /services/scenario-generated/upload | 想定式生成算法上传 |

---

## 2. 后端 (ioeb_backend) 元应用持久化模型

### 2.1 ServiceApi 表结构 (元应用复用此表)

`app/models/service/service_api.py` — `__tablename__ = "service_apis"`

| 列名 | 类型 | 约束 | 用途 |
|---|---|---|---|
| id | String(36) | PK | API ID |
| service_id | String(36) | FK→services.id, NOT NULL | 所属服务 |
| name | String(100) | NOT NULL | API 名称 |
| url | String(200) | NOT NULL | API 地址 |
| method | String(10) | NOT NULL | HTTP 方法 |
| des | Text | nullable | API 描述 |
| parameter_type | Integer | NOT NULL | 参数类型 |
| response_type | Integer | NOT NULL | 响应类型 |
| is_fake | Boolean | default=False | 是否模拟数据 |
| response | Text | nullable | 模拟响应 |
| response_file_name | String(100) | nullable | 响应文件名 |
| example_msg | Text | nullable | 示例消息 |
| **元应用专用** | | | |
| subtitle | String(200) | nullable | 副标题 |
| services | Text | nullable | 服务ID列表（逗号分隔） |
| input_name | String(100) | nullable | 输入名称 |
| output_name | String(100) | nullable | 输出名称 |
| output_visualization | Boolean | default=False | 可视化输出 |
| submit_button_text | String(50) | nullable | 提交按钮文本 |

**关联**：`parameters` (ServiceApiParameter), `tools` (ServiceApiTool)

### 2.2 to_dict() 序列化 (响应给前端)

```json
{
  "name", "url", "method", "des",
  "parameterType", "responseType",
  "isFake?", "response?", "responseFileName?",
  // 元应用字段 (仅非空时输出)：
  "subtitle?", "services?[]", "inputName?", "outputName?",
  "outputVisualization?", "submitButtonText?",
  // 嵌套：
  "tools?[{id, name, description}]",
  "parameters?[{name, type, des}]",
  "exampleMsg?"
}
```

### 2.3 写回路径

```
API (service_ns.py)           service_service.py          repository.py               DB
────────────────────         ──────────────────         ─────────────               ──
POST /services          →    create_service()      →    create_service_with_relations()
POST /services/{id}     →    update_service()      →    update_service_with_relations()
POST /services/prepublish→   create_service() + deploy_service()
```

**字段映射**（JSON camelCase → DB snake_case，在 repository 层完成）：
- `services` (数组) → `_convert_services_list_to_string()` → 逗号分隔字符串
- `subtitle`, `inputName`→`input_name`, `outputName`→`output_name`, `outputVisualization`→`output_visualization`, `submitButtonText`→`submit_button_text`

**Repository 的 update_service_with_relations** 采用**全量替换策略**：删掉旧的 apiList/parameters/tools，重新插入。新增字段需要在上面的映射中加入新的 `api_data.get()` 调用。

---

## 3. 数据库迁移现状 (ArtifactSpec 写回的第一道门槛)

### 3.1 当前状态

```python
# app/__init__.py l.43-46
from app.extensions import db, migrate
db.init_app(app)
migrate.init_app(app, db)  # Flask-Migrate 已初始化
```

```python
# manage.py l.35-38
@cli.command("create_db")
def create_db():
    db.create_all()  # ← 首次建表，不改已存在的表
```

**事实**：
- Flask-Migrate **已初始化但未使用**
- **仓库内无 `migrations/` 目录**
- `db.create_all()` 不会 ALTER 已存在的表
- 新增列需要择一方案：A) 补建 migrations workflow，B) 开发库重建，C) JSON 列作为临时逃逸口

### 3.2 推荐迁移策略

**短期（v0）**：在 `ServiceApi` 表新增 `des` (Text) 字段的兄弟用法 — 用已有 `des` 字段暂存 ArtifactSpec JSON，等 schema 稳定后再做正式迁移。**但这不可行**：`des` 是 API 描述，语义冲突。

**短期替代方案**：新增 `artifact_spec` (Text/JSON) 列 + 使用 `ALTER TABLE` 手动添加，不依赖 Flask-Migrate 自动生成迁移。或：**ArtifactSpec v0 暂不写回 ioeb_backend，只落盘 Micro-Agent 侧**，由前端通过 Micro-Agent API 获取 artifact 展示，写回阶段单独决策。

---

## 4. ArtifactSpec v0 → 元应用配置 字段映射设计（草稿）

### 4.1 现有字段承载能力

| ArtifactSpec 内容 | ioeb_backend 现状 | 承载方式 |
|---|---|---|
| 名称/描述 | ✅ name, des | 直接用 |
| 服务ID列表 | ✅ services (逗号分隔 Text) | 直接用 |
| 输入/输出名 | ✅ input_name, output_name | 直接用 |
| 可视化/按钮 | ✅ output_visualization, submit_button_text | 直接用 |
| **场景信息** | ❌ 无 | 需新增列或 JSON blob |
| **状态机轨迹** | ❌ 无 | 需新增列或 JSON blob |
| **溯源哈希** | ❌ 无 | 需新增列 |
| **sourceSessionId** | ❌ 无 | 需新增列 |

### 4.2 建议新增列 (ServiceApi)

```sql
ALTER TABLE service_apis ADD COLUMN artifact_spec_json TEXT;           -- ArtifactSpec 完整 JSON
ALTER TABLE service_apis ADD COLUMN source_session_id VARCHAR(64);     -- 来源仿真 session
ALTER TABLE service_apis ADD COLUMN trace_hash VARCHAR(64);            -- 原始 trace 哈希
ALTER TABLE service_apis ADD COLUMN artifact_hash VARCHAR(64);         -- 产物哈希
ALTER TABLE service_apis ADD COLUMN schema_version VARCHAR(16);        -- ArtifactSpec schema 版本
```

前端 `to_dict()` 新增输出（条件输出，仅当非空）：
```python
if self.artifact_spec_json:
    result["artifactSpec"] = json.loads(self.artifact_spec_json)
if self.source_session_id:
    result["sourceSessionId"] = self.source_session_id
if self.trace_hash:
    result["traceHash"] = self.trace_hash
if self.artifact_hash:
    result["artifactHash"] = self.artifact_hash
if self.schema_version:
    result["schemaVersion"] = self.schema_version
```

前端 `service_create_model` (Flask-RESTX) 需同步新增字段。Repository 的 `create_service_with_relations` 和 `update_service_with_relations` 需在 `ServiceApi(...)` 构造中加入对应的 `api_data.get()` 映射。

---

## 5. 全链路数据流

```
Micro-Agent 仿真构建
  │
  ├─ SSE stream → complete.result (SSE)
  │     └─ 当前: { executionPath, strategy, appName, domain, toolChannels }
  │     └─ 目标: + { compiledApp | artifactRef }
  │
  ├─ finally → TraceRecord 落盘 (workspace/data/traces/)
  │     └─ tool_call_record + planner_decision + verifier_result + metadata v1.0.0
  │
  ├─ /evidence API → trace_evidence.run_pipeline() → 当前只返回摘要，未持久化
  │     └─ 目标: 调用 PipelineResult.save_to_dir() 持久化证据产物
  │
  └─ /artifact API (计划) → ArtifactSpec compiler → 落盘 artifacts/{session_id}/
        └─ 输入: TraceRecord + PipelineResult
        └─ 输出: artifact_spec.json

ioeb 前端
  │
  ├─ simulation_builder.vue
  │     └─ onStreamComplete → this.finalResult = result  (来自 SSE complete.result)
  │     └─ loadDetailArtifacts() → 拉取 /trace + /evidence
  │     └─ 目标: 同时拉取 /artifact，展示在 detail 面板中
  │     └─ handlePrePublish → $emit('success', { ..., result, artifactSpec? })
  │
  ├─ 父组件接收 → 调用 createService / prepublishService / updateService
  │     └─ 当前 payload: api_model (name/url/.../meta-app: subtitle/services/inputName/...)
  │     └─ 目标 payload: + { artifactSpec, sourceSessionId, traceHash, artifactHash, schemaVersion }
  │
  └─ ioeb_backend API
        └─ POST /services → ServiceApi 落盘
              └─ 当前: 6 个 meta-app 字段
              └─ 目标: + artifact_spec_json, source_session_id, trace_hash, artifact_hash, schema_version
```

---

## 6. 实施建议

按 GOAL_META_APP_SPEC.md §7 的顺序：

1. ~~通读 ioeb/ioeb_backend 元应用配置读写全链路~~ ✅ 已完成（本报告）
2. ~~定稿 ArtifactSpec v0 schema~~ ✅ `trace_evidence/schemas/artifact_spec_schema.json`
3. ~~Micro-Agent 端 trace→ArtifactSpec 编译+落盘~~ ✅ `artifact_compiler.py` + `GET/POST /artifact`（**代码未入库**）
4. **后端 schema 迁移 + 写回**（在 v0 稳定后单独决策）

**v0 最小可行策略**：ArtifactSpec 只编译+落盘 Micro-Agent 侧，通过 Micro-Agent API 暴露给前端展示；写回 ioeb_backend 的决策延后到 schema 稳定、迁移策略确定后。

---

## 7. 审计缺口（2026-06-08，支撑 PPT §7–10）

本报告视角：**从 Micro-Agent 产物到 ioeb 持久化** 的全链路是否闭合。

### 7.1 链路各环节现状

| 环节 | 声称能力 | 审计结论 |
|------|----------|----------|
| Trace 落盘 | 构建过程可复盘 | ✅ `FileTraceStore` v1.0.0 |
| Evidence 落盘 | 验证证据可检查 | ⚠️ `POST /evidence` 可 `save_to_dir`；仿真结束**不自动**触发 |
| Artifact 编译 | 元应用产物样例 | ⚠️ `artifact_compiler.py` + `/artifact` 已实现，**未 commit** |
| 前端拉取 | 可加载、可检查 | ❌ `loadDetailArtifacts` 仅 trace + evidence，**未接 `/artifact`** |
| complete.result | 构建完成可携带产物引用 | ❌ 无 `artifactRef` / `compiledApp` |
| prePublish 载荷 | 用户确认后可写回 | ❌ 仅 `finalResult`（executionPath 等），无 artifact 字段 |
| ioeb_backend 表 | 四类产物可持久化 | ❌ ServiceApi 无 artifact/trace 哈希列 |
| DB 迁移 | 新字段可上线 | ❌ 无 `migrations/` 目录 |

### 7.2 与 §10 固化研究的审计差距

写回链之外，固化研究还要求产物本体包含 **适用条件** 与 **压缩冗余**——当前 ArtifactSpec **均不包含**，故即使写回闭合，也无法支撑「路径复用 / 确定性执行 / 异常重规划」实验。

### 7.3 演示路径风险

| 路径 | trace/evidence/artifact |
|------|-------------------------|
| `【本地MCP】(n)` → Micro-Agent | 真实产物 |
| `课题` → inmemory mock | **无真实产物**（对外演示须避开此路径） |

### 7.4 审计项优先级

| 优先级 | 审计项 | 阻塞 |
|--------|--------|------|
| P0 | 产物代码入库；artifact 单测 | §8「可检查产物」无法复现 |
| P0 | 前端接 `/artifact` | §7–8 成果无法端到端演示 |
| P1 | `complete.result` 带 `artifactRef` | 构建完成态与产物脱节 |
| P2 | ioeb_backend 迁移 + 写回字段 | 工作块 B |
| P2 | 适用条件 + 压缩规则进 ArtifactSpec | §10 研究前提 |
