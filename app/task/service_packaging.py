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
         * service的构建的**镜像名称**命名为{input_dir.split('/')[-1]}-mcp-service:latest
         * service的构建的**服务名称**命名为{input_dir.split('/')[-1]}-mcp-service
         例如
         ```docker-compose.yml
            services:
                aml-mcp-service:
                    build:
                    context: .
                    dockerfile: Dockerfile
                    image: aml-mcp-service:latest
                    container_name: aml-mcp-service
         ```
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

    注意：
    - 严格按照提供的技术栈和代码结构进行实现
    - 确保SSE端点正确配置为/sse，消息处理挂载为/messages/
    - 保持代码风格一致(PEP 8)和完整的类型注解
    - 确保所有MCP工具都有清晰的描述和参数说明
    - 如果原requirements.txt不存在，请基于{main_code}的导入语句推断依赖并添加MCP相关依赖
    - Dockerfile必须配置国内镜像源（阿里云APT源 + 清华pip源）以加速构建

    ============================================================================
    示例1：简单MCP服务 - 利奈唑胺给药计算
    ============================================================================
    
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

    MCP Server代码示例：
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
        
        print("启动MCP服务器...")
        
        # 运行服务器
        uvicorn.run(starlette_app, host=args.host, port=args.port)
    ```

    ============================================================================
    示例2：复杂MCP服务 - 反洗钱风险预测（包含多工具、状态管理、健康检查）
    ============================================================================
    
    Dockerfile示例（包含环境变量和健康检查）：
    ```dockerfile
    # 反洗钱预测MCP服务器 - Docker镜像
    FROM python:3.11-slim

    # 设置工作目录
    WORKDIR /app

    # 设置环境变量
    ENV PYTHONUNBUFFERED=1 \\
        PYTHONDONTWRITEBYTECODE=1 \\
        PIP_NO_CACHE_DIR=1 \\
        PIP_DISABLE_PIP_VERSION_CHECK=1 \\
        PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

    # 安装系统依赖（如有需要）
    # RUN apt-get update && apt-get install -y --no-install-recommends \\
    #     gcc \\
    #     g++ \\
    #     && rm -rf /var/lib/apt/lists/*

    # 复制依赖文件并安装
    COPY requirements.txt .
    RUN pip install --no-cache-dir -r requirements.txt

    # 复制应用代码
    COPY predictor.py .
    COPY example_data.py .
    COPY mcp_server.py .
    COPY __init__.py .

    # 创建模型目录
    RUN mkdir -p models

    # 暴露端口
    EXPOSE 8000

    # 健康检查
    HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \\
        CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()"

    # 设置默认环境变量
    ENV HOST=0.0.0.0 \\
        PORT=8000 \\
        MODEL_PATH=models/aml_model_random_forest.pkl

    # 启动命令
    CMD ["python", "mcp_server.py", "--host", "0.0.0.0", "--port", "8000"]
    ```

    MCP Server代码示例（包含多工具、全局状态、健康检查端点）：
    ```python
    \"\"\"
    反洗钱预测MCP服务器
    功能：单客户预测、批量预测、交易风险评分、模型信息查询
    \"\"\"
    import os
    from typing import Optional
    from mcp.server.fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from mcp.server.sse import SseServerTransport
    from mcp.server import Server
    import uvicorn

    from predictor import AMLPredictor
    from example_data import EXAMPLE_COUNTRY_RISK_MAPPING

    # 全局预测器实例
    predictor: Optional[AMLPredictor] = None

    # 创建MCP服务器
    mcp = FastMCP("反洗钱风险预测服务器")

    def get_predictor() -> AMLPredictor:
        \"\"\"获取预测器实例\"\"\"
        global predictor
        if predictor is None:
            raise RuntimeError("预测器未初始化")
        return predictor

    @mcp.tool(description="预测单个客户的反洗钱风险等级")
    async def predict_customer_risk(
        customer_id: str,
        name: str,
        age: int,
        nationality: str,
        occupation: str,
        account_opening_date: str,
        pep_status: int,
        sanctions_match: int,
        address_change_count: int,
        transactions: list[dict],
        country_risk_mapping: Optional[dict] = None
    ) -> dict:
        \"\"\"预测单个客户风险\"\"\"
        pred = get_predictor()
        customer_data = {{
            'customer_id': customer_id, 'name': name, 'age': age,
            'nationality': nationality, 'occupation': occupation,
            'account_opening_date': account_opening_date,
            'pep_status': pep_status, 'sanctions_match': sanctions_match,
            'address_change_count': address_change_count
        }}
        if country_risk_mapping is None:
            country_risk_mapping = EXAMPLE_COUNTRY_RISK_MAPPING
        return pred.predict_customer_risk(customer_data, transactions, country_risk_mapping)

    @mcp.tool(description="批量预测多个客户的反洗钱风险")
    async def predict_batch_customers(
        customers: list[dict],
        transactions: list[dict],
        country_risk_mapping: Optional[dict] = None
    ) -> dict:
        \"\"\"批量预测客户风险\"\"\"
        pred = get_predictor()
        if country_risk_mapping is None:
            country_risk_mapping = EXAMPLE_COUNTRY_RISK_MAPPING
        
        results = list(pred.predict_batch_customers(customers, transactions, country_risk_mapping))
        suspicious_count = sum(1 for r in results if r.get('is_suspicious') == 1)
        
        return {{
            'total_customers': len(customers),
            'results': results,
            'summary': {{
                'suspicious_count': suspicious_count,
                'suspicious_rate': f"{{(suspicious_count / len(customers) * 100):.2f}}%" if customers else "0%"
            }}
        }}

    @mcp.tool(description="计算单笔交易的风险评分")
    async def calculate_transaction_risk(
        transaction_id: str,
        amount: float,
        is_cash_transaction: int,
        is_cross_border: int,
        country_risk_score: int = 1
    ) -> dict:
        \"\"\"计算交易风险评分\"\"\"
        pred = get_predictor()
        transaction = {{
            'transaction_id': transaction_id, 'amount': amount,
            'is_cash_transaction': is_cash_transaction,
            'is_cross_border': is_cross_border,
            'country_risk_score': country_risk_score
        }}
        score = pred.calculate_transaction_risk_score(transaction)
        risk_level = 'high' if score >= 10 else ('medium' if score >= 5 else 'low')
        
        risk_factors = []
        if amount > 10000:
            risk_factors.append(f"大额交易 (${{amount:,.2f}})")
        if is_cash_transaction:
            risk_factors.append("现金交易")
        if is_cross_border:
            risk_factors.append("跨境交易")
        
        return {{
            'transaction_id': transaction_id,
            'risk_score': float(score),
            'risk_level': risk_level,
            'risk_factors': risk_factors if risk_factors else ["无明显风险因素"]
        }}

    @mcp.tool(description="获取当前加载的模型信息")
    async def get_model_info() -> dict:
        \"\"\"获取模型信息\"\"\"
        pred = get_predictor()
        return pred.get_model_info()

    def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
        \"\"\"创建支持SSE传输和健康检查的Starlette应用\"\"\"
        sse = SseServerTransport("/messages/")
        
        async def handle_sse(request: Request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as (read_stream, write_stream):
                await mcp_server.run(
                    read_stream, write_stream,
                    mcp_server.create_initialization_options()
                )
        
        async def health_check(request: Request):
            \"\"\"健康检查端点\"\"\"
            try:
                pred = get_predictor()
                model_info = pred.get_model_info()
                return JSONResponse({{
                    "status": "healthy",
                    "service": "aml-predictor-mcp-server",
                    "model_loaded": model_info.get('loaded', False)
                }})
            except Exception as e:
                return JSONResponse({{"status": "unhealthy", "error": str(e)}}, status_code=503)
        
        async def root_info(request: Request):
            \"\"\"根路径信息\"\"\"
            return JSONResponse({{
                "service": "反洗钱风险预测MCP服务器",
                "version": "1.0.0",
                "endpoints": {{
                    "health": "/health",
                    "sse": "/sse",
                    "messages": "/messages/"
                }}
            }})
        
        return Starlette(
            debug=debug,
            routes=[
                Route("/", endpoint=root_info, methods=["GET"]),
                Route("/health", endpoint=health_check, methods=["GET"]),
                Route("/sse", endpoint=handle_sse, methods=["GET"]),
                Mount("/messages", app=sse.handle_post_message),
            ],
        )

    def initialize_predictor():
        \"\"\"初始化预测器\"\"\"
        global predictor
        model_path = os.getenv("MODEL_PATH", "models/aml_model_random_forest.pkl")
        
        if os.path.exists(model_path):
            predictor = AMLPredictor(model_path)
            print(f"[成功] 模型加载完成: {{model_path}}")
        else:
            print(f"[警告] 模型文件不存在: {{model_path}}")

    if __name__ == "__main__":
        import argparse
        
        parser = argparse.ArgumentParser(description='运行反洗钱预测MCP服务器')
        parser.add_argument('--host', default='0.0.0.0', help='绑定的主机地址')
        parser.add_argument('--port', type=int, default=8000, help='监听的端口')
        parser.add_argument('--model', default='models/aml_model_random_forest.pkl', help='模型文件路径')
        args = parser.parse_args()
        
        if args.model:
            os.environ["MODEL_PATH"] = args.model
        
        # 初始化预测器
        initialize_predictor()
        
        # 创建并运行服务
        mcp_server = mcp._mcp_server
        starlette_app = create_starlette_app(mcp_server, debug=True)
        
        print(f"服务已启动: http://localhost:{{args.port}}")
        print(f"  - GET  /health   健康检查")
        print(f"  - SSE  /sse      MCP连接")
        
        uvicorn.run(starlette_app, host=args.host, port=args.port)
    ```

    ============================================================================
    示例3：复杂MCP服务 - 图神经网络风险检测（包含文件上传、深度学习、多参数）
    ============================================================================
    
    Dockerfile示例（包含深度学习框架和特殊依赖处理）：
    ```dockerfile
    # 图神经网络风险检测MCP服务器 - Docker镜像
    FROM python:3.10-slim

    # 安装系统依赖
    RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources && \\
        sed -i 's|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources && \\
        apt-get update && apt-get install -y --no-install-recommends \\
        libcurl4 \\
        build-essential \\
        cmake \\
        git \\
        && apt-get clean \\
        && rm -rf /var/lib/apt/lists/*

    # 设置工作目录
    WORKDIR /app

    # 设置环境变量
    ENV PYTHONUNBUFFERED=1 \\
        PYTHONDONTWRITEBYTECODE=1 \\
        PIP_DISABLE_PIP_VERSION_CHECK=1 \\
        DGL_GRAPHBOLT_USE_CPP=0

    # 复制依赖文件
    COPY requirements.txt .

    # 设置 pip 使用清华镜像源
    RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

    # 先删除 requirements.txt 中的 dgl（如果有的话）
    RUN sed -i '/^dgl/d' requirements.txt

    # 安装其他 Python 依赖（使用清华镜像）
    RUN pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

    # 单独安装 DGL（使用清华镜像）
    RUN pip install dgl==2.1.0 -i https://pypi.tuna.tsinghua.edu.cn/simple

    # 复制项目文件
    COPY main.py .
    COPY server.py .
    COPY app.py .
    COPY methods/ ./methods/
    COPY data/ ./data/

    # 创建checkpoint目录（用于存放模型文件）
    RUN mkdir -p checkpoint

    # 复制模型文件（如果存在）
    # 如果模型文件较大，建议在运行时通过volume挂载
    # 注意：如果checkpoint目录不存在或为空，请注释掉下面这行
    COPY checkpoint/ ./checkpoint/

    # 暴露端口
    EXPOSE 8000

    # 健康检查
    HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \\
        CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()" || exit 1

    # 设置默认环境变量
    ENV HOST=0.0.0.0 \\
        PORT=8000 \\
        MODEL_PATH=checkpoint/model.pt \\
        IN_FEATS=211 \\
        H_FEATS=211 \\
        OUT_FEATS=3

    # 启动命令
    CMD ["sh", "-c", "python server.py --host ${{{{HOST}}}} --port ${{{{PORT}}}} --model ${{{{MODEL_PATH}}}} --in-feats ${{{{IN_FEATS}}}} --h-feats ${{{{H_FEATS}}}} --out-feats ${{{{OUT_FEATS}}}}"]
    ```

    MCP Server代码示例（包含文件上传、深度学习、多工具）：
    ```python
    \"\"\"
    图神经网络风险检测MCP服务器
    功能：数据集推理、模型信息查询、健康检查
    \"\"\"
    import os
    import io
    import base64
    import tempfile
    import asyncio
    from typing import Optional, Dict, Any
    from mcp.server.fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from mcp.server.sse import SseServerTransport
    from mcp.server import Server
    import uvicorn
    import torch
    import logging

    from main import InferenceModel

    # 配置日志
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    # 全局推理模型实例
    inference_model: Optional[InferenceModel] = None

    # 创建MCP服务器
    mcp = FastMCP("图神经网络风险检测服务器")


    def get_inference_model() -> InferenceModel:
        \"\"\"获取推理模型实例\"\"\"
        global inference_model
        if inference_model is None:
            raise RuntimeError("推理模型未初始化")
        if inference_model.model is None:
            raise RuntimeError("模型文件未正确加载，请检查模型文件路径是否正确")
        return inference_model


    @mcp.tool(description="上传数据集ZIP文件并进行图神经网络推理预测")
    async def predict_from_dataset(
        file_base64: str,
        filename: str = "dataset.zip"
    ) -> Dict[str, Any]:
        \"\"\"
        上传数据集并进行推理
        
        Args:
            file_base64: Base64编码的ZIP文件内容
            filename: 文件名（默认dataset.zip）
            
        Returns:
            dict: 包含推理结果的字典
            
        示例：
            file_base64: "UEsDBBQAAAAIAC..."
            filename: "test_dataset.zip"
        \"\"\"
        def _process():
            \"\"\"同步处理函数，在线程中执行\"\"\"
            try:
                logger.info(f"开始处理数据集推理: {{{{filename}}}}")
                
                # 验证文件类型
                if not filename.endswith('.zip'):
                    return {{{{
                        'success': False,
                        'error': '文件必须是ZIP格式'
                    }}}}
                
                # 解码Base64文件内容
                try:
                    file_bytes = base64.b64decode(file_base64)
                except Exception as e:
                    return {{{{
                        'success': False,
                        'error': f'Base64解码失败: {{{{str(e)}}}}'
                    }}}}
                
                # 创建临时文件对象
                file_obj = io.BytesIO(file_bytes)
                
                # 获取推理模型
                model = get_inference_model()
                
                # 处理数据集
                dataset_path = model.process_uploaded_dataset(file_obj)
                logger.info(f"数据集处理完成: {{{{dataset_path}}}}")
                
                # 进行推理
                result = model.infer(dataset_path)
                logger.info("推理完成")
                
                return {{{{
                    'success': True,
                    'result': result,
                    'filename': filename
                }}}}
                
            except Exception as e:
                logger.exception("推理过程出错")
                return {{{{
                    'success': False,
                    'error': f"推理过程出错: {{{{str(e)}}}}"
                }}}}
        
        # 在线程池中执行阻塞操作
        return await asyncio.to_thread(_process)


    @mcp.tool(description="从指定路径加载数据集并进行推理")
    async def predict_from_path(
        dataset_path: str
    ) -> Dict[str, Any]:
        \"\"\"
        从指定路径加载数据集并进行推理
        
        Args:
            dataset_path: 数据集目录路径（包含meta.yaml等文件）
            
        Returns:
            dict: 包含推理结果的字典
        \"\"\"
        def _process():
            \"\"\"同步处理函数，在线程中执行\"\"\"
            try:
                logger.info(f"从路径加载数据集: {{{{dataset_path}}}}")
                
                # 验证路径存在
                if not os.path.exists(dataset_path):
                    return {{{{
                        'success': False,
                        'error': f'数据集路径不存在: {{{{dataset_path}}}}'
                    }}}}
                
                # 获取推理模型
                model = get_inference_model()
                
                # 进行推理
                result = model.infer(dataset_path)
                logger.info("推理完成")
                
                return {{{{
                    'success': True,
                    'result': result,
                    'dataset_path': dataset_path
                }}}}
                
            except Exception as e:
                logger.exception("推理过程出错")
                return {{{{
                    'success': False,
                    'error': f"推理过程出错: {{{{str(e)}}}}"
                }}}}
        
        # 在线程池中执行阻塞操作
        return await asyncio.to_thread(_process)


    @mcp.tool(description="获取当前加载的模型信息")
    async def get_model_info() -> Dict[str, Any]:
        \"\"\"
        获取模型信息
        
        Returns:
            dict: 包含模型配置和状态的字典
        \"\"\"
        try:
            model = get_inference_model()
            
            return {{{{
                'success': True,
                'model_path': model.model_path,
                'device': str(model.device),
                'model_loaded': model.model is not None,
                'model_type': 'GNN Risk Detection Model'
            }}}}
            
        except Exception as e:
            return {{{{
                'success': False,
                'error': f"获取模型信息失败: {{{{str(e)}}}}"
            }}}}


    @mcp.tool(description="检查服务健康状态")
    async def health_check() -> Dict[str, Any]:
        \"\"\"
        健康检查
        
        Returns:
            dict: 服务健康状态信息
        \"\"\"
        try:
            model = get_inference_model()
            
            return {{{{
                'status': 'healthy',
                'service': 'gnn-risk-detection-mcp-server',
                'model_loaded': model.model is not None,
                'device': str(model.device),
                'cuda_available': torch.cuda.is_available()
            }}}}
            
        except Exception as e:
            return {{{{
                'status': 'unhealthy',
                'error': str(e)
            }}}}


    @mcp.tool(description="列出所有可用的数据集")
    async def list_datasets(
        data_dir: str = "/app/data"
    ) -> Dict[str, Any]:
        \"\"\"
        列出指定目录下所有可用的数据集
        
        Args:
            data_dir: 数据集根目录（默认: /app/data）
            
        Returns:
            dict: 包含数据集列表的字典
        \"\"\"
        def _process():
            \"\"\"同步处理函数，在线程中执行\"\"\"
            try:
                logger.info(f"列出数据集目录: {{{{data_dir}}}}")
                
                # 验证目录存在
                if not os.path.exists(data_dir):
                    return {{{{
                        'success': False,
                        'error': f'数据集目录不存在: {{{{data_dir}}}}'
                    }}}}
                
                if not os.path.isdir(data_dir):
                    return {{{{
                        'success': False,
                        'error': f'路径不是目录: {{{{data_dir}}}}'
                    }}}}
                
                # 获取所有子文件夹
                datasets = []
                for item in os.listdir(data_dir):
                    item_path = os.path.join(data_dir, item)
                    if os.path.isdir(item_path):
                        # 检查是否包含 meta.yaml 文件
                        has_meta = os.path.exists(os.path.join(item_path, 'meta.yaml'))
                        datasets.append({{{{
                            'name': item,
                            'path': item_path,
                            'has_meta': has_meta
                        }}}})
                
                # 按名称排序
                datasets.sort(key=lambda x: x['name'])
                
                return {{{{
                    'success': True,
                    'data_dir': data_dir,
                    'total': len(datasets),
                    'datasets': datasets
                }}}}
                
            except Exception as e:
                logger.exception("列出数据集时出错")
                return {{{{
                    'success': False,
                    'error': f"列出数据集时出错: {{{{str(e)}}}}"
                }}}}
        
        # 在线程池中执行阻塞操作
        return await asyncio.to_thread(_process)


    def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
        \"\"\"创建支持SSE传输和健康检查的Starlette应用\"\"\"
        sse = SseServerTransport("/messages/")
        
        async def handle_sse(request: Request):
            \"\"\"处理SSE连接\"\"\"
            async with sse.connect_sse(
                request.scope,
                request.receive,
                request._send  # noqa: SLF001
            ) as (read_stream, write_stream):
                await mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp_server.create_initialization_options()
                )
            # 返回空响应（连接已由SSE传输处理）
            from starlette.responses import Response
            return Response()
        
        async def health_endpoint(request: Request):
            \"\"\"健康检查端点\"\"\"
            try:
                model = get_inference_model()
                return JSONResponse({{{{
                    "status": "healthy",
                    "service": "gnn-risk-detection-mcp-server",
                    "model_loaded": model.model is not None,
                    "device": str(model.device)
                }}}})
            except Exception as e:
                return JSONResponse(
                    {{{{"status": "unhealthy", "error": str(e)}}}},
                    status_code=503
                )
        
        async def root_info(request: Request):
            \"\"\"根路径信息\"\"\"
            return JSONResponse({{{{
                "service": "图神经网络风险检测MCP服务器",
                "version": "1.0.0",
                "description": "基于GNN的风险检测推理服务",
                "endpoints": {{{{
                    "root": "/",
                    "health": "/health",
                    "sse": "/sse",
                    "messages": "/messages/"
                }}}},
                "tools": [
                    {{{{
                        "name": "predict_from_dataset",
                        "description": "上传数据集ZIP文件并进行推理"
                    }}}},
                    {{{{
                        "name": "predict_from_path",
                        "description": "从指定路径加载数据集并进行推理"
                    }}}},
                    {{{{
                        "name": "list_datasets",
                        "description": "列出所有可用的数据集"
                    }}}},
                    {{{{
                        "name": "get_model_info",
                        "description": "获取模型信息"
                    }}}},
                    {{{{
                        "name": "health_check",
                        "description": "检查服务健康状态"
                    }}}}
                ]
            }}}})
        
        return Starlette(
            debug=debug,
            routes=[
                Route("/", endpoint=root_info, methods=["GET"]),
                Route("/health", endpoint=health_endpoint, methods=["GET"]),
                Route("/sse", endpoint=handle_sse, methods=["GET"]),
                Mount("/messages", app=sse.handle_post_message),
            ],
        )


    def initialize_model(
        model_path: str = 'checkpoint/model.pt',
        device: Optional[str] = None,
        in_feats: int = 211,
        h_feats: int = 211,
        out_feats: int = 3
    ) -> None:
        \"\"\"初始化推理模型\"\"\"
        global inference_model
        
        # 自动检测设备
        if device is None:
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
        
        logger.info(f"初始化模型: {{{{model_path}}}}")
        logger.info(f"使用设备: {{{{device}}}}")
        
        # 验证模型文件是否存在
        if not os.path.exists(model_path):
            logger.error(f"模型文件不存在: {{{{model_path}}}}")
            raise FileNotFoundError(f"模型文件不存在: {{{{model_path}}}}")
        
        # 创建推理模型实例
        inference_model = InferenceModel(model_path=model_path, device=device)
        
        # 加载模型
        inference_model.load_model(
            in_feats=in_feats,
            h_feats=h_feats,
            out_feats=out_feats
        )
        
        logger.info("✓ 模型加载成功")


    if __name__ == "__main__":
        import argparse
        
        parser = argparse.ArgumentParser(
            description='运行图神经网络风险检测MCP服务器'
        )
        parser.add_argument(
            '--host',
            default='0.0.0.0',
            help='绑定的主机地址 (默认: 0.0.0.0)'
        )
        parser.add_argument(
            '--port',
            type=int,
            default=8000,
            help='监听的端口 (默认: 8000)'
        )
        parser.add_argument(
            '--model',
            default='checkpoint/model.pt',
            help='模型文件路径 (默认: checkpoint/model.pt)'
        )
        parser.add_argument(
            '--device',
            choices=['cpu', 'cuda'],
            default=None,
            help='计算设备 (默认: 自动检测)'
        )
        parser.add_argument(
            '--in-feats',
            type=int,
            default=211,
            help='输入特征维度 (默认: 211)'
        )
        parser.add_argument(
            '--h-feats',
            type=int,
            default=211,
            help='隐藏层维度 (默认: 211)'
        )
        parser.add_argument(
            '--out-feats',
            type=int,
            default=3,
            help='输出类别数 (默认: 3)'
        )
        
        args = parser.parse_args()
        
        # 初始化推理模型
        initialize_model(
            model_path=args.model,
            device=args.device,
            in_feats=args.in_feats,
            h_feats=args.h_feats,
            out_feats=args.out_feats
        )
        
        # 获取MCP服务器实例
        mcp_server = mcp._mcp_server
        
        # 创建Starlette应用
        starlette_app = create_starlette_app(mcp_server, debug=True)
        
        # 打印启动信息
        print("=" * 60)
        print("图神经网络风险检测MCP服务器")
        print("=" * 60)
        print(f"服务地址: http://{{{{args.host}}}}:{{{{args.port}}}}")
        print(f"模型路径: {{{{args.model}}}}")
        print(f"计算设备: {{{{args.device or '自动检测'}}}}")
        print()
        print("可用端点:")
        print(f"  - GET  /          服务信息")
        print(f"  - GET  /health    健康检查")
        print(f"  - SSE  /sse       MCP连接")
        print(f"  - POST /messages/ MCP消息")
        print()
        print("MCP工具:")
        print("  - predict_from_dataset  上传ZIP数据集并推理")
        print("  - predict_from_path     从路径加载数据集并推理")
        print("  - list_datasets         列出所有可用数据集")
        print("  - get_model_info        获取模型信息")
        print("  - health_check          健康检查")
        print("=" * 60)
        
        # 运行服务器
        uvicorn.run(starlette_app, host=args.host, port=args.port)
    ```
    """
