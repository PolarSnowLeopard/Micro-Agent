import os
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from openai import OpenAI
from typing import Optional, Dict, Any

# 加载环境变量
load_dotenv()

# 检查API密钥
API_KEY = os.environ.get("API_KEY")
BASE_URL = os.environ.get("BASE_URL", "https://openrouter.ai/api/v1")
MODEL = os.environ.get("MODEL", "deepseek/deepseek-chat-v3-0324:free")

if not API_KEY:
    raise ValueError("API_KEY 环境变量必须设置")

# 创建 MCP 服务器
mcp = FastMCP("deepseek_r1", version="1.0.0")

# 创建OpenAI客户端用于调用DeepSeek API
openai_client = OpenAI(
    api_key=API_KEY,
    base_url=BASE_URL
)

@mcp.tool()
async def deepseek_r1(prompt: str, max_tokens: int = 8192, temperature: float = 0.2) -> Dict[str, Any]:
    """使用DeepSeek R1模型生成文本
    
    Args:
        prompt: DeepSeek的输入文本
        max_tokens: 最大生成令牌数，范围1-8192，默认8192
        temperature: 采样温度，范围0-2，默认0.2
    """
    try:
        completion = openai_client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Vous êtes un assistant intelligent et polyvalent."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=max_tokens,
            temperature=temperature
        )
        
        return {
            "content": [{
                "type": "text",
                "text": completion.choices[0].message.content or "无响应"
            }]
        }
    except Exception as e:
        print(f"DeepSeek API 错误: {str(e)}")
        return {
            "content": [{
                "type": "text",
                "text": f"DeepSeek API 错误: {str(e)}"
            }],
            "isError": True
        }

if __name__ == "__main__":
    # 使用stdio作为传输层运行服务器
    mcp.run(transport='stdio')
    print("DeepSeek R1 MCP 服务器在stdio上运行")
