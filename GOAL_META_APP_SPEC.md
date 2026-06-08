# 全量提示词 — 元应用产物规格的持续演进

## 0. 你的角色与终局目标
你是 fdueblab 元应用仿真构建系统的演进负责人,**以持续演进的方式推进**:小步迭代、每步用证据收口、阶段性产物逐步逼近终局,不追求一次到位。
**终局产物**:一份「初版元应用产物规格(Meta-App Artifact Spec)」——把仿真构建过程中产生的轨迹/证据,沉淀为可写回元应用配置、可溯源、可被调度智能体复用的结构化产物。

这不是写一篇文档,而是:定义 schema → 改造代码使其真实产出该 schema → 验证产物能写回元应用配置并通过溯源校验。

## 1. 必须先接受的事实基线(已实证,勿重新质疑)
代码根:`/home/lyx/workspace/fdueblab`,子项目:`Micro-Agent`(构建引擎)、`ioeb`(前端)、`ioeb_backend`(后端)、`external-mcp`(MCP 服务)。

- **服务池**:5 个 MCP 全为医疗/生物医药域 —— medical-calc(18000,75+计算器)、linezolid(25013,单一剂量)、healthcovered(18001,美国ACA医保)、opentargets(靶点,未启动)、openfda(FDA药品,未启动)。跨域能力为零,场景多样性受限于此。
- **架构是真的**:Planner/Verifier 双 Agent 真实调 LLM;MCP 调用真实(channel=real_mcp);轨迹真实落盘。
- **轨迹已落盘**:`Micro-Agent/workspace/data/traces/*.json`。结构:
  `{session_id, app_name, domain, mode, strategy{minIterations,verificationMode}, events[{type,data,timestamp}], success, iterations, elapsed_ms, created_at, metadata{trace_version,config_snapshot,runtime,tool_call_count}}`
- **证据产物落盘(部分解决)**:`POST /api/simulation/{id}/evidence` 会 `save_to_dir`；仿真结束**不会自动**跑 evidence/artifact,需显式调用或前端触发。三套 pipeline 入口仍未收敛。
- **已知技术债**:① 证据 pipeline 三套入口重复(`__init__.py`/`run_pipeline.py`/`headless_run.py`);② `orchestrator.py` 902 行 God Class;③ 私有方法跨模块外泄(`api/routes/simulation.py` 调 `orchestrator._collect_call_records()`);④ 无服务注册中心,MCP 地址硬编码;⑤ 18 处吞异常(含 MCP cleanup 跨 task cancel scope bug,被 except 静默)。

## 2. 当前元应用配置的真实形态(产物的写入端,已读后端坐实)
前端形态(`ioeb/src/mock/data/meta_apps_data.js`):
```
{ preName(名称), nodeList:[ { id, name, domain, scenario, status, tools:[{id,name,description}] } ] }
```

**后端真实持久化(`ioeb_backend`,已读代码确认):**
- 元应用**没有独立表,复用 `ServiceApi` 表**(`app/models/service/service_api.py`,`class ServiceApi(db.Model)`)。
- 元应用专属字段仅这几个:`subtitle`(副标题)、`services`(Text,**使用的服务ID列表,逗号分隔**)、`input_name`、`output_name`、`output_visualization`(Bool)、`submit_button_text`;另关联 `parameters`(ServiceApiParameter)与 `tools`(ServiceApiTool,即节点工具)。
- 写回接口(`app/api/namespaces/service_ns.py`):`POST /services`(`ServiceList.post` 新建)、`POST /services/<id>`(`ServiceResource.post` 更新)、`POST /services/prepublish`(预发布);服务层落地 `service_service.create_service()` / `update_service()`。
- **加字段难度(写回新字段的第一道门槛)**:工程引入了 `Migrate()`(Flask-Migrate)但**仓库内无 migrations 目录**,`manage.py` 靠 `db.create_all()` 直建表;`create_all` 不改已存在的表。故新增场景/轨迹/哈希字段需先定迁移方案(引入 `flask db migrate/upgrade`,或开发库重建表),**改 schema 前必须先决策这一步**。

**结论**:后端当前只承载 副标题/服务ID列表/输入输出名/可视化开关/按钮文案 —— 实质即用户判断的 名称/描述/服务列表/版本状态。**完全没有**场景信息、轨迹数据、溯源哈希的落点,这三类都需新增字段或新增关联表。

## 3. 产物规格必须新增的内容(用户明确要求 + 推导)
在元应用配置中,除现有(名称/描述/服务列表/版本号)外,**至少新增**:

