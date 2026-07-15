# MCP Packager v2（独立验证阶段）

`mcp_packager` 是一个不依赖平台前端、数据库或 Agent 提示词的确定性封装引擎。它把符合 IoEB 提交规范的 Python 算法包编译成 MCP 服务，并可在一次性 Docker 容器中进行真实协议验证。

## 当前边界

- 仅支持 Python 单文件、目录或 ZIP。
- 项目入口固定为 `main.py`，核心函数固定为 `main_process`。
- MCP Tool 参数和返回值必须有 JSON 兼容的完整类型注解。
- ZIP 不允许路径穿越或符号链接。
- 生产严格模式要求依赖精确锁定，并要求机器可读的服务元数据和测试样例。
- 服务工程、Dockerfile 和 Compose 均由平台模板生成，不接受 LLM 或用户自由生成的部署配置。

## 现有平台接入

在不修改前端交互和返回字段的前提下，现有发布链路已经切换到确定性引擎：

1. 前端仍调用 `/api/agent/code_analysis`，服务端校验算法包并把 `main_process` 投影成原有 `nodes`/`edges` 函数图。
2. 前端仍调用 `/api/agent/service_packaging`，服务端生成并静态验证 MCP 工程，再按原有 `service_package` Base64 ZIP 字段返回。
3. 前端仍把 ZIP 提交到后端 `/services/upload`；后端继续使用当前异步 Docker Compose 部署逻辑。
4. 后端从生成包的 `ioeb-service.json` 读取真实 Tool、`/mcp` 端点和 Streamable HTTP 传输方式。没有 v1 清单的历史服务仍按 `/sse` 处理。

上传带 `ioeb_algorithm.json` 的 ZIP 时自动使用生产严格配置。单个 `.py` 文件暂时保留旧模板兼容配置，校验报告中的 `productionReady` 为 `false`；可靠部署第三方依赖时应使用带精确锁定 `requirements.txt` 的生产 ZIP。

目前封装接口执行静态产物验证，随后由现有后端执行真实镜像构建和启动。前端收到“发布成功”表示部署任务已经被后端接受，并不代表容器已完成健康检查；任务状态与部署队列属于后续系统侧改造范围。

可用以下命令生成一个前端冒烟测试 ZIP：

```bash
cd tests/fixtures/mcp_packager_valid
zip -r /tmp/ioeb-repeat-text-algorithm.zip main.py requirements.txt ioeb_algorithm.json
```

## 使用方法

```bash
python -m mcp_packager init --output ./algorithm
python -m mcp_packager validate ./algorithm --strict
python -m mcp_packager plan ./algorithm --strict
python -m mcp_packager build ./algorithm --strict --output ./dist/service
python -m mcp_packager verify ./dist/service
python -m mcp_packager verify ./dist/service --docker
python -m mcp_packager score ./verification.json
python -m mcp_packager batch ./benchmarks/amq_template \
  --docker --no-cache --output ./batch-report.json
```

不加 `--strict` 时兼容现有提交文档：缺少生产清单或依赖文件会产生警告，报告中的 `productionReady` 为 `false`。严格模式会在生成前拒绝这类输入。

两种验证配置及迁移边界见 [IoEB Algorithm Submission Contracts](./algorithm-submission-contract.md)。兼容模式允许旧文档中的 `Optional[Dict]` 等模糊泛型用于预览，但这类输入不能进入生产发布门禁。

## 生产清单

严格模式要求算法根目录包含 `ioeb_algorithm.json`：

```json
{
  "specVersion": "ioeb.algorithm-package/v1",
  "service": {
    "name": "repeat-text",
    "description": "Repeat text a requested number of times."
  },
  "entrypoint": "main:main_process",
  "parameterConstraints": {
    "text": {"minLength": 1, "maxLength": 10000},
    "repeat": {"minimum": 1, "maximum": 10}
  },
  "tests": [
    {
      "name": "repeat-twice",
      "arguments": {"text": "IoEB", "repeat": 2},
      "expected": {"value": "IoEBIoEB", "length": "8"}
    }
  ]
}
```

测试样例有两个用途：验证原算法输出是否符合提交者声明，以及比较直接调用与 MCP Tool 调用的结果是否一致。二者任一不一致，Docker 验证都会失败。

`parameterConstraints` 会同时进入包装计划、Pydantic 参数校验和 MCP `tools/list` JSON Schema。除顶层数值、字符串和数组范围外，也支持对象 `properties`/`required`/`additionalProperties`、数组 `items`/`prefixItems` 和嵌套字段约束。测试输入如果违反类型或范围，会在 Docker 构建前被拒绝，MCP 运行时也会执行同一套约束。

## 验证层级

普通 `verify` 执行文件完整性、Python 语法、Artifact JSON 和 MCP SDK 版本范围检查。

`verify --docker` 额外执行：

1. 构建固定模板镜像。
2. 使用非 root、只读文件系统、无 Linux capabilities、资源配额和无外网容器启动服务。
3. 建立 MCP Streamable HTTP 连接并执行 `initialize`。
4. 执行 `tools/list` 并检查 `main_process`。
5. 对每个清单样例分别直接调用原函数和调用 MCP Tool。
6. 比较声明结果、原函数结果和 MCP 返回结果。
7. 对缺失参数、类型错误、枚举/范围错误和嵌套约束错误执行主动探测；任一请求未被明确拒绝都禁止发布。

Docker 验证的算法执行默认最多 120 秒，可通过 `--execution-timeout` 调整。超时、依赖安装、导入、协议和功能不一致会分别归因。

Docker 验证仍然是本地研发验证器，不等同于正式生产沙箱；包含非可信依赖构建时，正式系统应把构建 Worker 放在独立节点或虚拟机中。

## AMQ-Bench 指标

生成后的 runtime 或 Docker verification report 可以通过 `score` 转换为严格的 AMQ 兼容质量报告，包含 D1 Availability、D2 Usability、D3 Utility、AQS 和失败分类。严格配置只接受确定性测试 oracle，不使用 LLM 或关键词 fallback。

数据筛选、双轨评测和 holdout 规则见 [AMQ-Bench Integration Design](./amq-bench-integration.md)。
