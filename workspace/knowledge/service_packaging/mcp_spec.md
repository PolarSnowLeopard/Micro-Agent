# MCP 协议核心概念

MCP（Model Context Protocol）是一种标准化的协议，用于将外部工具和数据源接入大语言模型。

## 三类能力

MCP 服务可以暴露三类能力：

1. **Tools（工具）**：可被 LLM 调用执行操作的函数。类似于 OpenAI function calling，但标准化了协议格式。
2. **Resources（资源）**：可被读取的静态或动态数据源，如文件内容、数据库记录。
3. **Prompts（提示模板）**：预定义的提示词模板，参数化后供 LLM 使用。

在服务封装场景中，我们主要关注 **Tools**。

## Tool 定义格式

每个 Tool 包含：
- `name`：工具名称（snake_case）
- `description`：功能描述（LLM 根据此决定是否调用）
- `inputSchema`：参数的 JSON Schema（LLM 根据此生成参数）

## 调用流程

1. 客户端连接到 MCP Server（SSE 或 stdio）
2. 客户端发送 `tools/list` 获取可用工具列表
3. LLM 决定调用某个 Tool，客户端发送 `tools/call` + 参数
4. Server 执行 Tool 并返回结果
5. 结果回传给 LLM 继续推理

## SSE 传输细节

SSE 模式下，MCP Server 是一个 HTTP 服务：
- `GET /sse`：建立 SSE 连接，Server 通过此通道推送消息
- `POST /messages/`：客户端通过此端点发送请求

连接建立后，所有通信通过 JSON-RPC 2.0 格式。
