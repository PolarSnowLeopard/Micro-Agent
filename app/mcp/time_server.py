from mcp.server.fastmcp import FastMCP
from datetime import datetime

# 创建 MCP 服务器
mcp = FastMCP("MCP 服务器示例")

# 添加获取服务器时间的工具
@mcp.tool()
async def get_server_time() -> str:
    """获取服务器当前时间"""
    return datetime.now().isoformat()

if __name__ == "__main__":
    mcp.run(transport='stdio')
