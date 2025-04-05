from app.config import WORKSPACE_ROOT

def get_meta_app_validation_prompt(meta_app_api: str, 
                                     metrics_list: list, 
                                     zip_filename: str) -> str:
    """
    生成元应用数据验证任务的提示词
    
    参数:
        meta_app_api: 待测试的元应用的API端点（SSE端点）
        metrics_list: 需要评测的指标列表（查全率/查准率/计算效率中的一个或多个）
        zip_filename: 数据集文件路径
        
    返回:
        构建好的提示词字符串
    """
    prompt = f"""对元应用 API '{meta_app_api}' 进行以下指标的数据验证评测: {', '.join(metrics_list)}。
    评测数据位于: {zip_filename}

    元应用API以SSE（Server-Sent Events）流式接口的形式提供服务，你需要使用评测数据通过访问该API端点对元应用进行数据验证评测。
    
    评测指标说明：
    - 查全率：衡量元应用能够正确识别所有相关数据项的能力，计算公式为：正确识别的相关项数量 / 所有相关项数量
    - 查准率：衡量元应用识别结果的准确性，计算公式为：正确识别的相关项数量 / 识别的所有项数量
    - 计算效率：衡量元应用处理数据的速度和资源使用情况
    
    请按照以下步骤进行评测：
    1. 解压数据集文件，了解数据结构和格式
    2. 根据评测指标，设计合适的测试方案
    3. 调用元应用API，收集评测数据
    4. 分析结果并计算相关指标得分
    5. 将评测结果写入指定的输出文件

    **由于目前元应用 API尚不可用，因此请直接返回mock的评测结果**
    
    请将评测结果写入 `{WORKSPACE_ROOT}/temp/validation_result.json` 文件中。
    """
    return prompt 