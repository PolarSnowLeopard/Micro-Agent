import asyncio
import json
import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
import shutil
from datetime import datetime

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dotenv import load_dotenv

load_dotenv()

from app.config import WORKSPACE_ROOT

from app.task.demo import demo_task_configs
# 导入任务提示
from app.task.code_analysis import (
    get_code_analysis_prompt
)
from app.task.service_evaluation import (
    get_service_evaluation_prompt
)
from app.task.meta_app_validation import (
    get_meta_app_validation_prompt
)
from app.task.aml_model_evaluation import (
    get_aml_model_evaluation_prompt
)
from app.task.aml_report import (
    get_aml_report_prompt
)
from app.task.service_packaging import (
    get_service_packaging_prompt
)
from app.task.mcp_service_recommendation import (
    get_mcp_service_recommendation_prompt
)
from app.utils.file_utils import extract_zip

# 设置日志记录器
logger = logging.getLogger(__name__)

# 创建FastAPI应用
app = FastAPI(
    title="Agent流式执行服务",
    description="Agent流式执行API",
    version="1.0",
    docs_url="/",
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 配置静态文件
static_dir = Path("static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")

# 辅助函数：创建流式响应生成器
async def create_stream_generator(task_name: str, task_config: Dict[str, Any], agent_name: str, 
                                  cleanup_files: List[str] = None, zip_extract_path: str = None):
    """
    创建通用的流式响应生成器
    
    参数:
        task_name: 任务名称
        task_config: 任务配置
        agent_name: Agent名称
        cleanup_files: 任务完成后需要清理的文件列表
        zip_extract_path: 如果指定，将此目录压缩成zip文件并返回（用于service_packaging等任务）
        
    返回:
        异步生成器，产生SSE格式的事件流
    """
    from run_mcp import MCPRunner
    
    runner = None
    full_result = []
    try:
        runner = MCPRunner(agent_name)
        
        # 从任务配置中获取服务器配置列表
        server_configs = task_config.get("server_config", [])
        
        # 先添加内置的MCP服务器（这是默认的，始终存在）
        logger.info("添加默认内置MCP服务器")
        await runner.add_server(
            connection_type="stdio",
            server_url=None,
            command=None,  
            args=None,    
            server_id="stdio_built_in"  
        )
        
        # 遍历并添加配置中的其他服务器
        if server_configs:
            for idx, server_config in enumerate(server_configs):
                connection_type = server_config.get("connection_type", "stdio")
                server_url = server_config.get("server_url")
                command = server_config.get("command")
                args = server_config.get("args")
                server_id = server_config.get("server_id") or f"server_{idx}"
                
                # 检查是否有足够的配置信息来添加服务器
                if server_url or command:
                    logger.info(f"添加配置的MCP服务器 #{idx+1}")
                    await runner.add_server(
                        connection_type=connection_type,
                        server_url=server_url,
                        command=command,
                        args=args,
                        server_id=server_id
                    )
        
        # 获取prompt
        prompt = task_config["prompt"]
        
        # 运行流式Agent
        async for step_result in runner.run_stream(prompt):
            # 将结果转为SSE格式
            json_result = json.dumps(step_result, ensure_ascii=False)

            if not step_result.get("is_last", False):
                full_result.append(step_result)
                yield f"data: {json_result}\n\n"
            
            # 如果是最后一个结果，保存完整记录并返回特定输出
            else:        
                # 保存完整记录到文件
                from app.utils.visualize_record import save_record_to_json, generate_visualization_html
                full_json = json.dumps(full_result, ensure_ascii=False)
                save_record_to_json(task_name, full_json)
                generate_visualization_html(task_name)
                
                # 读取任务特定的最终输出文件
                final_results = {}
                
                # 如果指定了zip_extract_path，则压缩目录并返回
                if zip_extract_path and os.path.exists(zip_extract_path):
                    try:
                        # 生成唯一的zip文件名
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        zip_filename = f"{WORKSPACE_ROOT}/{timestamp}_service_package.zip"
                        
                        # 压缩目录
                        import zipfile
                        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                            for root, dirs, files in os.walk(zip_extract_path):
                                for file in files:
                                    file_path = os.path.join(root, file)
                                    arcname = os.path.relpath(file_path, zip_extract_path)
                                    zipf.write(file_path, arcname)
                        
                        logger.info(f"已创建压缩包: {zip_filename}")
                        
                        # 读取压缩文件并转换为base64
                        import base64
                        with open(zip_filename, 'rb') as f:
                            zip_content = base64.b64encode(f.read()).decode('utf-8')
                        
                        final_results["service_package"] = {
                            "filename": os.path.basename(zip_filename),
                            "content": zip_content,
                            "type": "zip"
                        }
                        
                        # 清理临时zip文件
                        if os.path.exists(zip_filename):
                            os.remove(zip_filename)
                            
                    except Exception as e:
                        logger.error(f"压缩目录失败: {str(e)}")
                        final_results["error"] = f"压缩目录失败: {str(e)}"
                
                # 按照任务配置读取输出文件（仅当未指定zip_extract_path时）
                if not zip_extract_path:
                    for output_config in task_config.get("outputs", []):
                        output_name = output_config["name"]
                        output_file = output_config["file"]
                        
                        try:
                            file_path = Path(output_file)
                            if file_path.exists():
                                with open(file_path, 'r', encoding='utf-8') as f:
                                    content = f.read()
                                    try:
                                        final_results[output_name] = json.loads(content)
                                    except json.JSONDecodeError:
                                        final_results[output_name] = content
                            else:
                                logger.warning(f"输出文件不存在: {output_file}")
                        except Exception as e:
                            logger.warning(f"无法读取输出文件 {output_file}: {str(e)}")
                
                # 调试日志，查看最终结果
                logger.info(f"最终结果文件状态: {final_results}")
                
                # 仅当有最终结果时才发送
                if final_results:
                    # 发送包含最终结果的最后一条消息
                    last_message = {
                        "is_last": True,
                        "is_final_result": True,
                        "final_results": final_results
                    }
                    yield f"data: {json.dumps(last_message, ensure_ascii=False)}\n\n"
                else:
                    # 如果没有找到最终结果，也发送消息通知前端
                    logger.warning(f"没有找到任务 {task_name} 的最终输出文件")
                    last_message = {
                        "is_last": True,
                        "warning": f"没有找到任务 {task_name} 的最终输出文件"
                    }
                    yield f"data: {json.dumps(last_message, ensure_ascii=False)}\n\n"
            
    except Exception as e:
        error_msg = f"执行出错: {str(e)}"
        logger.error(error_msg, exc_info=True)
        yield f"data: {json.dumps({'error': error_msg, 'is_last': True})}\n\n"
    finally:
        if runner:
            try:
                # 使用非阻塞方式清理资源
                logger.info("在后台启动清理过程...")
                # 创建任务但不等待其完成
                # asyncio.create_task(runner.cleanup())
                runner.cleanup()
                # 给清理任务一点时间启动
                # await asyncio.sleep(0.1)
                logger.info("清理任务已在后台启动")
            except Exception as e:
                logger.error(f"启动清理过程时出错: {str(e)}")
            
            # 清理临时文件
            try:
                # 清理通用临时目录
                if os.path.exists(f"{WORKSPACE_ROOT}/temp"):
                    shutil.rmtree(f"{WORKSPACE_ROOT}/temp")
                
                # 清理任务特定的文件
                if cleanup_files:
                    for file_path in cleanup_files:
                        if os.path.exists(file_path):
                            if os.path.isdir(file_path):
                                shutil.rmtree(file_path)
                            else:
                                os.remove(file_path)
            except Exception as e:
                logger.warning(f"清理临时文件失败: {str(e)}")

# 创建通用流式响应
def create_streaming_response(generator):
    """创建标准的流式SSE响应"""
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # 禁用Nginx缓冲
        }
    )

