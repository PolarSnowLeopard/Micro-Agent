# 服务封装常见问题与解决方案

## 1. 依赖冲突

**问题**：原项目的 requirements.txt 中的包版本与 MCP SDK 依赖冲突。

**解决**：
- mcp 依赖 httpx、starlette，避免在 requirements.txt 中指定这些包的不兼容版本
- 如果原项目使用 Flask，封装后不再需要 Flask，从依赖中移除
- 使用 `pip install --no-cache-dir` 确保干净安装

## 2. 同步函数转异步

**问题**：原项目的核心函数是同步的，但 MCP Tool 推荐使用 async。

**解决**：
- 如果函数是 CPU 密集型（如 numpy 计算），同步函数也可以直接使用，FastMCP 会处理
- 如果函数涉及 I/O（文件读写、网络请求），应改为 async 或用 `asyncio.to_thread()` 包装
- 示例：`result = await asyncio.to_thread(sync_heavy_function, arg1, arg2)`

## 3. 文件路径问题

**问题**：原代码使用相对路径读取文件，Docker 环境中路径不同。

**解决**：
- 在 Dockerfile 中设置 `WORKDIR /app`，确保工作目录一致
- 使用 `os.path.dirname(__file__)` 获取脚本所在目录
- 数据文件通过 Docker volume 挂载或环境变量指定路径

## 4. 编码问题

**问题**：中文内容在 JSON 序列化时变成 unicode 转义。

**解决**：使用 `json.dumps(result, ensure_ascii=False)` 保持中文原文。

## 5. 大文件返回

**问题**：Tool 返回值超大（如图片 base64）导致 LLM 上下文溢出。

**解决**：
- Tool 返回结果应简洁，大文件保存到磁盘后返回文件路径
- 或返回摘要信息而非完整数据

## 6. 端口冲突

**问题**：多个 MCP 服务在同一台机器部署时端口冲突。

**解决**：
- docker-compose.yml 中映射不同的宿主机端口
- server.py 中通过环境变量配置端口：`port = int(os.getenv("PORT", "8000"))`
