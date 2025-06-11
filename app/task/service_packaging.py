import os
from app.config import WORKSPACE_ROOT

WORKSPACE = os.path.join(os.getcwd(),WORKSPACE_ROOT)

def get_service_packaging_prompt(workspace: str = WORKSPACE,
                               input_dir: str = "app-demo-input",
                               main_code: str = "main.py",
                               temp_dir: str = 'temp'):
    return f"""
    任务：将Python代码封装为MCP（Model Context Protocol）服务并完成容器化部署准备

    项目结构：
    - 工作目录: {workspace}
    - 输入目录: {input_dir}/
        ├── {main_code} (主程序文件)
        └── requirements.txt (依赖文件，可选)

    具体要求：
    1. MCP服务封装要求：
       - 分析{main_code}中的功能函数，识别适合作为MCP工具的函数
       - 使用FastMCP + Starlette创建server.py文件，要求：
         * 导入必要模块：
           - from mcp.server.fastmcp import FastMCP
           - from starlette.applications import Starlette
           - from starlette.requests import Request
           - from starlette.routing import Mount, Route
           - from mcp.server.sse import SseServerTransport
           - from mcp.server import Server
           - import uvicorn
         * 使用FastMCP创建服务器实例：mcp = FastMCP("服务名称")
         * 为每个功能函数使用@mcp.tool装饰器创建工具：
           - 提供clear的工具名称和描述
           - 添加完整的类型注解和参数说明
           - 添加详细的docstring说明参数和返回值
         * 实现create_starlette_app函数：
           - 创建SseServerTransport实例 (/messages/)
           - 配置SSE连接处理函数
           - 设置路由：/sse端点和/messages/挂载点
         * 添加命令行参数支持：
           - --host (默认0.0.0.0)
           - --port (默认8000)
         * 使用uvicorn启动服务器

    2. 依赖配置要求：
       - 更新requirements.txt，确保包含：
         * mcp (Python MCP SDK)
         * starlette
         * uvicorn[standard]
         * 其他原有依赖

    3. 容器化准备：
       - 创建生产级Dockerfile，要求：
         * 使用python:3.10-slim基础镜像
         * 正确安装相关依赖
         * 暴露8000端口
         * 配置合适的启动命令

       - 创建docker-compose.yml，要求：
         * 配置MCP服务容器
         * 端口映射(8000:8000)
         * 环境变量支持
         * 重启策略配置

    4. 文档要求：
       - 如果已有README.md，则更新README.md；否则生成README.md，包含：
         * MCP服务功能描述和工具列表
         * 本地开发运行指南：
           - python server.py
           - python server.py --host localhost --port 8001
         * SSE端点访问说明
         * 容器化部署指南
         * MCP客户端连接配置示例
         * 环境变量和配置说明

    5. 代码结构要求：
       - 保持原有功能函数的实现逻辑不变
       - 必要时将原功能函数作为内部实现函数（如_xxx_impl）
       - MCP工具函数作为包装器调用内部实现
       - 添加适当的错误处理和输入验证
       - 提供清晰的日志输出和启动信息

    输出：
    请在{input_dir}目录下生成以下文件：
    - app.py (MCP服务器主程序)
    - requirements.txt (更新后的依赖文件)
    - Dockerfile
    - docker-compose.yml
    - README.md

    技术栈参考示例：
    ```python
    from mcp.server.fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from mcp.server.sse import SseServerTransport
    import uvicorn

    mcp = FastMCP("服务名称")

    @mcp.tool("tool_name", description="工具描述")
    async def tool_function(param1: type, param2: type = default) -> str:
        \"\"\"详细的参数和返回值说明\"\"\"
        return implementation_function(param1, param2)

    # SSE传输配置和Starlette应用创建
    # 命令行参数处理和uvicorn启动
    ```

    注意：
    - 严格按照提供的技术栈和代码结构进行实现
    - 确保SSE端点正确配置为/sse，消息处理挂载为/messages/
    - 保持代码风格一致(PEP 8)和完整的类型注解
    - 确保所有MCP工具都有清晰的描述和参数说明
    - 如果原requirements.txt不存在，请基于{main_code}的导入语句推断依赖并添加MCP相关依赖
    """