# 新增流式处理的路由
@app.get("/stream/run/{task_name}", tags=["stream"])
async def stream_run(task_name: str):
    """
    以流式方式运行Agent
    
    参数:
        task_name: 任务名称
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
        最后一个事件包含任务特定的最终结果
    """
    from run_mcp import MCPRunner
    
    # 任务配置映射：每个任务包含prompt和最终输出文件配置
    task_configs = demo_task_configs
    
    # 检查任务是否存在
    if task_name not in task_configs:
        raise HTTPException(status_code=400, detail=f"未知的任务名称: {task_name}")
        
    # 获取任务配置
    task_config = task_configs[task_name]
    agent_name = f'{task_name.replace("_", " ").capitalize()} Agent'
    
    # 使用通用生成器创建流式响应
    stream_generator = create_stream_generator(task_name, task_config, agent_name)
    return create_streaming_response(stream_generator)

# 添加演示页面路由
@app.get("/stream_demo", tags=["demo"])
async def stream_demo():
    """
    返回流式演示页面
    """
    return FileResponse("static/stream_demo.html")

# 添加文件上传演示页面路由
@app.get("/upload_demo", tags=["demo"])
async def upload_demo():
    """
    返回文件上传演示页面
    """
    return FileResponse("static/upload_demo.html")

