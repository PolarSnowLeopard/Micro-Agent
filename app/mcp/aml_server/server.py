import os
import logging
import requests
import asyncio
from typing import Dict, List, Any, Optional
from mcp.server.fastmcp import FastMCP

from dotenv import load_dotenv

load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("aml_mcp_server")

# 创建 MCP 服务器
mcp = FastMCP("aml_mcp_server")

def get_config():
    """从环境变量获取微服务配置"""
    config = {
        "gnn_predict_url": os.getenv("GNN_PREDICT_URL", "https://fdueblab.cn/api/project1/predict"),
        "nl2gql_url": os.getenv("NL2GQL_URL", "https://fdueblab.cn/api/project3/nl2gql"),
        "report_url": os.getenv("REPORT_URL", "https://fdueblab.cn/api/project3/generate-report"),
        "safety_url": os.getenv("SAFETY_URL", "https://fdueblab.cn/api/project4/safety/safety-fingerprint")
    }
    
    # 打印配置信息，但不显示任何可能的敏感信息
    logger.info(f"已加载API配置。微服务端点: {', '.join(config.keys())}")
    
    return config

# 微服务1和微服务2的 /api/predict 接口
@mcp.tool()
async def gnn_predict(file_path: str) -> Dict[str, Any]:
    """使用GNN进行预测
    
    Args:
        file_path: 数据集ZIP文件的路径
    """
    config = get_config()
    logger.info(f"发送GNN预测请求，使用文件: {file_path}")
    
    if not os.path.exists(file_path):
        logger.error(f"文件不存在: {file_path}")
        return {
            "content": [{
                "type": "text",
                "text": f"错误: 文件不存在 {file_path}"
            }],
            "isError": True
        }
    
    try:
        with open(file_path, 'rb') as f:
            files = {'file': (os.path.basename(file_path), f, 'application/zip')}
            response = requests.post(config["gnn_predict_url"], files=files)
            
        if response.status_code == 200:
            result = response.json()
            return {
                "content": [{
                    "type": "text",
                    "text": f"预测成功:\n{result}"
                }]
            }
        else:
            error_msg = f"API错误 (HTTP {response.status_code}): {response.text}"
            logger.error(error_msg)
            return {
                "content": [{
                    "type": "text",
                    "text": error_msg
                }],
                "isError": True
            }
            
    except Exception as e:
        logger.error(f"执行GNN预测时发生错误：{str(e)}")
        return {
            "content": [{
                "type": "text",
                "text": f"执行GNN预测时发生错误：{str(e)}"
            }],
            "isError": True
        }

# # 微服务3的 /api/nl2gql 接口
# @mcp.tool()
# async def nl2gql(query: str) -> Dict[str, Any]:
#     """将自然语言查询转换为图数据库查询并执行
    
#     Args:
#         query: 自然语言查询
#     """
#     config = get_config()
#     logger.info(f"发送NL2GQL请求: {query}")
    
#     try:
#         response = requests.get(config["nl2gql_url"], params={"query": query})
        
#         if response.status_code == 200:
#             result = response.json()
#             return {
#                 "content": [{
#                     "type": "text",
#                     "text": f"查询成功:\n{result}"
#                 }]
#             }
#         else:
#             error_msg = f"API错误 (HTTP {response.status_code}): {response.text}"
#             logger.error(error_msg)
#             return {
#                 "content": [{
#                     "type": "text",
#                     "text": error_msg
#                 }],
#                 "isError": True
#             }
            
#     except Exception as e:
#         logger.error(f"执行NL2GQL查询时发生错误：{str(e)}")
#         return {
#             "content": [{
#                 "type": "text",
#                 "text": f"执行NL2GQL查询时发生错误：{str(e)}"
#             }],
#             "isError": True
#         }

# # 微服务3的 /api/generate-report 接口
# @mcp.tool()
# async def generate_report(query: str) -> Dict[str, Any]:
#     """生成金融风险报告
    
#     Args:
#         query: 查询语句
#     """
#     config = get_config()
#     logger.info(f"生成风险报告: {query}")
    
#     try:
#         response = requests.get(config["report_url"], params={"query": query})
        
#         if response.status_code == 200:
#             # 假设API直接返回PDF文件
#             # 在实际实现中，可能需要将文件保存到磁盘或处理其他返回格式
#             return {
#                 "content": [{
#                     "type": "text",
#                     "text": "报告生成成功。"
#                 }]
#             }
#         else:
#             error_msg = f"API错误 (HTTP {response.status_code}): {response.text}"
#             logger.error(error_msg)
#             return {
#                 "content": [{
#                     "type": "text",
#                     "text": error_msg
#                 }],
#                 "isError": True
#             }
            
#     except Exception as e:
#         logger.error(f"生成报告时发生错误：{str(e)}")
#         return {
#             "content": [{
#                 "type": "text",
#                 "text": f"生成报告时发生错误：{str(e)}"
#             }],
#             "isError": True
#         }

# # 微服务4的 /safety/safety-fingerprint 接口
# @mcp.tool()
# async def safety_fingerprint(file_path: str, model_name: str) -> Dict[str, Any]:
#     """对指定模型进行安全性指纹检测
    
#     Args:
#         file_path: 数据集ZIP文件的路径
#         model_name: 要评测的模型名称(目前仅支持 HattenGCN)
#     """
#     config = get_config()
#     logger.info(f"发送安全性指纹检测请求，使用文件: {file_path}, 模型: {model_name}")
    
#     if not os.path.exists(file_path):
#         logger.error(f"文件不存在: {file_path}")
#         return {
#             "content": [{
#                 "type": "text",
#                 "text": f"错误: 文件不存在 {file_path}"
#             }],
#             "isError": True
#         }
    
#     try:
#         with open(file_path, 'rb') as f:
#             files = {'file': (os.path.basename(file_path), f, 'application/zip')}
#             data = {'model_name': model_name}
#             response = requests.post(config["safety_url"], files=files, data=data)
            
#         if response.status_code == 200:
#             result = response.json()
#             return {
#                 "content": [{
#                     "type": "text",
#                     "text": f"安全性指纹检测成功:\n{result}"
#                 }]
#             }
#         else:
#             error_msg = f"API错误 (HTTP {response.status_code}): {response.text}"
#             logger.error(error_msg)
#             return {
#                 "content": [{
#                     "type": "text",
#                     "text": error_msg
#                 }],
#                 "isError": True
#             }
            
#     except Exception as e:
#         logger.error(f"执行安全性指纹检测时发生错误：{str(e)}")
#         return {
#             "content": [{
#                 "type": "text",
#                 "text": f"执行安全性指纹检测时发生错误：{str(e)}"
#             }],
#             "isError": True
#         }

# 提供资源列表
@mcp.resource("aml://services")
async def list_services() -> str:
    """列出可用的AML微服务"""
    services = [
        "1. GNN预测服务 - 使用神经网络进行图数据预测",
        # "2. NL2GQL服务 - 将自然语言转换为图查询语言",
        # "3. 报告生成服务 - 生成金融风险报告",
        # "4. 安全性指纹检测 - 评估模型安全性"
    ]
    
    return "\n".join(["可用的AML微服务:"] + services)

if __name__ == "__main__":
    # 使用stdio作为传输层运行服务器
    logger.info("启动AML MCP服务器...")
    
    try:
        config = get_config()
        mcp.run(transport='stdio')
    except Exception as e:
        logger.error(f"服务器错误：{str(e)}", exc_info=True)
        raise
