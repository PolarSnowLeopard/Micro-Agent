from app.config import WORKSPACE_ROOT

def get_aml_report_prompt(workspace: str, 
                          input_dir: str) -> str:
    """
    生成AML报告任务的提示词
    
    参数:
        workspace: 工作区路径
        input_dir: 输入数据集文件路径
        
    返回:
        构建好的提示词字符串
    """
    prompt = f"""使用`aml_server`中的`gnn_predict_url`工具，对输入数据集进行AML风险预测。得到预测结果后，
    使用`deepseek_server`工具，对预测结果进行解释，并生成AML报告。

    输入数据集位于: {input_dir}

    请将AML报告写入 `{WORKSPACE_ROOT}/temp/aml_report.md` 文件中。
    """
    
    return prompt 