# 添加mcp测试任务的POST API端点
@app.post("/api/agent/mcp_test", tags=["api"])
async def mcp_test_upload(message: str = Form(...), server_url: str = Form(...)):
    """
    上传URL并执行mcp测试任务
    
    参数:
        message: 需要测试的mcp服务URL
        server_url: 需要测试的mcp服务URL
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
        最后一个事件包含任务特定的最终结果
    """
    
    try:
        
        # 使用与code_analysis任务相同的配置
        task_name = "mcp_test"
        task_config = {
            "prompt": "你是一个用于对指定mcp server进行测试的Agent，主要职责在于向用户介绍你所接入的MCP Server。"
            + "以下是用户的原始指令："
            + f"{message}\n\n"
            + "注意，用户提到的MCP Server指的是你接入内置Server之外的MCP Server，请不要向用户提及内置Server。"
            + "除了用户指令外，你还需要向用户介绍你接入的MCP Server，包括Server的名称、功能、以及每个Server下的各tool的详细信息。"
            + f"请将这些内容写入{WORKSPACE_ROOT}/temp/mcp_server_list.md文件中",
            "outputs": [
                {"name": "mcp_server_list", "file": f"{WORKSPACE_ROOT}/temp/mcp_server_list.md"}
            ],
            "server_config": [
                {
                    "connection_type": "sse",
                    "server_url": server_url,
                    "command": None,
                    "args": None,
                    "server_id": None
                }
            ]
        }
        
        agent_name = "MCP Test Agent"
        
        # 设置需要清理的文件列表
        cleanup_files = [f"{WORKSPACE_ROOT}/temp/mcp_server_list.md"]
        
        # 使用通用生成器创建流式响应
        stream_generator = create_stream_generator(task_name, task_config, agent_name, cleanup_files)
        return create_streaming_response(stream_generator)
    
    except Exception as e:
        logger.error(f"处理上传文件时出错: {str(e)}", exc_info=True)
        # 确保清理临时文件
        if os.path.exists(f"{WORKSPACE_ROOT}/temp/mcp_server_list.md"):
            os.remove(f"{WORKSPACE_ROOT}/temp/mcp_server_list.md")
        raise HTTPException(status_code=500, detail=f"处理文件时出错: {str(e)}")
    
    
