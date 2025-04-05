from app.config import WORKSPACE_ROOT

services = """[
    {
    "name": '课题一风险识别模型推理微服务',
    "attribute": 1,
    "type": 0,
    "domain": 0,
    "industry": 0,
    scenario: 1,
    technology: 1,
    netWork: 'bridge',
    port: '0.0.0.0:8000/TCP → 0.0.0.0:80000',
    volume: '/var/opt/gitlab/mnt/user  →  /appdata/aml/data',
    status: 4,
    number: '512',
    norm: [
      {
        key: 0,
        score: 5
      },
      {
        key: 2,
        score: 5
      }
    ],
    source: {
      popoverTitle: '可信云技术服务溯源',
      companyName: '复旦大学课题组',
      companyAddress: '上海市杨浦区邯郸路220号',
      companyContact: '021-65642222',
      companyIntroduce: '课题一',
      msIntroduce: '基于智能体的风险识别算法',
      companyScore: 5,
      msScore: 5
    },
    apiList: [
      {
        name: 'predict',
        url: '/api/project1/predict',
        method: 'POST',
        des: '模型推理接口，基于数据集和参数配置得到风险识别结果',
        parameterType: 2,
        parameters: [
          {
            name: 'file',
            type: 'zip file',
            des: '数据集和参数配置文件的zip压缩包'
          }
        ],
        responseType: 1
      },
      {
        name: 'healthCheck',
        url: '/api/project1/health',
        method: 'GET',
        des: '判断微服务状态是否正常可用',
        parameterType: 0
      }
    ]
  }
]"""

def get_service_evaluation_prompt(service_name:str, 
                                  metrics_list:list, 
                                  zip_filename:str) -> str:
    prompt = f"""对远程服务 '{service_name}' 进行以下指标的评测: {', '.join(metrics_list)}。
    评测数据位于: {zip_filename}

    服务的信息如下所示，注意所有的api端点都是相对路径，其base_url为：`https://fdueblab.cn`
    {services}

    远程服务以REST API的形式进行访问，具体API端点参见服务信息中的`apiList`字段。

    你需要使用评测数据，通过访问远程服务中的API端点，对服务进行评测。

    不用解压评测数据，直接使用其访问远程服务的API端点。
    
    请将评测结果写入`{WORKSPACE_ROOT}/temp/evaluation_result.json`文件中。
    """
    return prompt