1. **场景信息(scenario)**:场景解析的最终结论。字段建议:`scenarioId, title, description, domain, parsedIntent, involvedServices[], sourceEvidenceRef`。
2. **类状态机的轨迹数据(stateMachineTrace)**:把仿真轨迹抽象为状态机。必须能表达:
   - 正常状态流转(state → transition → next state,每个 transition 绑定一次服务调用);
   - **异常态标记**:何时进入异常(断言失败/语义不通过/工具报错);
   - **退出固化的条件**:在哪个状态判定"不可固化,需退出",触发智能体重新介入调度(对应双 Agent 的 Verifier→Planner 回环)。
3. **溯源哈希(provenance)**:用于与服务器侧数据匹配/溯源。建议:`traceHash(原始trace内容哈希)、configSnapshotHash、artifactHash、sourceSessionId、createdAt`。

## 4. 完整流程需记录的过程材料(作为产物的证据链)
1. **场景解析结果**:作为最终场景信息的材料/证据,需保留原始输入→解析中间产物→结论的链路。
2. **轨迹数据(每次迭代)**:
   - 每轮迭代的**调度方案**(Planner 给出的调用链);
   - **仿真调用轨迹**(real_mcp 实际调用序列与返回);
   - **状态断言检查结果**(structural/state assertion);
   - **语义检查结果**(Verifier 的语义裁决)。
   这正是双 Agent「Planner 出链 → Verifier 审 → 纠偏 → 再出链」回环的逐轮快照,是状态机异常态/退出固化判断的数据来源。

## 5. 两大工作块(goal 模式的主线)
### 工作块 A:轨迹数据的记录与整理(进行中)
- 定义统一的 ArtifactSpec schema(覆盖 §3 + §4)。
- 让 API 主链路真实落盘证据产物(消除 §1 的关键缺口),优先收敛三套 pipeline 为一套。
- 从现有 trace 的 events 流中,抽取/重组出"每轮迭代的调度方案/调用轨迹/断言/语义检查",并归一为状态机轨迹。
### 工作块 B:整理好的产物写回元应用配置
- **先读 `ioeb` 前后端**确认元应用配置的读写接口(后端 model/repository/api、前端 mock/builder),搞清写回的真实通道与字段约束。
- 设计 ArtifactSpec → 元应用配置的映射(新增字段如何挂到现有结构,是否需后端 schema 迁移)。
- 实现写回 + 溯源哈希校验(产物哈希能与服务器侧数据匹配)。

## 6. 执行纪律
- **证据优先**:任何关于运行行为/数据结构的论断,先用工具读代码/跑验证,禁止臆断。已实证结论见 §1,不重复质疑。
- **持续演进、小步可逆**:把大目标拆成可独立验证的小步,改一处验一处;先做 schema 与落盘(可逆、低风险),后做跨服务写回(中风险,涉及后端 schema 需先确认);每步产出一个能跑/能看的中间物,再叠加下一步。
- **失败升级**:同一方法失败 2 次即停,改读环境/根因,第 3 次切换策略或问用户。
- **不扩大范围**:聚焦"产出规格 + 落盘 + 写回",不顺手重构无关代码;若必须动 902 行 God Class 或后端 schema,先说明影响再动。
- **领域局限要如实说**:产物 demo 仍只能用医疗场景;如需证明跨域,需先接入非医疗 MCP,属另一议题。

## 7. 第一步建议(起手)
1. ~~通读 `ioeb`/`ioeb_backend` 元应用配置的读写全链路,产出"现状字段 + 写回接口"清单。~~ → 见 `workspace/RECON_META_APP_READ_WRITE_CHAIN.md`
2. ~~据此定稿 ArtifactSpec v0 schema(JSON Schema)。~~ → 见 `trace_evidence/schemas/artifact_spec_schema.json`
3. ~~在 `Micro-Agent` 用一条真实 trace 跑通"trace → ArtifactSpec"的转换 + 落盘。~~ → `artifact_compiler.py` + `/artifact` API（**待入库**）
4. 再做"ArtifactSpec → 元应用配置写回 + 溯源哈希校验"。

## 8. 成果对标缺口（2026-06-08，对照 PPT §7–10）

对外成果叙事（§7 中间产物 → §8 四类产物 → §9 实验支撑 → **§10 固化研究**）与当前代码差距。工程排期见 `docs/simulation-build-roadmap.md`；读写链审计见 `workspace/RECON_META_APP_READ_WRITE_CHAIN.md`。

**范围外（不算缺口）**：构建完成后点**预发布**，在预发布表单填写名称、描述、类型、输入/输出格式等——现有产品流程已覆盖；**不在构建期**采集「用户补充约束」或更进一步的确认交互，本期不实现。