# 添加代码分析任务的POST API端点
@app.post("/api/agent/code_analysis", tags=["api"])
async def code_analysis_upload(file: UploadFile = File(...)):
    """
    上传ZIP或PY文件并执行代码分析任务
    
    参数:
        file: ZIP格式的代码文件或单个Python文件
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
        最后一个事件包含任务特定的最终结果
    """
    # 确保temp目录存在
    workspace = Path(f"{WORKSPACE_ROOT}")
    workspace.mkdir(parents=True, exist_ok=True)
    
    # 生成唯一的文件名和解压目录
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    original_filename = f"{workspace}/{timestamp}_{file.filename}"
    extract_path = f"{workspace}/{timestamp}_extracted"
    
    try:
        # 保存上传的文件
        with open(original_filename, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # 获取文件扩展名
        file_ext = os.path.splitext(file.filename)[1].lower()
        
        if file_ext == '.zip':
            # ZIP文件：解压处理
            logger.info(f"检测到ZIP文件，解压到: {extract_path}")
            extract_zip(original_filename, extract_path)
        elif file_ext == '.py':
            # PY文件：创建目录并拷贝文件
            logger.info(f"检测到Python文件，创建目录并拷贝到: {extract_path}")
            os.makedirs(extract_path, exist_ok=True)
            destination_file = os.path.join(extract_path, file.filename)
            shutil.copy2(original_filename, destination_file)
        else:
            raise HTTPException(
                status_code=400, 
                detail=f"不支持的文件类型: {file_ext}。只支持 .zip 和 .py 文件"
            )
        
        # 使用与code_analysis任务相同的配置
        task_name = "code_analysis"
        task_config = {
            "prompt": get_code_analysis_prompt(workspace=workspace, 
                                               main_code=file.filename,
                                               input_dir=extract_path),
            "outputs": [
                {"name": "function", "file": f"{WORKSPACE_ROOT}/temp/function.json"}
            ],
            "server_config": [
                {
                    "connection_type": "stdio",
                    "server_url": None,
                    "command": None,
                    "args": None,
                    "server_id": None
                }
            ]
        }
        
        agent_name = "Code Analysis Agent"
        
        # 设置需要清理的文件列表
        cleanup_files = [original_filename, extract_path]
        
        # 使用通用生成器创建流式响应
        stream_generator = create_stream_generator(task_name, task_config, agent_name, cleanup_files)
        return create_streaming_response(stream_generator)
    
    except Exception as e:
        logger.error(f"处理上传文件时出错: {str(e)}", exc_info=True)
        # 确保清理临时文件
        if os.path.exists(original_filename):
            os.remove(original_filename)
        if os.path.exists(extract_path):
            shutil.rmtree(extract_path)
        raise HTTPException(status_code=500, detail=f"处理文件时出错: {str(e)}")
    
# 添加服务封装任务的POST API端点
@app.post("/api/agent/service_packaging", tags=["api"])
async def service_packaging_upload(file: UploadFile = File(...)):
    """
    上传ZIP或PY文件并执行服务封装任务
    
    参数:
        file: ZIP格式的代码文件或单个Python文件
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
        最后一个事件包含任务特定的最终结果
    """
    # 确保temp目录存在
    workspace = Path(f"{WORKSPACE_ROOT}")
    workspace.mkdir(parents=True, exist_ok=True)
    
    # 生成唯一的文件名和解压目录
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    original_filename = f"{workspace}/{timestamp}_{file.filename}"
    extract_path = f"{workspace}/{timestamp}_extracted"
    
    try:
        # 保存上传的文件
        with open(original_filename, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # 获取文件扩展名
        file_ext = os.path.splitext(file.filename)[1].lower()
        
        if file_ext == '.zip':
            # ZIP文件：解压处理
            logger.info(f"检测到ZIP文件，解压到: {extract_path}")
            extract_zip(original_filename, extract_path)
        elif file_ext == '.py':
            # PY文件：创建目录并拷贝文件
            logger.info(f"检测到Python文件，创建目录并拷贝到: {extract_path}")
            os.makedirs(extract_path, exist_ok=True)
            destination_file = os.path.join(extract_path, file.filename)
            shutil.copy2(original_filename, destination_file)
        else:
            raise HTTPException(
                status_code=400, 
                detail=f"不支持的文件类型: {file_ext}。只支持 .zip 和 .py 文件"
            )
        
        # Agent配置
        task_name = "service_packaging"
        task_config = {
            "prompt": get_service_packaging_prompt(workspace=workspace, 
                                               main_code=file.filename,
                                               input_dir=extract_path),
            "outputs": [],  # 清空outputs，因为我们将直接返回压缩的zip文件
            "server_config": [
                {
                    "connection_type": "stdio",
                    "server_url": None,
                    "command": None,
                    "args": None,
                    "server_id": None
                }
            ]
        }
        
        agent_name = "Service Packaging Agent"
        
        # 设置需要清理的文件列表（不包括extract_path，因为它会在压缩后被清理）
        cleanup_files = [original_filename, extract_path]
        
        # 使用通用生成器创建流式响应，传入zip_extract_path启用zip压缩功能
        stream_generator = create_stream_generator(task_name, task_config, agent_name, cleanup_files, zip_extract_path=extract_path)
        return create_streaming_response(stream_generator)
    
    except Exception as e:
        logger.error(f"处理上传文件时出错: {str(e)}", exc_info=True)
        # 确保清理临时文件
        if os.path.exists(original_filename):
            os.remove(original_filename)
        if os.path.exists(extract_path):
            shutil.rmtree(extract_path)
        raise HTTPException(status_code=500, detail=f"处理文件时出错: {str(e)}")

# 添加反洗钱报告生成任务的POST API端点
@app.post("/api/agent/aml_report", tags=["aml"])
async def aml_report_upload(
    file_url: str = Form(default=None),
    file: UploadFile = File(None)
):
    """
    上传ZIP文件或提供文件URL并执行反洗钱报告生成任务
    
    参数:
        file: ZIP格式的数据集文件（可选）
        file_url: 数据集文件的URL地址（可选）
        
    注意:
        必须提供file或file_url中的一个参数
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
        最后一个事件包含任务特定的最终结果
    """
    # 确保temp目录存在
    workspace = Path(f"{WORKSPACE_ROOT}")
    workspace.mkdir(parents=True, exist_ok=True)
    
    # 生成唯一的文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 检查参数
    has_file = file is not None and hasattr(file, "filename") and file.filename
    has_url = file_url is not None and file_url.strip() != ""
    
    if not has_file and not has_url:
        raise HTTPException(status_code=400, detail="必须提供文件上传或文件URL")
    
    try:
        # 根据提供的参数类型处理文件
        if has_file:
            # 直接上传文件的情况
            zip_filename = f"{workspace}/{timestamp}_{file.filename}"
            with open(zip_filename, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            logger.info(f"已保存上传的文件: {zip_filename}")
        elif has_url:
            # 从URL下载文件的情况
            import requests
            from urllib.parse import urlparse
            
            # 从URL中提取文件名
            parsed_url = urlparse(file_url)
            url_path = parsed_url.path
            file_name = os.path.basename(url_path) or f"dataset_{timestamp}.zip"
            
            zip_filename = f"{workspace}/{timestamp}_{file_name}"
            
            # 下载文件
            logger.info(f"从URL下载文件: {file_url}")
            response = requests.get(file_url, stream=True)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail=f"无法从URL下载文件，状态码: {response.status_code}")
            
            with open(zip_filename, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"已下载文件: {zip_filename}")
        else:
            raise HTTPException(status_code=400, detail="无效的参数组合")
        
        task_name = "aml_report"
        task_config = {
            "prompt": get_aml_report_prompt(workspace=workspace, 
                                           input_dir=zip_filename),
            "outputs": [
                {"name": "report", "file": f"{WORKSPACE_ROOT}/temp/aml_report.md"}
            ],
            "server_config": [
                {
                    "connection_type": "stdio",
                    "server_url": None,
                    "command": "python",
                    "args": ["-m", "app.mcp.aml_server.server"],
                    "server_id": None
                },
                {
                    "connection_type": "stdio",
                    "server_url": None,
                    "command": "python",
                    "args": ["-m", "app.mcp.deepseek_server.server"],
                    "server_id": None
                }
            ]
        }
        
        agent_name = "AML Report Agent"
        
        # 设置需要清理的文件列表
        cleanup_files = [zip_filename]
        
        # 使用通用生成器创建流式响应
        stream_generator = create_stream_generator(task_name, task_config, agent_name, cleanup_files)
        return create_streaming_response(stream_generator)
    
    except Exception as e:
        logger.error(f"处理上传文件时出错: {str(e)}", exc_info=True)
        # 确保清理临时文件
        if 'zip_filename' in locals() and os.path.exists(zip_filename):
            os.remove(zip_filename)
        raise HTTPException(status_code=500, detail=f"处理文件时出错: {str(e)}")

class ServerConfig(BaseModel):
    """服务器配置数据模型"""
    connection_type: str = "stdio"
    server_url: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    server_id: Optional[str] = None

class TaskRequest(BaseModel):
    """任务请求数据模型"""
    task_name: str
    server_config: Optional[List[ServerConfig]] = None
    prompt_override: Optional[str]

# 微服务评测请求数据模型
class EvaluationRequest(BaseModel):
    """微服务评测请求数据模型"""
    service_name: str
    metrics: List[str]

# 微服务评测API端点
@app.post("/api/agent/service_evaluation", tags=["api"])
async def service_evaluation(
    service_name: str = Form(...),
    metrics: str = Form(...),  # 前端会发送JSON字符串或逗号分隔的字符串
    file_url: str = Form(default=None),
    data_file: UploadFile = File(None)
):
    """
    上传ZIP数据文件或提供文件URL并执行原子微服务技术评测任务
    
    参数:
        service_name: 待测试服务的名称
        metrics: 需要评测的指标，privacy, safety-fingerprint, safety-watermark, fairness, robustness, explainability，JSON字符串格式
        data_file: ZIP格式的数据文件（可选）
        file_url: 数据集文件的URL地址（可选）
        
    注意:
        必须提供data_file或file_url中的一个参数
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
        最后一个事件包含评测结果
    """
    # 确保temp目录存在
    workspace = Path(f"{WORKSPACE_ROOT}")
    workspace.mkdir(parents=True, exist_ok=True)
    
    # 生成唯一的文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 检查参数
    has_file = data_file is not None and hasattr(data_file, "filename") and data_file.filename
    has_url = file_url is not None and file_url.strip() != ""
    
    if not has_file and not has_url:
        raise HTTPException(status_code=400, detail="必须提供文件上传或文件URL")
    
    try:
        # 尝试解析metrics参数 - 处理两种可能的格式
        try:
            # 尝试作为JSON数组解析
            metrics_list = json.loads(metrics)
        except json.JSONDecodeError:
            # 如果不是JSON，则作为逗号分隔的字符串处理
            metrics_list = [m.strip() for m in metrics.split(',')]
        
        # 验证指标是否合法
        valid_metrics = ["privacy", "safety-fingerprint", "safety-watermark", "fairness", "robustness", "explainability"]
        for metric in metrics_list:
            if metric not in valid_metrics:
                raise HTTPException(
                    status_code=400, 
                    detail=f"无效的评测指标: {metric}。有效指标为: {', '.join(valid_metrics)}"
                )
        
        # 根据提供的参数类型处理文件
        if has_file:
            # 直接上传文件的情况
            zip_filename = f"{workspace}/{timestamp}_{data_file.filename}"
            with open(zip_filename, "wb") as buffer:
                shutil.copyfileobj(data_file.file, buffer)
            logger.info(f"已保存上传的文件: {zip_filename}")
        elif has_url:
            # 从URL下载文件的情况
            import requests
            from urllib.parse import urlparse
            
            # 从URL中提取文件名
            parsed_url = urlparse(file_url)
            url_path = parsed_url.path
            file_name = os.path.basename(url_path) or f"dataset_{timestamp}.zip"
            
            zip_filename = f"{workspace}/{timestamp}_{file_name}"
            
            # 下载文件
            logger.info(f"从URL下载文件: {file_url}")
            response = requests.get(file_url, stream=True)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail=f"无法从URL下载文件，状态码: {response.status_code}")
            
            with open(zip_filename, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"已下载文件: {zip_filename}")
        else:
            raise HTTPException(status_code=400, detail="无效的参数组合")

        # 创建评测任务的prompt
        prompt = get_service_evaluation_prompt(service_name, metrics_list, zip_filename)
        logger.info(f"评测任务的prompt: {prompt}")
        # 评测任务配置
        task_name = "service_evaluation"
        output_file = f"{WORKSPACE_ROOT}/temp/evaluation_result.json"
        task_config = {
            "prompt": prompt,
            "outputs": [
                {"name": "evaluation_result", "file": output_file}
            ],
            "server_config": [
                {
                    "connection_type": "sse",
                    "server_url": f"{os.getenv('PROJECT_4_MCP')}",
                    "server_id": "project_4_mcp"
                }
            ]
        }
        
        agent_name = "服务评测Agent"
        
        # 设置需要清理的文件列表
        cleanup_files = [zip_filename, output_file]
        
        # 使用通用生成器创建流式响应
        stream_generator = create_stream_generator(task_name, task_config, agent_name, cleanup_files)
        return create_streaming_response(stream_generator)
    
    except json.JSONDecodeError:
        logger.error(f"无效的JSON格式指标: {metrics}")
        raise HTTPException(status_code=400, detail="指标必须是有效的JSON格式数组")
    except Exception as e:
        logger.error(f"处理服务评测请求时出错: {str(e)}", exc_info=True)
        # 确保清理临时文件
        if 'zip_filename' in locals() and os.path.exists(zip_filename):
            os.remove(zip_filename)
        raise HTTPException(status_code=500, detail=f"处理评测请求时出错: {str(e)}")

class MetaAppValidationRequest(BaseModel):
    """元应用数据验证请求数据模型"""
    meta_app_api: str
    metrics: List[str]
    
# 元应用业务验证api
@app.post("/api/agent/meta_app_validation", tags=["api"])
async def meta_app_validation(
    meta_app_api: str = Form(...),
    metrics: str = Form(...),  # 前端会发送JSON字符串或逗号分隔的字符串
    file_url: str = Form(default=None),
    data_file: UploadFile = File(None)
):
    """
    上传ZIP数据文件或提供文件URL并执行元应用数据验证任务
    
    参数:
        meta_app_api: 待测试的元应用API端点（SSE端点）
        metrics: 需要评测的指标(查全率/查准率/计算效率中的一个或多个)，JSON字符串格式
        data_file: ZIP格式的数据文件（可选）
        file_url: 数据集文件的URL地址（可选）
        
    注意:
        必须提供data_file或file_url中的一个参数
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
        最后一个事件包含评测结果
    """
    # 确保temp目录存在
    workspace = Path(f"{WORKSPACE_ROOT}")
    workspace.mkdir(parents=True, exist_ok=True)
    
    # 生成唯一的文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 检查参数
    has_file = data_file is not None and hasattr(data_file, "filename") and data_file.filename
    has_url = file_url is not None and file_url.strip() != ""
    
    if not has_file and not has_url:
        raise HTTPException(status_code=400, detail="必须提供文件上传或文件URL")
    
    try:
        # 尝试解析metrics参数 - 处理两种可能的格式
        try:
            # 尝试作为JSON数组解析
            metrics_list = json.loads(metrics)
        except json.JSONDecodeError:
            # 如果不是JSON，则作为逗号分隔的字符串处理
            metrics_list = [m.strip() for m in metrics.split(',')]
        
        # 验证指标是否合法
        valid_metrics = ["查全率", "查准率", "计算效率"]
        for metric in metrics_list:
            if metric not in valid_metrics:
                raise HTTPException(
                    status_code=400, 
                    detail=f"无效的评测指标: {metric}。有效指标为: {', '.join(valid_metrics)}"
                )
        
        # 根据提供的参数类型处理文件
        if has_file:
            # 直接上传文件的情况
            zip_filename = f"{workspace}/{timestamp}_{data_file.filename}"
            with open(zip_filename, "wb") as buffer:
                shutil.copyfileobj(data_file.file, buffer)
            logger.info(f"已保存上传的文件: {zip_filename}")
        elif has_url:
            # 从URL下载文件的情况
            import requests
            from urllib.parse import urlparse
            
            # 从URL中提取文件名
            parsed_url = urlparse(file_url)
            url_path = parsed_url.path
            file_name = os.path.basename(url_path) or f"dataset_{timestamp}.zip"
            
            zip_filename = f"{workspace}/{timestamp}_{file_name}"
            
            # 下载文件
            logger.info(f"从URL下载文件: {file_url}")
            response = requests.get(file_url, stream=True)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail=f"无法从URL下载文件，状态码: {response.status_code}")
            
            with open(zip_filename, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"已下载文件: {zip_filename}")
        else:
            raise HTTPException(status_code=400, detail="无效的参数组合")

        # 创建评测任务的prompt
        prompt = get_meta_app_validation_prompt(meta_app_api, metrics_list, zip_filename)
        logger.info(f"元应用数据验证任务的prompt: {prompt}")
        
        # 评测任务配置
        task_name = "meta_app_validation"
        output_file = f"{WORKSPACE_ROOT}/temp/validation_result.json"
        task_config = {
            "prompt": prompt,
            "outputs": [
                {"name": "validation_result", "file": output_file}
            ],
            "server_config": [
                # 如有需要可以定义具体的服务器配置
            ]
        }
        
        agent_name = "元应用数据验证Agent"
        
        # 设置需要清理的文件列表
        cleanup_files = [zip_filename, output_file]
        
        # 使用通用生成器创建流式响应
        stream_generator = create_stream_generator(task_name, task_config, agent_name, cleanup_files)
        return create_streaming_response(stream_generator)
    
    except json.JSONDecodeError:
        logger.error(f"无效的JSON格式指标: {metrics}")
        raise HTTPException(status_code=400, detail="指标必须是有效的JSON格式数组")
    except Exception as e:
        logger.error(f"处理元应用数据验证请求时出错: {str(e)}", exc_info=True)
        # 确保清理临时文件
        if 'zip_filename' in locals() and os.path.exists(zip_filename):
            os.remove(zip_filename)
        raise HTTPException(status_code=500, detail=f"处理元应用数据验证请求时出错: {str(e)}")

