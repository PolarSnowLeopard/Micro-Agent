import os
from app.config import WORKSPACE_ROOT

def get_mcp_service_recommendation_prompt(message: str, service_type: str):
    """
    生成MCP服务推荐任务的prompt
    
    参数：
        message: 用户的需求描述
        service_type: 服务类型，用于过滤domain字段
    
    返回：
        生成的prompt字符串
    """
    return f"""你是一个专业的MCP服务推荐Agent，你的任务是根据用户的需求推荐合适的MCP服务来构成应用。

用户需求：{message}
服务类型：{service_type}

你需要完成以下任务：

1. 首先通过MCP服务器查询数据库，获取所有可用的MCP服务信息。数据库包含以下三个相关表：
   - `services` - 服务表，包含服务基本信息
   - `service_apis` - API表，关联到services表
   - `service_api_tools` - 工具表，关联到service_apis表

2. 使用以下SQL查询获取符合条件的服务数据：
   ```sql
   SELECT s.id, s.name, s.attribute, s.type, s.domain, s.industry, s.scenario, s.technology, s.status, s.number, s.deleted, 
          a.name as api_name, a.url, a.method, a.des, a.parameter_type, a.response_type, a.is_fake,
          t.id as tool_id, t.name as tool_name, t.description as tool_description
   FROM services s
   LEFT JOIN service_apis a ON s.id = a.service_id  
   LEFT JOIN service_api_tools t ON a.id = t.api_id
   WHERE s.type = 'atomic_mcp' AND s.domain = '{service_type}' AND s.deleted = 0;
   ```

3. 根据用户所有的需求，分析哪些MCP服务和工具最适合用户的应用场景。例如，用户想基于课题一的算法生成一个跨境支付报告生成应用，那么就需要获取课题一的风险识别模型推理服务以及一个报告生成服务。

4. 推荐的服务和工具必须严格来源于数据库中的真实数据，不能有任何编造或虚构的内容。

5. 如果数据库中没有合适的服务能够满足用户需求，应该返回推荐失败的结果。

6. 将推荐结果按照指定的JSON格式保存到 `{WORKSPACE_ROOT}/temp/mcp_recommendation_result.json` 文件中。

输出格式要求：
- 文件内容必须是严格的JSON格式
- 包含success（布尔值）和result两个字段
- 如果推荐失败，success为false，result为null
- 如果推荐成功，success为true，result按照以下格式：

{{
  "success": true,
  "result": {{
    "preName": "根据用户需求生成的元应用名称",
    "preInputName": "根据用户需求生成的输入数据名称",
    "preOutputName": "根据用户需求生成的输出数据名称", 
    "inputType": 2,
    "outputType": 1,
    "nodeList": [
      {{
        "id": "数据库中services表的实际ID",
        "name": "数据库中services表的实际服务名称",
        "des": "数据库中service_apis表的实际API描述(des字段)",
        "url": "数据库中service_apis表的实际API地址",
        "method": "数据库中service_apis表的实际请求方法",
        "attribute": "数据库中services表的实际属性",
        "type": "atomic_mcp",
        "domain": "数据库中services表的实际领域",
        "industry": "数据库中services表的实际行业",
        "scenario": "数据库中services表的实际场景",
        "technology": "数据库中services表的实际技术",
        "status": "数据库中services表的实际状态",
        "number": "数据库中services表的实际调用次数",
        "deleted": "数据库中services表的实际删除状态",
        "parameter_type": "数据库中service_apis表的实际参数类型",
        "response_type": "数据库中service_apis表的实际响应类型",
        "is_fake": "数据库中service_apis表的实际is_fake值",
        "tools": [
          {{
            "id": "数据库中service_api_tools表的实际工具ID",
            "name": "数据库中service_api_tools表的实际工具名称",
            "description": "数据库中service_api_tools表的实际工具描述"
          }}
        ]
      }}
    ]
  }}
}}

请注意：
- 所有的服务和工具信息都必须来源于数据库的真实数据
- preName、preInputName、preOutputName要根据用户需求生成合适的名称
- inputType固定为2，outputType固定为1
- 字段映射关系：
  * 服务基本信息来自services表：id, name, attribute, type, domain, industry, scenario, technology, status, number, deleted
  * API信息来自service_apis表：url, method, des, parameter_type, response_type, is_fake
  * 工具信息来自service_api_tools表：id, name, description
- 如果没有找到合适的服务，务必返回失败结果，不要编造任何数据

现在开始执行推荐任务。""" 