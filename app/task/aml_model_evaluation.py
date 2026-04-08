from app.config import WORKSPACE_ROOT

def get_aml_model_evaluation_prompt(model_name: str, 
                                   zip_filename: str,
                                   metrics_list: list = None) -> str:
    """
    生成AML模型技术评测的提示词（原版，保持向后兼容）
    
    参数:
        model_name: 需要评测的模型名称
        zip_filename: 数据集文件路径
        metrics_list: 评测指标列表，默认为None（评测所有指标）
    
    返回:
        用于Agent的提示词字符串
    """
    if metrics_list is None:
        metrics_list = ["privacy", "safety-fingerprint", "safety-watermark", "fairness", "robustness", "explainability"]
    
    metrics_str = ", ".join(metrics_list)
    
    prompt = f"""对AML模型 '{model_name}' 进行技术评测。
    评测数据集位于: {zip_filename}
    请对该模型进行以下维度的评测: {metrics_str}

    你可以使用已接入的MCP 服务来完成任务

    不用解压评测数据，直接使用其访问远程服务的API端点。

    如果使用数据集文件访问MCP服务失败，可以直接用云上数据集url访问：https://lhcos-84055-1317429791.cos.ap-shanghai.myqcloud.com/ioeb/test_dataset.zip
    但是请注意，如果使用url请求MCP服务，必须将url作为MCP Tool的参数传入才能正确访问MCP工具
    此外，MCP Tool的Model Name 需要使用 `HattenGCN` 作为参数
    请将评测结果写入`{WORKSPACE_ROOT}/temp/model_evaluation_result.json`文件中。
    """
    return prompt