# 反洗钱模型评估
@app.post("/api/agent/aml_model_evaluation", tags=["api"])
async def aml_model_evaluation(
    model_name: str = Form(...),
    metrics: str = Form(...),  # 前端会发送JSON字符串或逗号分隔的字符串
    file_url: str = Form(default=None),
    data_file: UploadFile = File(None)
):
    """
    上传ZIP数据文件或提供文件URL并执行AML模型技术评测任务
    
    参数:
        model_name: 需要评测的模型名称
        metrics: 需要评测的指标，privacy, safety-fingerprint, safety-watermark, fairness, robustness, explainability，JSON字符串格式
        data_file: ZIP格式的数据集文件（可选）
        file_url: 数据集文件的URL地址（可选）
        
    注意:
        必须提供data_file或file_url中的一个参数
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
        最后一个事件包含评测结果
    """
    # 确保temp目录存在
    workspace = Path(f"{WORKSPACE_ROOT}")
    workspace.mkdir(parents=True, exist_ok=True)
    
    # 生成唯一的文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 检查参数
    has_file = data_file is not None and hasattr(data_file, "filename") and data_file.filename
    has_url = file_url is not None and file_url.strip() != ""
    
    if not has_file and not has_url:
        raise HTTPException(status_code=400, detail="必须提供文件上传或文件URL")
    
    try:
        # 尝试解析metrics参数 - 处理两种可能的格式
        try:
            # 尝试作为JSON数组解析
            metrics_list = json.loads(metrics)
        except json.JSONDecodeError:
            # 如果不是JSON，则作为逗号分隔的字符串处理
            metrics_list = [m.strip() for m in metrics.split(',')]
        
        # 验证指标是否合法
        valid_metrics = ["privacy", "safety-fingerprint", "safety-watermark", "fairness", "robustness", "explainability"]
        for metric in metrics_list:
            if metric not in valid_metrics:
                raise HTTPException(
                    status_code=400, 
                    detail=f"无效的评测指标: {metric}。有效指标为: {', '.join(valid_metrics)}"
                )
        
        # 根据提供的参数类型处理文件
        if has_file:
            # 直接上传文件的情况
            zip_filename = f"{workspace}/{timestamp}_{data_file.filename}"
            with open(zip_filename, "wb") as buffer:
                shutil.copyfileobj(data_file.file, buffer)
            logger.info(f"已保存上传的文件: {zip_filename}")
        elif has_url:
            # 从URL下载文件的情况
            import requests
            from urllib.parse import urlparse
            
            # 从URL中提取文件名
            parsed_url = urlparse(file_url)
            url_path = parsed_url.path
            file_name = os.path.basename(url_path) or f"dataset_{timestamp}.zip"
            
            zip_filename = f"{workspace}/{timestamp}_{file_name}"
            
            # 下载文件
            logger.info(f"从URL下载文件: {file_url}")
            response = requests.get(file_url, stream=True)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail=f"无法从URL下载文件，状态码: {response.status_code}")
            
            with open(zip_filename, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"已下载文件: {zip_filename}")
        else:
            raise HTTPException(status_code=400, detail="无效的参数组合")

        # 创建评测任务的prompt
        prompt = get_aml_model_evaluation_prompt(model_name, zip_filename, metrics_list)
        logger.info(f"AML模型技术评测任务的prompt: {prompt}")
        
        # 评测任务配置
        task_name = "aml_model_evaluation"
        output_file = f"{WORKSPACE_ROOT}/temp/model_evaluation_result.json"
        task_config = {
            "prompt": prompt,
            "outputs": [
                {"name": "evaluation_result", "file": output_file}
            ],
            "server_config": [
                # 如有需要可以定义具体的服务器配置
                {
                    "connection_type": "sse",
                    "server_url": f"{os.getenv('PROJECT_4_MCP')}",
                    "server_id": "project_4_mcp"
                }
            ]
        }
        
        agent_name = "AML模型技术评测Agent"
        
        # 设置需要清理的文件列表
        cleanup_files = [zip_filename, output_file]
        
        # 使用通用生成器创建流式响应
        stream_generator = create_stream_generator(task_name, task_config, agent_name, cleanup_files)
        return create_streaming_response(stream_generator)
    
    except json.JSONDecodeError:
        logger.error(f"无效的JSON格式指标: {metrics}")
        raise HTTPException(status_code=400, detail="指标必须是有效的JSON格式数组")
    except Exception as e:
        logger.error(f"处理AML模型技术评测请求时出错: {str(e)}", exc_info=True)
        # 确保清理临时文件
        if 'zip_filename' in locals() and os.path.exists(zip_filename):
            os.remove(zip_filename)
        raise HTTPException(status_code=500, detail=f"处理AML模型技术评测请求时出错: {str(e)}")

