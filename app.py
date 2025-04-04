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

from app.config import WORKSPACE_ROOT

from app.task.demo_task import demo_task_configs
# 导入任务提示
from app.task.code_analysis_task import (
    get_code_analysis_prompt
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
                                  cleanup_files: List[str] = None):
    """
    创建通用的流式响应生成器
    
    参数:
        task_name: 任务名称
        task_config: 任务配置
        agent_name: Agent名称
        cleanup_files: 任务完成后需要清理的文件列表
        
    返回:
        异步生成器，产生SSE格式的事件流
    """
    from run_mcp import MCPRunner
    
    runner = None
    full_result = []
    try:
        runner = MCPRunner(agent_name)
        
        # 从任务配置中获取服务器配置
        server_config = task_config.get("server_config", {})
        connection_type = server_config.get("connection_type", "stdio")
        server_url = server_config.get("server_url")
        command = server_config.get("command")
        args = server_config.get("args")
        server_id = server_config.get("server_id")
        
        # 先添加内置的MCP服务器
        logger.info("添加默认内置MCP服务器")
        await runner.add_server(
            connection_type="stdio",
            server_url=None,
            command=None,  # 使用默认Python解释器
            args=None,     # 使用默认的app.mcp.server模块
            server_id="stdio_built_in"  # 指定一个固定ID以便识别
        )
        
        # 如果配置了其他服务器，也添加它
        if server_url or command:
            logger.info("添加用户配置的MCP服务器")
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
                
                # 按照任务配置读取输出文件
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
                asyncio.create_task(runner.cleanup())
                # 给清理任务一点时间启动
                await asyncio.sleep(0.1)
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

# 添加代码分析任务的POST API端点
@app.post("/api/agent/code_analysis", tags=["api"])
async def code_analysis_upload(file: UploadFile = File(...)):
    """
    上传ZIP文件并执行代码分析任务
    
    参数:
        file: ZIP格式的代码文件
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
        最后一个事件包含任务特定的最终结果
    """
    # 确保temp目录存在
    workspace = Path(f"{WORKSPACE_ROOT}")
    workspace.mkdir(parents=True, exist_ok=True)
    
    # 生成唯一的文件名和解压目录
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"{workspace}/{timestamp}_{file.filename}"
    extract_path = f"{workspace}/{timestamp}_extracted"
    
    try:
        # 保存上传的文件
        with open(zip_filename, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # 解压文件
        logger.info(f"解压文件到: {extract_path}")
        extract_zip(zip_filename, extract_path)
        
        # 使用与code_analysis任务相同的配置
        task_name = "code_analysis"
        task_config = {
            "prompt": get_code_analysis_prompt(workspace=workspace, 
                                               input_dir=extract_path),
            "outputs": [
                {"name": "function", "file": f"{WORKSPACE_ROOT}/temp/function.json"}
            ],
            "server_config": {
                "connection_type": "stdio",
                "server_url": None,
                "command": None,
                "args": None,
                "server_id": None
            }
        }
        
        agent_name = "Code Analysis Agent"
        
        # 设置需要清理的文件列表
        cleanup_files = [zip_filename, extract_path]
        
        # 使用通用生成器创建流式响应
        stream_generator = create_stream_generator(task_name, task_config, agent_name, cleanup_files)
        return create_streaming_response(stream_generator)
    
    except Exception as e:
        logger.error(f"处理上传文件时出错: {str(e)}", exc_info=True)
        # 确保清理临时文件
        if os.path.exists(zip_filename):
            os.remove(zip_filename)
        if os.path.exists(extract_path):
            shutil.rmtree(extract_path)
        raise HTTPException(status_code=500, detail=f"处理文件时出错: {str(e)}")

class ServerConfig(BaseModel):
    """服务器配置数据模型
    
    无论是否提供此配置，系统都会首先添加一个内置的MCP服务器。
    此配置仅用于添加额外的服务器连接。
    
    如果指定connection_type='stdio'且不指定command和args，
    则会使用系统默认的Python解释器启动内置MCP服务器模块。
    
    如果指定connection_type='sse'，则必须提供有效的server_url。
    """
    connection_type: str = "stdio"  # 'stdio'使用内置MCP服务器，'sse'连接远程服务器
    server_url: Optional[str] = None  # SSE连接必须提供此项
    command: Optional[str] = None  # stdio连接的命令，默认为sys.executable
    args: Optional[List[str]] = None  # 命令参数，默认为["-m", "app.mcp.server"]
    server_id: Optional[str] = None  # 服务器ID，默认自动生成
    add_built_in: bool = True  # 是否同时添加内置服务器（默认为True）

class TaskRequest(BaseModel):
    """任务请求数据模型"""
    task_name: str
    server_config: Optional[ServerConfig] = None
    prompt_override: Optional[str] = None
    
# 启动应用
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000) 