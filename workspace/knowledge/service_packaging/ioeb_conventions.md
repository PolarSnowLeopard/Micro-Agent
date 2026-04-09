# IoEB 平台服务封装约定

## 输出目录结构

封装完成后的输出目录应包含：

```
output/
├── server.py           # MCP 服务入口（必须）
├── requirements.txt    # Python 依赖（必须）
├── Dockerfile          # 容器化配置（必须）
├── docker-compose.yml  # 编排配置（必须）
└── [原始代码文件]       # 从输入目录复制（必须）
```

## 服务命名规范

- 服务名使用小写字母和连字符：`sentiment-analysis`、`image-classifier`
- MCP Tool 名使用 snake_case：`analyze_sentiment`、`classify_image`
- Docker 服务名与 MCP 服务名一致

## 端口规范

- MCP 服务默认端口：8000
- Docker 内部端口固定 8000，宿主机端口可配

## 依赖管理

- requirements.txt 中 MCP 相关依赖必须包含：mcp>=1.4.0, starlette, uvicorn
- 使用清华 PyPI 镜像源加速安装
- 不要包含开发依赖（pytest、black 等）

## 容器化规范

- 基础镜像：python:3.10-slim
- 工作目录：/app
- 环境变量 PYTHONUNBUFFERED=1（确保日志实时输出）