# MCP服务推荐
@app.post("/api/agent/mcp_service_recommendation", tags=["api"])
async def mcp_service_recommendation(
    message: str = Form(...),
    service_type: str = Form(...)
):
    """
    根据用户需求推荐合适的MCP服务
    
    参数:
        message: 用户的需求描述
        service_type: 服务类型，用于过滤domain字段
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
        最后一个事件包含推荐结果
    """
    try:
        # 创建推荐任务的prompt
        prompt = get_mcp_service_recommendation_prompt(message, service_type)
        logger.info(f"MCP服务推荐任务的prompt: {prompt}")
        
        # 任务配置
        task_name = "mcp_service_recommendation"
        output_file = f"{WORKSPACE_ROOT}/temp/mcp_recommendation_result.json"
        task_config = {
            "prompt": prompt,
            "outputs": [
                {"name": "recommendation_result", "file": output_file}
            ],
            "server_config": [
                {
                    "connection_type": "stdio",
                    "server_url": None,
                    "command": "python",
                    "args": ["-m", "app.mcp.mysql_server.server"],
                    "server_id": "mysql_server"
                }
            ]
        }
        
        agent_name = "MCP服务推荐Agent"
        
        # 设置需要清理的文件列表
        cleanup_files = [output_file]
        
        # 使用通用生成器创建流式响应
        stream_generator = create_stream_generator(task_name, task_config, agent_name, cleanup_files)
        return create_streaming_response(stream_generator)
    
    except Exception as e:
        logger.error(f"处理MCP服务推荐请求时出错: {str(e)}", exc_info=True)
        # 确保清理临时文件
        if 'output_file' in locals() and os.path.exists(output_file):
            os.remove(output_file)
        raise HTTPException(status_code=500, detail=f"处理推荐请求时出错: {str(e)}")

# 启动应用
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010) 