### 8.1 六阶段中间产物（§7）

| 阶段 | 声称产物 | 代码现状 | 判定 |
|------|----------|----------|------|
| 自然语言需求 | 原始需求、用户目标 | 仅 `scenarioDescription` 进 `config_snapshot` | ⚠️ 弱 |
| **想定解析** | 结构化场景（目标/环境/行为/约束/验证标准） | 无独立解析阶段；`parsedIntent` schema 有、编译器不填 | ❌ 缺 |
| 智能构建 | 服务匹配、候选调度、参数依赖、预期输出 | 服务匹配✅、`planner_decision`✅；参数依赖无；预期输出未记 | ⚠️ 半 |
| 仿真执行 | 调用过程、状态、回执、异常 | `tool_call_record` v1 信封✅ | ✅ 强 |
| 验证反馈 | 结论、**状态断言**、修正建议、修正历史 | `verifier_result` 仅语义裁决；无结构化断言 | ⚠️ 半 |
| 规格整理与预发布 | 产物样例；交付用元数据 | ArtifactSpec 样例✅；名称/描述/类型/I-O 在预发布表单✅ | ✅ |

### 8.2 元应用产物四类（§8）

| 类别 | 应含 | ArtifactSpec v0 现状 |
|------|------|---------------------|
| 场景与意图 | 目标/范围/约束/验证标准 | 仅 raw 描述 + 服务列表 |
| 服务与契约 | 绑定服务/参数约束/I-O 说明 | 仅 serviceId/name/channel |
| 验证与断言 | 结论/状态断言/执行记录引用 | 语义结论 + evidence_refs✅；状态断言❌ |
| 运行与交付 | 策略/异常入口/预览；交付元数据 | strategy✅、状态机异常态✅；预发布表单✅；异常处理入口❌ |

### 8.3 系统支撑实验（§9）

| 能力 | 现状 |
|------|------|
| 端到端真链路 | ✅ `【本地MCP】(n)` → Micro-Agent → 真实 MCP |
| 模块化解耦 | ⚠️ artifact 编译旁路解耦良好；orchestrator God Class；evidence 多入口 |
| 批处理接口 | ❌ 无批量 run / compile / compare |
| 演示分叉 | `课题` 走 inmemory mock，不产生真实 trace/evidence/artifact |

### 8.4 轨迹固化研究前提（§10）

固化依据 = **执行结构 + 适用条件 + 验证证据 + 异常处理 + 压缩冗余**。

| 要素 | 现状 | 差距 |
|------|------|------|
| 执行结构 | `stateMachineTrace` | 基本满足 |
| 验证证据 | `solidificationReport` 六道 gate | 二值门禁，非可复用依据本体 |
| 异常处理信息 | 状态机 `exception` | 缺运行时处理/重规划策略 |
| **适用条件** | 无 | **路径复用与确定性执行的前提缺失** |
| **冗余压缩** | 无 | 原始 trace 全量保留，失败尝试未剪枝 |

**结论**：当前可产出「带固化门禁结论的状态机轨迹」，尚非「可复用固化依据」。

### 8.5 与工作块 A/B 的映射

**工作块 A（轨迹记录与整理）**

| 项 | 状态 |
|----|------|
| ArtifactSpec v0 schema | ✅ |
| trace → ArtifactSpec 编译 | ✅ |
| stateMachineTrace + solidificationReport | ✅ |
| evidence 落盘 | ⚠️ `POST /evidence` 可落盘；仿真结束不自动触发 |
| parsedIntent / 状态断言 / 服务契约 | ❌ |
| artifact 单测 | ❌ |

**工作块 B（写回元应用配置）**

| 项 | 状态 |
|----|------|
| 读写链侦察 | ✅ RECON 报告 |
| ioeb_backend 新字段 + 迁移 | ❌ |
| 前端 `/artifact`；prePublish 携带 artifact 字段 | ❌（预发布表单元数据已有，缺 artifact 写回链） |
| 溯源哈希校验 | ❌ |

### 8.6 近期优先级

| 优先级 | 内容 |
|--------|------|
| **P0** | 产物代码入库；artifact 单测；前端接 `/artifact`；演示走真链路 |
| **P1** | 想定解析结构化；状态断言；服务契约 |
| **P2** | 适用条件 schema；冗余压缩规则；批处理接口；ioeb_backend 写回 |
| **P3** | 技术债：拆 orchestrator、收敛 evidence 入口、消除私有方法外泄 |
