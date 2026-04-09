# 案例：Flask 应用封装为 MCP 服务

## 原始代码（app.py）

一个 Flask 应用，提供文本情感分析 API：

```python
from flask import Flask, request, jsonify
from textblob import TextBlob

app = Flask(__name__)

@app.route('/analyze', methods=['POST'])
def analyze_sentiment():
    text = request.json.get('text', '')
    blob = TextBlob(text)
    return jsonify({
        'polarity': blob.sentiment.polarity,
        'subjectivity': blob.sentiment.subjectivity
    })

if __name__ == '__main__':
    app.run(port=5000)
```

## 分析过程

1. 识别核心功能函数：`analyze_sentiment` 内部的情感分析逻辑
2. Flask 路由处理函数本身不适合直接封装，需要提取核心逻辑
3. 依赖：textblob

## 封装后的 server.py

```python
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from mcp.server.sse import SseServerTransport
from textblob import TextBlob
import json

mcp = FastMCP("sentiment-analysis")
sse = SseServerTransport("/messages/")

@mcp.tool()
async def analyze_sentiment(text: str) -> str:
    """分析文本的情感极性和主观性。text: 要分析的文本内容。
    返回 JSON，包含 polarity（-1到1的极性分数）和 subjectivity（0到1的主观性分数）。"""
    blob = TextBlob(text)
    return json.dumps({
        'polarity': blob.sentiment.polarity,
        'subjectivity': blob.sentiment.subjectivity
    })

async def handle_sse(request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await mcp.run(streams[0], streams[1], mcp.create_initialization_options())

starlette_app = Starlette(routes=[
    Route("/sse", endpoint=handle_sse),
    Mount("/messages/", app=sse.get_asgi_app()),
])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(starlette_app, host="0.0.0.0", port=8000)
```

## 关键转换要点

- Flask 的 `request.json.get('text')` 变为 Tool 参数 `text: str`
- Flask 的 `jsonify()` 返回变为 `json.dumps()` 字符串返回
- 去掉了 Flask 框架依赖，改用 Starlette + FastMCP
- requirements.txt 中 flask 替换为 mcp、starlette、uvicorn
