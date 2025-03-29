from fastapi import FastAPI
from fastapi_mcp import add_mcp_server
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
from mcp.server.sse import SseServerTransport
from mcp.server import Server
import uvicorn
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="代码解析-微服务封装-远程部署 Agent调用",
    description="run ioeb agent",
    version="1.0",
    docs_url="/",
)

# 添加CORS支持
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 创建 MCP 服务器并启用高级配置
mcp_server = add_mcp_server(
    app,
    mount_path="/mcp",
    name="My API MCP",
    describe_all_responses=True,   # 显示所有可能的响应模式
    describe_full_response_schema=True   # 提供完整 JSON Schema
)

# 额外添加一个 MCP 工具（获取服务器时间）
@mcp_server.tool()
async def get_server_time() -> str:
    """获取服务器当前时间"""
    from datetime import datetime
    return datetime.now().isoformat()

# 添加SSE传输支持
def setup_sse_endpoints(app: FastAPI, mcp_server: Server):
    """为FastAPI应用添加SSE端点"""
    sse = SseServerTransport("/sse-messages/")
    
    @app.get("/sse")
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
    
    # 挂载消息处理器
    app.mount("/sse-messages/", sse.handle_post_message)
    
    return app

if __name__ == "__main__":
    import uvicorn
    
    # 获取原始MCP服务器对象
    raw_mcp_server = mcp_server._mcp_server
    
    # 设置SSE端点
    setup_sse_endpoints(app, raw_mcp_server)
    
    print("启动服务：")
    print("- FastAPI文档: http://localhost:8800/")
    print("- MCP API: http://localhost:8800/mcp")
    print("- SSE端点: http://localhost:8800/sse")
    
    # 运行FastAPI应用（包含SSE支持）
    uvicorn.run(app, host="localhost", port=8800)

    # python dev_mcp/fastapi_mcp_demo.py