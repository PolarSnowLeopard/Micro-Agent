"""AML 模型评测 — 增强版 prompt（支持数据适配）。

迁移自 Micro-Agent/app/task/aml_model_evaluation.py。
"""

from __future__ import annotations


def build_aml_evaluation_prompt_with_adaptation(
    *,
    model_name: str,
    data_info: dict,
    metrics_list: list[str] | None = None,
    workspace: str,
) -> str:
    if metrics_list is None:
        metrics_list = ["privacy", "fairness", "robustness"]

    metrics_str = ", ".join(metrics_list)
    dataset_type = data_info.get("dataset_type", "1")
    data_path = data_info.get("data_path", "")
    data_url = data_info.get("data_url", "")

    dataset_type_desc = {
        "0": "平台数据集",
        "1": "用户上载数据集",
        "2": "开源数据集",
    }.get(dataset_type, "数据集")

    url_line = f"数据URL: `{data_url}`" if data_url else ""
    fallback_url = data_url or "https://lhcos-84055-1317429791.cos.ap-shanghai.myqcloud.com/ioeb/test_dataset.zip"

    return f"""你是一个专业的微服务评测Agent，具备强大的数据适配和服务评测能力。

## 任务目标

评测MCP服务: **{model_name}**
评测维度: **{metrics_str}**
数据来源: **{dataset_type_desc}**
数据路径: `{data_path}`
{url_line}

## 执行流程（请严格按步骤执行）

### 第一阶段：智能数据适配

#### 步骤1 分析源数据
使用 `data_adaptation_mcp_analyze_data` MCP工具分析数据集：
- 调用: data_adaptation_mcp_analyze_data(data_path="{data_path}", sample_size=100)

#### 步骤2 获取服务Schema要求
使用 `data_adaptation_mcp_get_service_schema` MCP工具：
- 调用: data_adaptation_mcp_get_service_schema(service_name="{model_name}")

#### 步骤3 兼容性分析
使用 `data_adaptation_mcp_analyze_schema_mapping` MCP工具：
- 如果 compatibility.level == "compatible" → 跳到【第二阶段】
- 如果 compatibility.level == "needs_conversion" → 继续步骤4
- 如果 compatibility.level == "incompatible" → 报告错误并终止

#### 步骤4 生成转换代码（如需要）
使用 `data_adaptation_mcp_generate_transform_code` MCP工具。

#### 步骤5 执行数据转换
使用 bash 工具执行转换代码。如失败最多重试3次。

#### 步骤6 验证转换结果
再次用 `data_adaptation_mcp_analyze_data` 验证转换后数据。

---

### 第二阶段：MCP服务评测

#### 步骤7 准备评测数据
- 如进行了转换: 使用转换后的数据路径
- 如数据兼容: 使用原始路径 `{data_path}`

#### 步骤8 调用MCP评测工具
- **隐私性**: `project_4_mcp_EvaluatePrivacy`(model_name="HattenGCN", datasetUrl=<路径或URL>)
- **公平性**: `project_4_mcp_EvaluateFairness`(model_name="HattenGCN", datasetUrl=<路径或URL>)
- **鲁棒性**: `project_4_mcp_EvaluateRobustness`(model_name="HattenGCN", datasetUrl=<路径或URL>)

如本地路径失败，使用云端URL: `{fallback_url}`

#### 步骤9 保存评测结果
使用 bash 工具将结果写入 `{workspace}/temp/model_evaluation_result.json`。

#### 步骤10 完成任务
调用 terminate 结束任务。

---

现在开始执行任务，请从【第一阶段 步骤1】开始。
"""
