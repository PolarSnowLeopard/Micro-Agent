# FastMCP SDK 使用指南

FastMCP 是 MCP 的 Python SDK，提供高层封装让开发者快速创建 MCP Server。

## 基本用法

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-service")

@mcp.tool()
async def add(a: int, b: int) -> str:
    """计算两个数的和。"""
    return str(a + b)
```

`@mcp.tool()` 装饰器会自动：
- 从函数名生成 Tool name
- 从 docstring 生成 description
- 从类型注解生成 inputSchema

## 与 Starlette 集成（SSE 模式）

```python
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from mcp.server.sse import SseServerTransport

sse = SseServerTransport("/messages/")

async def handle_sse(request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await mcp.run(streams[0], streams[1], mcp.create_initialization_options())

app = Starlette(routes=[
    Route("/sse", endpoint=handle_sse),
    Mount("/messages/", app=sse.get_asgi_app()),
])
```

## 处理复杂输入

如果 Tool 需要接收复杂结构，用 JSON 字符串：

```python
@mcp.tool()
async def process_data(data_json: str) -> str:
    """处理 JSON 格式的数据。data_json: JSON 字符串，包含 items 数组。"""
    import json
    data = json.loads(data_json)
    # 处理逻辑
    return json.dumps(result)
```

## 错误处理

Tool 内部应捕获异常并返回错误信息：

```python
@mcp.tool()
async def risky_operation(input: str) -> str:
    """执行可能失败的操作。"""
    try:
        result = do_something(input)
        return json.dumps({"success": True, "data": result})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
```
