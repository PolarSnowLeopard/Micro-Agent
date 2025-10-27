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
         * 配置国内镜像源以加速构建：
           - Debian/Ubuntu APT源：使用阿里云镜像源
           - Python pip源：使用清华大学镜像源
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

    Dockerfile参考示例（使用国内镜像源加速构建）。尽量避免安装无用的系统依赖：
    ```dockerfile
    FROM python:3.10-slim

    # 配置阿里云APT镜像源（自动适配Debian版本）
    # 生成时默认注释掉，需要时再取消注释
    # RUN . /etc/os-release && \\
    #    echo "deb https://mirrors.aliyun.com/debian/ $VERSION_CODENAME main contrib non-free" > /etc/apt/sources.list && \\
    #    echo "deb https://mirrors.aliyun.com/debian/ $VERSION_CODENAME-updates main contrib non-free" >> /etc/apt/sources.list && \\
    #    echo "deb https://mirrors.aliyun.com/debian-security $VERSION_CODENAME-security main contrib non-free" >> /etc/apt/sources.list

    # 安装系统依赖
    # 生成时默认注释掉，需要时再取消注释
    # RUN apt-get update && apt-get install -y --no-install-recommends \\
    #     build-essential \\
    #     && apt-get clean \\
    #     && rm -rf /var/lib/apt/lists/*

    # 设置工作目录
    WORKDIR /app

    # 复制依赖文件
    COPY requirements.txt .

    # 安装Python依赖（使用清华源）
    RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

    # 复制项目文件
    COPY . .

    # 暴露端口
    EXPOSE 8000

    # 启动命令
    CMD ["python", "server.py"]
    ```

    技术栈参考示例：
    ```python
    from mcp.server.fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.routing import Mount, Route
    from mcp.server.sse import SseServerTransport
    from mcp.server import Server
    import uvicorn
    from datetime import datetime
    from typing import Tuple
    from api.calculator import _calculate_linezolid_dose_impl

    # 创建 MCP 服务器
    mcp = FastMCP("利奈唑胺给药计算服务器")

    # 添加获取服务器时间的工具
    @mcp.tool("calculate_linezolid_dose", description="计算利奈唑胺的推荐剂量")
    async def calculate_linezolid_dose(
        sex: int, 
        age: int, 
        height: int, 
        weight: int, 
        scr: float, 
        tb: float, 
        auc_range: list[float] = [160,240]
    ) -> str:
        \"\"\"
        计算利奈唑胺的推荐剂量 - 内部实现函数
        
        Args:
            sex: 性别(1=男性, 0=女性)
            age: 年龄(岁)
            height: 身高(厘米)
            weight: 体重(千克)
            scr: 血清肌酐(μmol/L)
            tb: 总胆红素(μmol/L)
            auc_range: 目标AUC24h范围(min, max), 默认[160,240]
            
        Returns:
            dict: 包含计算结果的字典
        \"\"\"
        return _calculate_linezolid_dose_impl(sex, age, height, weight, scr, tb, auc_range)

    # 创建支持SSE的Starlette应用
    def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
        \"\"\"创建一个支持SSE传输的Starlette应用\"\"\"
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
        parser.add_argument('--port', type=int, default=8000, help='监听的端口')
        args = parser.parse_args()
        
        # 获取MCP服务器
        mcp_server = mcp._mcp_server
        
        # 创建Starlette应用
        starlette_app = create_starlette_app(mcp_server, debug=True)
        
        print("启动MCP服务器..."))
        
        # 运行服务器
        uvicorn.run(starlette_app, host=args.host, port=args.port)
    ```

    注意：
    - 严格按照提供的技术栈和代码结构进行实现
    - 确保SSE端点正确配置为/sse，消息处理挂载为/messages/
    - 保持代码风格一致(PEP 8)和完整的类型注解
    - 确保所有MCP工具都有清晰的描述和参数说明
    - 如果原requirements.txt不存在，请基于{main_code}的导入语句推断依赖并添加MCP相关依赖
    - Dockerfile必须配置国内镜像源（阿里云APT源 + 清华pip源）以加速构建
    """
