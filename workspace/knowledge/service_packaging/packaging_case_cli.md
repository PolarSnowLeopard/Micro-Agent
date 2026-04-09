# 案例：CLI 工具封装为 MCP 服务

## 原始代码（predictor.py）

一个命令行 ML 推理工具：

```python
import argparse
import pickle
import numpy as np

def load_model(model_path):
    with open(model_path, 'rb') as f:
        return pickle.load(f)

def predict(model, features):
    features_array = np.array(features).reshape(1, -1)
    prediction = model.predict(features_array)
    probability = model.predict_proba(features_array)
    return {
        'prediction': int(prediction[0]),
        'probability': probability[0].tolist()
    }

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--features', nargs='+', type=float, required=True)
    args = parser.parse_args()
    model = load_model(args.model)
    result = predict(model, args.features)
    print(result)
```

## 分析过程

1. `load_model` 是辅助函数，在服务启动时调用一次
2. `predict` 是核心功能函数，适合封装为 MCP Tool
3. argparse 相关代码是 CLI 入口，不需要封装
4. 依赖：numpy, scikit-learn（pickle.load 的模型来自 sklearn）

## 封装后的 server.py

```python
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from mcp.server.sse import SseServerTransport
import pickle
import numpy as np
import json
import os

mcp = FastMCP("ml-predictor")
sse = SseServerTransport("/messages/")

MODEL_PATH = os.getenv("MODEL_PATH", "model.pkl")
_model = None

def _get_model():
    global _model
    if _model is None:
        with open(MODEL_PATH, 'rb') as f:
            _model = pickle.load(f)
    return _model

@mcp.tool()
async def predict(features_json: str) -> str:
    """使用 ML 模型进行预测。features_json: JSON 数组格式的特征值，如 "[1.0, 2.5, 3.0]"。
    返回预测类别和各类别概率。"""
    features = json.loads(features_json)
    model = _get_model()
    features_array = np.array(features).reshape(1, -1)
    prediction = model.predict(features_array)
    probability = model.predict_proba(features_array)
    return json.dumps({
        'prediction': int(prediction[0]),
        'probability': probability[0].tolist()
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

- CLI 参数 `--features 1.0 2.5 3.0` 变为 Tool 参数 `features_json: str`（JSON 数组字符串）
- 模型加载改为延迟加载单例模式，避免每次调用都 load
- 模型路径通过环境变量配置，便于 Docker 部署
- docker-compose.yml 中需要挂载模型文件或 COPY 到镜像
