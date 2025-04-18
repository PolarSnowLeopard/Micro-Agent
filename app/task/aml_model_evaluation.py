from app.config import WORKSPACE_ROOT

services = [
    {
        "name": '课题四模型评测-安全性指纹微服务',
        "attribute": 2,
        "type": 0,
        "domain": 0,
        "industry": 3,
        "scenario": 4,
        "technology": 4,
        "netWork": 'bridge',
        "port": '0.0.0.0:8006/TCP → 0.0.0.0:80006',
        "volume": '/var/opt/gitlab/mnt/user  →  /appdata/aml/data',
        "status": 4,
        "number": '2342',
        "norm": [
            {
                "key": 1,
                "score": 5
            },
            {
                "key": 2,
                "score": 5
            },
            {
                "key": 3,
                "score": 5
            }
    ],
    "source": {
        "popoverTitle": '可信云技术服务溯源',
        "companyName": '复旦大学课题组',
        "companyAddress": '上海市杨浦区邯郸路220号',
        "companyContact": '021-65642222',
        "companyIntroduce": '课题四',
        "msIntroduce": '安全性指纹测评算法',
        "companyScore": 5,
        "msScore": 5
    },
    "apiList": [
        {
            "name": 'safety-fingerprint',
            "url": '/api/project4/safety/safety-fingerprint',
            "method": 'POST',
            "parameterType": 2,
            "parameters": [
                {
                    "name": 'file',
                    "type": 'file',
                    "des": '数据集和配置文件的zip压缩包'
                },
                {
                    "name": 'model_name',
                    "type": 'string',
                    "des": '模型名称，目前只支持HattenGCN'
                }
            ],
            "responseType": 1
        },
        {
            "name": 'healthCheck',
            "url": '/api/project4/safety/health',
            "method": 'GET',
                "parameterType": 0
            }
        ]
    }
]

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
    
    请将评测结果写入`{WORKSPACE_ROOT}/temp/model_evaluation_result.json`文件中。
    """
    return prompt 