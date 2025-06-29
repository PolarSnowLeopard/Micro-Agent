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

1. 首先通过MCP服务器查询数据库，获取所有可用的MCP服务信息。这些服务在数据库中必须同时满足以下条件：
   - type字段值为'atomic_mcp'
   - domain字段值为'{service_type}'
   - 服务会关联api表和tool表

2. 根据用户的需求，分析哪些MCP服务和工具最适合用户的应用场景。

3. 推荐的服务和工具必须严格来源于数据库中的真实数据，不能有任何编造或虚构的内容。

4. 如果数据库中没有合适的服务能够满足用户需求，应该返回推荐失败的结果。

5. 将推荐结果按照指定的JSON格式保存到 `{WORKSPACE_ROOT}/temp/mcp_recommendation_result.json` 文件中。

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
        "id": "数据库中的实际ID",
        "name": "数据库中的实际服务名称",
        "des": "数据库中的实际服务描述",
        "url": "数据库中的实际服务URL",
        "method": "数据库中的实际方法类型",
        "attribute": "数据库中的实际属性",
        "type": "atomic_mcp",
        "domain": "数据库中的实际领域",
        "industry": "数据库中的实际行业",
        "scenario": "数据库中的实际场景",
        "technology": "数据库中的实际技术",
        "status": "数据库中的实际状态",
        "number": 数据库中的实际数字,
        "deleted": 数据库中的实际删除状态,
        "parameter_type": 数据库中的实际参数类型,
        "is_fake": 数据库中的实际is_fake值,
        "tools": [
          {{
            "id": "数据库中的实际工具ID",
            "name": "数据库中的实际工具名称",
            "des": "数据库中的实际工具描述"
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
- 如果没有找到合适的服务，务必返回失败结果，不要编造任何数据

现在开始执行推荐任务。""" 