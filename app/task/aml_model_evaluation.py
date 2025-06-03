from app.config import WORKSPACE_ROOT

def get_aml_model_evaluation_prompt(model_name: str, 
                                   zip_filename: str,
                                   metrics_list: list = None) -> str:
    """
    生成AML模型技术评测的提示词
    
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