def get_aml_model_evaluation_prompt_with_adaptation(
    model_name: str,
    data_info: dict,
    metrics_list: list = None
) -> str:
    """
    生成包含数据适配功能的AML模型技术评测提示词
    
    参数:
        model_name: 需要评测的模型名称
        data_info: 数据信息字典，包含:
            - dataset_type: 数据集类型 ('0'=平台, '1'=上载, '2'=开源)
            - data_path: 数据集文件路径
            - data_url: 数据集URL（可选）
        metrics_list: 评测指标列表，默认为None
    
    返回:
        用于Agent的提示词字符串
    """
    if metrics_list is None:
        metrics_list = ["privacy", "fairness", "robustness"]
    
    metrics_str = ", ".join(metrics_list)
    dataset_type = data_info.get('dataset_type', 'uploaded')
    data_path = data_info.get('data_path', '')
    data_url = data_info.get('data_url', '')
    
    # 数据集类型描述
    dataset_type_desc = {
        '0': '平台数据集',
        '1': '用户上载数据集',
        '2': '开源数据集'
    }.get(dataset_type, '数据集')
    
    prompt = f"""你是一个专业的微服务评测Agent，具备强大的数据适配和服务评测能力。

##  任务目标

评测MCP服务: **{model_name}**
评测维度: **{metrics_str}**
数据来源: **{dataset_type_desc}**
数据路径: `{data_path}`
{f"数据URL: `{data_url}`" if data_url else ""}

##  执行流程（请严格按步骤执行）

###  第一阶段：智能数据适配

#### 步骤1 分析源数据
使用 `data_adaptation_mcp_analyze_data` MCP工具分析数据集：
- 调用方式: data_adaptation_mcp_analyze_data(data_path="{data_path}", sample_size=100)
- 目的: 了解数据格式、结构、字段等信息
- 输出: 仔细记录数据类型、列名、维度等关键特征

#### 步骤2 获取服务Schema要求
使用 `data_adaptation_mcp_get_service_schema` MCP工具查询服务的数据格式要求：
- 调用方式: data_adaptation_mcp_get_service_schema(service_name="{model_name}")
- 目的: 了解服务接受什么格式的数据
- 输出: 记录目标Schema和格式要求

#### 步骤3 兼容性分析
使用 `data_adaptation_mcp_analyze_schema_mapping` MCP工具分析数据兼容性：
- 调用方式: data_adaptation_mcp_analyze_schema_mapping(source_analysis=<步骤1的输出>, target_schema=<步骤2的输出>)
- 目的: 判断是否需要数据转换
- 决策逻辑:
  * 如果 compatibility.level == "compatible" → 跳到【第二阶段】
  * 如果 compatibility.level == "needs_conversion" → 继续步骤4
  * 如果 compatibility.level == "incompatible" → 报告错误并终止

#### 步骤4 生成转换代码（如需要）
使用 `data_adaptation_mcp_generate_transform_code` MCP工具生成转换代码：
- 调用方式: data_adaptation_mcp_generate_transform_code(
    mapping_analysis=<步骤3的输出>,
    source_path="{data_path}",
    output_path="{WORKSPACE_ROOT}/temp/converted_data"
  )
- 目的: 自动生成数据转换的Python代码
- 输出: 完整的可执行转换代码

#### 步骤5 执行数据转换
使用 `stdio_built_in_python_execute` 工具执行转换代码：
- 调用方式: stdio_built_in_python_execute(code=<步骤4生成的代码>, timeout=60)
- 目的: 将数据转换为服务所需格式
- 重要: 
  * 仔细检查执行结果，确认转换成功
  * 记录转换后的数据路径
  * 如果失败，分析错误原因并重试（最多3次）

#### 步骤6 验证转换结果（如进行了转换）
再次使用 `data_adaptation_mcp_analyze_data` MCP工具验证转换后的数据：
- 目的: 确保转换后的数据符合目标格式
- 如果验证失败，返回步骤4重新生成代码

---

###  第二阶段：MCP服务评测

#### 步骤7 准备评测数据
确定用于评测的数据路径：
- 如果进行了数据转换: 使用转换后的数据路径（步骤5的输出）
- 如果数据兼容: 使用原始数据路径 `{data_path}`

#### 步骤8 调用MCP评测工具
对每个评测指标，调用相应的MCP工具：

**可用的MCP评测工具：**
- **隐私性评测**: `project_4_mcp_EvaluatePrivacy`
  * 参数: model_name="HattenGCN", datasetUrl=<数据路径或URL>
  
- **公平性评测**: `project_4_mcp_EvaluateFairness`
  * 参数: model_name="HattenGCN", datasetUrl=<数据路径或URL>
  
- **鲁棒性评测**: `project_4_mcp_EvaluateRobustness`
  * 参数: model_name="HattenGCN", datasetUrl=<数据路径或URL>

**重要提示：**
- 如果本地文件路径失败，使用云端URL: `{data_url if data_url else "https://lhcos-84055-1317429791.cos.ap-shanghai.myqcloud.com/ioeb/test_dataset.zip"}`
- Model Name 固定使用 "HattenGCN"
- 每个工具调用后，仔细记录评测结果

#### 步骤9 保存评测结果
使用 `stdio_built_in_json_saver` 工具保存完整的评测报告：
- 保存路径: `{WORKSPACE_ROOT}/temp/model_evaluation_result.json`
- 结果格式:
```json
{{
  "model_name": "{model_name}",
  "dataset_info": {{
    "type": "{dataset_type_desc}",
    "original_path": "{data_path}",
    "url": "{data_url if data_url else 'N/A'}"
  }},
  "data_adaptation": {{
    "status": "success/skipped/failed",
    "original_format": "...",
    "target_format": "...",
    "conversion_applied": true/false,
    "converted_data_path": "..." 
  }},
  "evaluation_results": {{
    "privacy": {{"score": ..., "details": ..., "timestamp": "..."}},
    "fairness": {{"score": ..., "details": ..., "timestamp": "..."}},
    "robustness": {{"score": ..., "details": ..., "timestamp": "..."}}
  }},
  "summary": {{
    "total_metrics": ...,
    "average_score": ...,
    "completion_time": "..."
  }}
}}
```

#### 步骤10 完成任务
使用 `stdio_built_in_terminate` 工具标记任务完成：
- 调用方式: stdio_built_in_terminate(status="success")

---

## 💡 重要注意事项

1. **逐步执行**: 不要跳过任何步骤，严格按顺序执行
2. **详细思考**: 在每步的 thinking 中说明你的推理过程
3. **错误处理**: 
   - 如果工具调用失败，分析错误信息
   - 尝试不同的参数或方法
   - 最多重试3次，如仍失败则报告问题
4. **数据路径**: 
   - 使用绝对路径
   - 确保路径存在且可访问
   - 优先使用转换后的数据（如有）
5. **验证结果**: 
   - 每步完成后验证输出
   - 确保数据质量和格式正确
6. **清晰报告**:
   - 在最终结果中清楚说明是否进行了数据适配
   - 提供完整的评测摘要

## 🛠️ 可用工具列表

**数据适配MCP工具（data_adaptation_mcp服务器）：**
- `data_adaptation_mcp_analyze_data`: 分析数据格式和结构
- `data_adaptation_mcp_get_service_schema`: 获取服务的Schema要求
- `data_adaptation_mcp_analyze_schema_mapping`: 分析Schema映射兼容性
- `data_adaptation_mcp_generate_transform_code`: 生成数据转换代码

**代码执行工具（stdio_built_in服务器）：**
- `stdio_built_in_python_execute`: 执行Python代码
- `stdio_built_in_json_saver`: 保存JSON数据
- `stdio_built_in_bash`: 执行Shell命令
- `stdio_built_in_file_saver`: 保存文件
- `stdio_built_in_terminate`: 标记任务完成

**MCP评测工具（project_4_mcp服务器）：**
- `project_4_mcp_EvaluatePrivacy`: 隐私性评测
- `project_4_mcp_EvaluateFairness`: 公平性评测
- `project_4_mcp_EvaluateRobustness`: 鲁棒性评测

---

现在开始执行任务！请从【第一阶段 步骤1】开始。
"""
    
    return prompt 