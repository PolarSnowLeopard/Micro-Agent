from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
from mcp.server.sse import SseServerTransport
from mcp.server import Server
import uvicorn
from dotenv import load_dotenv
from datetime import datetime
import os

# 加载环境变量
load_dotenv()

# 创建 MCP 服务器
mcp = FastMCP("MCP 服务器示例")

# 添加获取服务器时间的工具
@mcp.tool()
async def get_server_time() -> str:
    """获取服务器当前时间"""
    return datetime.now().isoformat()

# 创建支持SSE的Starlette应用
def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
    """创建一个支持SSE传输的Starlette应用"""
    sse = SseServerTransport("/messages/")
    
    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(
                request.scope,
                request.receive,
                request._send,  # noqa: SLF001
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )
    
    return Starlette(
        debug=debug,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='运行MCP服务器（SSE传输）')
    parser.add_argument('--host', default='0.0.0.0', help='绑定的主机')
    parser.add_argument('--port', type=int, default=8800, help='监听的端口')
    args = parser.parse_args()
    
    # 获取MCP服务器
    mcp_server = mcp._mcp_server
    
    # 创建Starlette应用
    starlette_app = create_starlette_app(mcp_server, debug=True)
    
    print("启动MCP服务器...")
    print(f"- SSE端点: http://{args.host if args.host != '0.0.0.0' else 'localhost'}:{args.port}/sse")
    
    # 运行服务器
    uvicorn.run(starlette_app, host=args.host, port=args.port)
