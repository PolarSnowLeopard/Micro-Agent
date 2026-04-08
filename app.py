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

from app.config import PROJECT_ROOT, WORKSPACE_ROOT

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
from app.task.mcp_test import (
    get_mcp_test_prompt
)
from app.task.aml_auto_generate import (
    get_aml_auto_generate_prompt
)
from app.task.aml_auto_generate_knowledge import (
    build_knowledge_context
)
from app.utils.file_utils import extract_zip
from run_meta_app import MetaAppRunner

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
async def create_stream_generator_with_adaptation(
    task_name: str, 
    task_config: Dict[str, Any], 
    agent_name: str, 
    cleanup_files: List[str] = None, 
    zip_extract_path: str = None
):
    """
    创建支持数据适配的流式响应生成器
    
    参数:
        task_name: 任务名称
        task_config: 任务配置（包含 enable_adaptation 标志）
        agent_name: Agent名称
        cleanup_files: 任务完成后需要清理的文件列表
        zip_extract_path: 如果指定，将此目录压缩成zip文件并返回
        
    返回:
        异步生成器，产生SSE格式的事件流
    """
    from run_mcp import MCPRunner
    import sys
    
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
        
        # 关键：如果启用数据适配，连接数据适配MCP服务器
        enable_adaptation = task_config.get("enable_adaptation", False)
        if enable_adaptation:
            logger.info("=" * 60)
            logger.info("连接数据适配 MCP 服务器...")
            
            try:
                # 获取数据适配MCP服务器的路径
                data_adaptation_server_path = Path(__file__).parent / "app" / "mcp" / "data_adaptation_server" / "server.py"
                
                if not data_adaptation_server_path.exists():
                    logger.error(f"数据适配MCP服务器文件不存在: {data_adaptation_server_path}")
                    raise FileNotFoundError(f"数据适配MCP服务器文件不存在")
                
                # 连接数据适配MCP服务器
                await runner.add_server(
                    connection_type="stdio",
                    server_url=None,
                    command=sys.executable,  # 使用当前Python解释器
                    args=[str(data_adaptation_server_path)],
                    server_id="data_adaptation_mcp"
                )
                
                logger.info("成功连接数据适配 MCP 服务器")
                logger.info("   可用工具:")
                logger.info("   - data_adaptation_mcp_analyze_data (数据分析)")
                logger.info("   - data_adaptation_mcp_get_service_schema (获取服务Schema)")
                logger.info("   - data_adaptation_mcp_analyze_schema_mapping (Schema映射分析)")
                logger.info("   - data_adaptation_mcp_generate_transform_code (转换代码生成)")
                logger.info("=" * 60)
            except Exception as e:
                logger.error(f"连接数据适配MCP服务器失败: {str(e)}")
                logger.error("将继续执行，但数据适配功能可能不可用")
        
        # 遍历并添加配置中的其他服务器
        if server_configs:
            for idx, server_config in enumerate(server_configs):
                connection_type = server_config.get("connection_type", "stdio")
                server_url = server_config.get("server_url")
                command = server_config.get("command")
                args = server_config.get("args")
                server_id = server_config.get("server_id") or f"server_{idx}"
                
                # 根据连接类型检查是否有必要的配置
                should_add = False
                if connection_type == "sse" and server_url:
                    should_add = True
                elif connection_type == "stdio" and command:
                    should_add = True
                
                if should_add:
                    logger.info(f"添加配置的MCP服务器 #{idx+1} (类型: {connection_type})")
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
                        
                        # 获取项目文件夹名称（保留目录结构）
                        project_folder_name = os.path.basename(zip_extract_path)
                        
                        # 压缩目录，保留顶层文件夹结构
                        import zipfile
                        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                            for root, dirs, files in os.walk(zip_extract_path):
                                for file in files:
                                    file_path = os.path.join(root, file)
                                    # 保留项目文件夹名称作为zip中的顶层目录
                                    rel_path = os.path.relpath(file_path, os.path.dirname(zip_extract_path))
                                    zipf.write(file_path, rel_path)
                        
                        logger.info(f"已创建压缩包: {zip_filename}，顶层文件夹: {project_folder_name}")
                        
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
                
                if task_name == "aml_auto_generate":
                    _recover_aml_generate_result_if_missing(task_config, final_results)

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
                runner.cleanup()
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


def _recover_aml_generate_result_if_missing(
    task_config: Dict[str, Any], final_results: Dict[str, Any]
) -> None:
    """
    aml_auto_generate：若智能体未写出 JSON 或写错目录，尝试从备用路径读取；
    仍失败则从 workspace/temp/*_algorithm.py 回退组装，避免前端拿不到最终结果。
    """
    if final_results.get("aml_generate_result"):
        return

    alt_json_paths = [
        Path(WORKSPACE_ROOT) / "temp" / "aml_generate_result.json",
        PROJECT_ROOT / "temp" / "aml_generate_result.json",
    ]
    for ap in alt_json_paths:
        try:
            if ap.is_file():
                raw = ap.read_text(encoding="utf-8")
                try:
                    final_results["aml_generate_result"] = json.loads(raw)
                except json.JSONDecodeError:
                    final_results["aml_generate_result"] = raw
                logger.info(f"已从备用路径读取 aml_generate_result: {ap}")
                return
        except OSError as e:
            logger.warning(f"读取备用 JSON 失败 {ap}: {e}")

    meta = task_config.get("meta") or {}
    model_name = (meta.get("model_name") or "").strip()
    temp_dir = Path(WORKSPACE_ROOT) / "temp"
    py_candidates: List[Path] = []
    if model_name:
        py_candidates.append(temp_dir / f"{model_name}_algorithm.py")
    try:
        if temp_dir.is_dir():
            py_candidates.extend(
                sorted(
                    temp_dir.glob("*_algorithm.py"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            )
    except OSError:
        pass

    seen = set()
    for py_path in py_candidates:
        try:
            key = str(py_path.resolve())
        except OSError:
            key = str(py_path)
        if key in seen or not py_path.is_file():
            continue
        seen.add(key)
        try:
            code = py_path.read_text(encoding="utf-8")
            stem = py_path.stem
            model_guess = stem[: -len("_algorithm")] if stem.endswith("_algorithm") else stem
            final_results["aml_generate_result"] = {
                "model_name": model_name or model_guess,
                "generated_code": code,
                "code_filename": py_path.name,
                "test_results": [],
                "references": [],
            }
            logger.warning(
                "未找到 aml_generate_result.json，已从源码文件回退组装最终结果: %s",
                py_path,
            )
            return
        except OSError as e:
            logger.warning(f"回退读取源码失败 {py_path}: {e}")


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
                
                # 根据连接类型检查是否有必要的配置
                should_add = False
                if connection_type == "sse" and server_url:
                    should_add = True
                elif connection_type == "stdio" and command:
                    should_add = True
                
                if should_add:
                    logger.info(f"添加配置的MCP服务器 #{idx+1} (类型: {connection_type})")
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
                        
                        # 获取项目文件夹名称（保留目录结构）
                        project_folder_name = os.path.basename(zip_extract_path)
                        
                        # 压缩目录，保留顶层文件夹结构
                        import zipfile
                        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                            for root, dirs, files in os.walk(zip_extract_path):
                                for file in files:
                                    file_path = os.path.join(root, file)
                                    # 保留项目文件夹名称作为zip中的顶层目录
                                    rel_path = os.path.relpath(file_path, os.path.dirname(zip_extract_path))
                                    zipf.write(file_path, rel_path)
                        
                        logger.info(f"已创建压缩包: {zip_filename}，顶层文件夹: {project_folder_name}")
                        
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
                
                if task_name == "aml_auto_generate":
                    _recover_aml_generate_result_if_missing(task_config, final_results)

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

# 辅助函数：查找项目主入口文件
def find_project_main_file(project_root: str) -> str:
    """
    在项目根目录中查找主入口文件
    
    参数:
        project_root: 项目根目录路径
    
    返回:
        主入口文件的文件名（相对于项目根目录），如果没有找到则返回空字符串
    """
    try:
        # 只查找根目录下的.py文件（不递归）
        py_files = [f for f in os.listdir(project_root) 
                    if f.endswith('.py') and os.path.isfile(os.path.join(project_root, f))]
        
        if not py_files:
            return ""
        
        # 如果只有一个.py文件，直接返回
        if len(py_files) == 1:
            return py_files[0]
        
        # 如果有多个.py文件，按优先级查找
        priority_names = ['main.py', 'app.py', 'server.py', 'run.py', 'start.py', '__main__.py']
        for name in priority_names:
            if name in py_files:
                return name
        
        # 如果没有匹配的优先名称，返回第一个
        return py_files[0]
    
    except Exception as e:
        logger.warning(f"查找主入口文件时出错: {str(e)}")
        return ""

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

# 元应用演示页面路由
@app.get("/meta_app_demo", tags=["demo"])
async def meta_app_demo():
    """
    返回元应用智能体演示页面
    """
    return FileResponse("static/meta_app_demo.html")

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
            "prompt": get_mcp_test_prompt(message),
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
        
        # 实际的项目根路径
        actual_project_path = extract_path
        
        if file_ext == '.zip':
            # ZIP文件：解压处理
            logger.info(f"检测到ZIP文件，解压到: {extract_path}")
            extract_zip(original_filename, extract_path)
            
            # 检测实际的项目根路径（处理zip中有顶层文件夹的情况）
            items = os.listdir(extract_path)
            # 过滤掉隐藏文件和特殊文件夹
            ignore_items = {'.git', '__MACOSX', '.DS_Store', '.gitignore', '.gitattributes', 
                           'Thumbs.db', 'desktop.ini'}
            visible_items = [item for item in items 
                           if not item.startswith('.') and item not in ignore_items]
            
            logger.info(f"解压后的内容（过滤后）: {visible_items}")
            
            # 如果过滤后只有一个文件夹，那这个文件夹就是项目根目录
            if len(visible_items) == 1 and os.path.isdir(os.path.join(extract_path, visible_items[0])):
                actual_project_path = os.path.join(extract_path, visible_items[0])
                logger.info(f"检测到项目根路径: {actual_project_path}")
            else:
                actual_project_path = extract_path
                logger.info(f"使用解压路径作为项目根路径: {actual_project_path}")
                
        elif file_ext == '.py':
            # PY文件：创建项目目录结构
            logger.info(f"检测到Python文件，创建目录并拷贝到: {extract_path}")
            # 创建一个以文件名命名的项目文件夹
            project_name = os.path.splitext(file.filename)[0]
            actual_project_path = os.path.join(extract_path, project_name)
            os.makedirs(actual_project_path, exist_ok=True)
            destination_file = os.path.join(actual_project_path, file.filename)
            shutil.copy2(original_filename, destination_file)
        else:
            raise HTTPException(
                status_code=400, 
                detail=f"不支持的文件类型: {file_ext}。只支持 .zip 和 .py 文件"
            )
        
        # 查找项目的主入口文件
        main_code = find_project_main_file(actual_project_path)
        if main_code:
            logger.info(f"找到主入口文件: {main_code}")
        else:
            logger.warning("未在项目根目录中找到.py文件")
        
        # 使用与code_analysis任务相同的配置，使用实际的项目根路径和找到的主入口文件
        task_name = "code_analysis"
        task_config = {
            "prompt": get_code_analysis_prompt(workspace=workspace, 
                                               main_code=main_code,
                                               input_dir=actual_project_path),
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
        
        # 实际的项目根路径
        actual_project_path = extract_path
        
        if file_ext == '.zip':
            # ZIP文件：解压处理
            logger.info(f"检测到ZIP文件，解压到: {extract_path}")
            extract_zip(original_filename, extract_path)
            
            # 检测实际的项目根路径（处理zip中有顶层文件夹的情况）
            items = os.listdir(extract_path)
            # 过滤掉隐藏文件和特殊文件夹
            ignore_items = {'.git', '__MACOSX', '.DS_Store', '.gitignore', '.gitattributes', 
                           'Thumbs.db', 'desktop.ini'}
            visible_items = [item for item in items 
                           if not item.startswith('.') and item not in ignore_items]
            
            logger.info(f"解压后的内容（过滤后）: {visible_items}")
            
            # 如果过滤后只有一个文件夹，那这个文件夹就是项目根目录
            if len(visible_items) == 1 and os.path.isdir(os.path.join(extract_path, visible_items[0])):
                actual_project_path = os.path.join(extract_path, visible_items[0])
                logger.info(f"检测到项目根路径: {actual_project_path}")
            else:
                actual_project_path = extract_path
                logger.info(f"使用解压路径作为项目根路径: {actual_project_path}")
                
        elif file_ext == '.py':
            # PY文件：创建项目目录结构
            logger.info(f"检测到Python文件，创建目录并拷贝到: {extract_path}")
            # 创建一个以文件名命名的项目文件夹
            project_name = os.path.splitext(file.filename)[0]
            actual_project_path = os.path.join(extract_path, project_name)
            os.makedirs(actual_project_path, exist_ok=True)
            destination_file = os.path.join(actual_project_path, file.filename)
            shutil.copy2(original_filename, destination_file)
        else:
            raise HTTPException(
                status_code=400, 
                detail=f"不支持的文件类型: {file_ext}。只支持 .zip 和 .py 文件"
            )
        
        # 查找项目的主入口文件
        main_code = find_project_main_file(actual_project_path)
        if main_code:
            logger.info(f"找到主入口文件: {main_code}")
        else:
            logger.warning("未在项目根目录中找到.py文件")
        
        # Agent配置，使用实际的项目根路径和找到的主入口文件
        task_name = "service_packaging"
        task_config = {
            "prompt": get_service_packaging_prompt(workspace=workspace, 
                                               main_code=main_code,
                                               input_dir=actual_project_path),
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
        # 传入actual_project_path作为需要压缩的路径
        stream_generator = create_stream_generator(task_name, task_config, agent_name, cleanup_files, zip_extract_path=actual_project_path)
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
    data_file: UploadFile = File(None),
    dataset_type: str = Form(default='1'),  # 新增：数据集类型 ('0'=平台, '1'=上载, '2'=开源)
    enable_adaptation: str = Form(default='true')  # 新增：是否启用数据适配
):
    """
    上传ZIP数据文件或提供文件URL并执行AML模型技术评测任务（支持智能数据适配）
    
    参数:
        model_name: 需要评测的模型名称
        metrics: 需要评测的指标，privacy, safety-fingerprint, safety-watermark, fairness, robustness, explainability，JSON字符串格式
        data_file: ZIP格式的数据集文件（可选）
        file_url: 数据集文件的URL地址（可选）
        dataset_type: 数据集类型，'0'=平台数据集, '1'=用户上载数据集, '2'=开源数据集
        enable_adaptation: 是否启用智能数据适配，'true'或'false'
        
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

        # 判断是否启用数据适配
        use_adaptation = enable_adaptation.lower() == 'true'
        
        # 创建评测任务的prompt
        if use_adaptation:
            # 使用增强的数据适配Prompt
            from app.task.aml_model_evaluation import get_aml_model_evaluation_prompt_with_adaptation
            data_info = {
                'dataset_type': dataset_type,
                'data_path': zip_filename,
                'data_url': file_url if file_url else ''
            }
            prompt = get_aml_model_evaluation_prompt_with_adaptation(model_name, data_info, metrics_list)
            logger.info(f"使用数据适配模式的AML模型技术评测")
        else:
            # 使用原始Prompt（向后兼容）
            prompt = get_aml_model_evaluation_prompt(model_name, zip_filename, metrics_list)
            logger.info(f"使用标准模式的AML模型技术评测")
        
        logger.info(f"AML模型技术评测任务的prompt: {prompt[:200]}...")
        
        # 评测任务配置
        task_name = "aml_model_evaluation"
        output_file = f"{WORKSPACE_ROOT}/temp/model_evaluation_result.json"
        
        # 构建server_config - 仅在环境变量配置时添加
        server_configs = []
        project_4_mcp_url = os.getenv('PROJECT_4_MCP')
        if project_4_mcp_url:
            server_configs.append({
                "connection_type": "sse",
                "server_url": project_4_mcp_url,
                "server_id": "project_4_mcp"
            })
        
        task_config = {
            "prompt": prompt,
            "outputs": [
                {"name": "evaluation_result", "file": output_file}
            ],
            "server_config": server_configs,
            # 如果启用数据适配，添加额外的工具
            "enable_adaptation": use_adaptation
        }
        
        agent_name = "AML模型技术评测Agent"
        
        # 设置需要清理的文件列表
        cleanup_files = [zip_filename, output_file]
        
        # 使用增强的生成器创建流式响应（支持注册额外工具）
        stream_generator = create_stream_generator_with_adaptation(
            task_name, task_config, agent_name, cleanup_files
        )
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

# 兼容前端FormData流式调用模式：/api/agent/meta_app/run
@app.post("/api/agent/meta_app/run", tags=["api"])
async def meta_app_run_form(
    message: str = Form(...),
    app_config: str = Form(...),
    use_sim_only: Optional[str] = Form(default=None),
):
    """根据FormData参数运行元应用智能体（流式SSE）。"""
    try:
        # 解析配置
        try:
            config = json.loads(app_config)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"app_config 非法JSON: {str(e)}")

        # 解析模拟开关，默认True
        use_sim = True
        if use_sim_only is not None:
            val = str(use_sim_only).strip().lower()
            use_sim = val in ["1", "true", "yes", "on"]

        # 创建Runner
        runner = MetaAppRunner("Meta App Agent")
        await runner.initialize(config, use_sim_only=use_sim)

        async def stream_generator():
            try:
                yield f"data: {json.dumps({'status': 'start'}, ensure_ascii=False)}\n\n"
                # 记录执行过程中的结果，供最终汇总
                import re
                last_thought = None
                last_action_result = None  # 任意最后结果
                preferred_action_result = None  # 更符合业务的结果（如生成报告）
                preferred_visualization = None
                finalized_payload = None  # 若Agent调用 finalize_meta_result，记录其payload
                info_cfg = config.get("info") or config.get("meta") or {}
                allow_viz = info_cfg.get("outputVisualization")
                if allow_viz is None:
                    allow_viz = False
                async for step in runner.run_stream(message):
                    if isinstance(step, dict):
                        if step.get("thought"):
                            last_thought = step.get("thought")
                        if step.get("action_result"):
                            ar = step.get("action_result") or ""
                            # 解析工具名：Observed output of cmd `TOOL` executed:\n...
                            m = re.search(r"cmd\s+`([^`]+)`\s+executed:", ar)
                            tool_name = (m.group(1) if m else "").lower()
                            # 捕获 finalize_meta_result 的payload
                            if tool_name == "finalize_meta_result":
                                try:
                                    parts = ar.split("\n", 1)
                                    if len(parts) == 2:
                                        obj = json.loads(parts[1])
                                        if isinstance(obj, dict) and (
                                            "text_result" in obj and "visualization_data" in obj and "file_result" in obj
                                        ):
                                            finalized_payload = obj
                                except Exception:
                                    pass
                            # 过滤无意义的终止/健康检查结果
                            if tool_name in ("terminate",) or tool_name.endswith("_terminate"):
                                pass
                            elif "healthcheck" in tool_name:
                                # 只在没有其他结果时作为兜底
                                if not last_action_result:
                                    last_action_result = ar
                            else:
                                # 业务相关结果
                                last_action_result = ar
                                # 优先级：报告类/生成类
                                if any(k in tool_name for k in ["generatereport", "report"]):
                                    preferred_action_result = ar
                                elif any(k in tool_name for k in ["analy", "compute"]):
                                    # 次优先级：分析/计算
                                    if not preferred_action_result:
                                        preferred_action_result = ar

                                # 提取可视化候选（从结果JSON中的“模拟数据”）
                                if allow_viz:
                                    try:
                                        parts = ar.split("\n", 1)
                                        if len(parts) == 2:
                                            obj = json.loads(parts[1])
                                            viz = obj.get("模拟数据") if isinstance(obj, dict) else None
                                            if viz is not None:
                                                preferred_visualization = viz
                                    except Exception:
                                        pass
                    json_result = json.dumps(step, ensure_ascii=False)
                    yield f"data: {json_result}\n\n"

                # 构造最终结果三字段（英文键名）
                if finalized_payload is not None:
                    text_result = finalized_payload.get("text_result")
                    visualization_data = finalized_payload.get("visualization_data") if allow_viz else None
                    file_result = finalized_payload.get("file_result")
                else:
                    text_result = preferred_action_result or last_action_result or last_thought or None
                    visualization_data = preferred_visualization if (allow_viz and preferred_visualization is not None) else None
                    file_result = None

                final_event = {
                    "is_final_result": True,
                    "final_results": {
                        "text_result": text_result,
                        "visualization_data": visualization_data,
                        "file_result": file_result,
                    },
                }
                yield f"data: {json.dumps(final_event, ensure_ascii=False)}\n\n"
            finally:
                try:
                    await runner.cleanup()
                except Exception:
                    pass

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"运行元应用(FormData)时出错: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"运行元应用时出错: {str(e)}")

# ========== 能力描述翻译 & 引导式问答 ==========

@app.post("/api/agent/capability_describe", tags=["api"])
async def capability_describe(
    capabilities: str = Form(...),
    context: str = Form(default=""),
):
    """
    使用LLM将代码分析识别出的能力描述转换为业务友好的中文描述
    """
    from app.llm import LLM
    from app.schema import Message

    llm = LLM("default")

    prompt = f"""你是一个技术到业务的翻译专家。以下是从代码中自动识别出的服务能力列表。
请将每个能力的技术描述转换为普通业务人员能够理解的中文描述。

服务信息：{context}

原始能力列表：
{capabilities}

请严格以JSON数组格式返回，每个元素包含：
- "name": 原始能力名称（不要修改，用于前端关联）
- "friendlyName": 简洁的中文能力名称（2-6个字，不含任何英文或技术术语）
- "friendlyDesc": 一句话中文说明这个能力能做什么（面向完全不懂技术的业务人员）
- "friendlyInput": 用通俗中文描述需要提供什么（如"待分析的数据"）
- "friendlyOutput": 用通俗中文描述会得到什么结果（如"分析报告"）

重要：
1. 绝对不要使用任何英文单词、代码术语、变量名
2. 描述要像产品说明书一样通俗易懂
3. 只返回JSON数组，不要包含markdown代码块标记或其他内容"""

    try:
        response = await llm.ask(
            [Message.user_message(prompt)],
            stream=False,
            temperature=0.3,
        )

        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        result = json.loads(text)
        return JSONResponse(content={"success": True, "data": result})
    except json.JSONDecodeError:
        return JSONResponse(content={"success": True, "data": response})
    except Exception as e:
        logger.error(f"能力描述翻译失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/agent/capability_chat", tags=["api"])
async def capability_chat(
    capabilities: str = Form(...),
    history: str = Form(default="[]"),
    context: str = Form(default=""),
):
    """
    LLM引导式问答，帮助业务用户优化服务能力配置。
    以SSE流式返回LLM回复。
    """
    from app.llm import LLM

    llm = LLM("default")

    system_prompt = f"""你是一个友好的服务配置助手，正在帮助一位不懂技术的业务用户优化他们的服务能力配置。

当前服务信息：{context}

已识别的服务能力：
{capabilities}

你只有3轮对话机会来帮助用户优化服务能力，请严格遵循以下规则：
1. 每次只问一个问题，问题要简短
2. 优先使用选择题（给出A/B/C选项）或者是/否问题，让用户轻松作答
3. 完全不要使用任何技术术语、英文单词或代码概念
4. 用日常用语描述功能，比如"自动整理数据""生成分析报告"

对话节奏严格按以下执行：
- 第1轮：问一个最关键的问题
- 第2轮：根据用户回答，问第二个关键问题
- 第3轮：不再提问，直接给出最终优化建议总结，格式如下：

【优化建议】
✅ 建议保留：xxx、xxx（原因）
➕ 建议新增：xxx（原因）
❌ 建议移除：xxx（原因）
📝 建议调整：将"xxx"改为"xxx"（原因）"""

    try:
        chat_history = json.loads(history)
    except json.JSONDecodeError:
        chat_history = []

    user_msgs = [m for m in chat_history if m.get("role") == "user"]
    current_round = len(user_msgs) + 1

    if current_round >= 3:
        system_prompt += "\n\n【重要】这是第3轮（最后一轮），请不要再提问，直接根据之前的对话给出最终优化建议总结。"
    else:
        system_prompt += f"\n\n当前是第{current_round}轮对话。请提出你的问题。"

    messages = [{"role": "system", "content": system_prompt}]
    for msg in chat_history:
        messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})

    if not chat_history:
        messages.append({"role": "user", "content": "你好，请帮我看看这些服务能力是否合理。"})

    async def stream_generator():
        try:
            params = {
                "model": llm.model,
                "messages": messages,
                "max_tokens": llm.max_tokens,
                "temperature": 0.7,
                "stream": True,
            }
            response = await llm.client.chat.completions.create(**params)

            async for chunk in response:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    data = json.dumps({"type": "text", "content": delta}, ensure_ascii=False)
                    yield f"data: {data}\n\n"

            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"能力问答流式响应失败: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

# ============================================================================
# 算法模型想定式开发 — 自动生成代码
# ============================================================================

def _read_paper_content(file_path: str) -> str:
    """从 PDF / DOC / DOCX 文件中提取文本内容"""
    _, ext = os.path.splitext(file_path.lower())
    content = ""
    try:
        if ext == ".pdf":
            from PyPDF2 import PdfReader
            reader = PdfReader(file_path)
            for page in reader.pages:
                content += (page.extract_text() or "")
        elif ext in (".doc", ".docx"):
            from docx import Document
            doc = Document(file_path)
            for para in doc.paragraphs:
                content += para.text + "\n"
    except Exception as e:
        logger.warning(f"提取文件内容失败 ({file_path}): {e}")
    return content


@app.post("/api/aml_auto_generate/generate_code", tags=["aml_auto_generate"])
async def aml_auto_generate_code(
    model_name: str = Form(..., description="算法模型名称"),
    free_narrative: str = Form(..., description="用户自由叙述的需求"),
    industry: str = Form("", description="行业（可选）"),
    scenario: str = Form("", description="场景（可选）"),
    technology: str = Form("", description="技术方向（可选）"),
    file: UploadFile = File(None, description="想定式描述文件 (PDF/DOC/DOCX，可选)"),
):
    """
    算法模型想定式开发 — 根据用户需求自动生成算法模型代码

    接收算法模型名称、用户需求描述及可选的描述文件，
    通过 Agent 流式执行代码生成、质量分析，最终返回代码与测试结果。

    返回:
        流式 SSE 响应
    """
    workspace = Path(f"{WORKSPACE_ROOT}")
    workspace.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cleanup_files: List[str] = []
    paper_content = ""

    try:
        # ---- 1. 处理可选的描述文件上传 ----
        if file and file.filename:
            file_ext = os.path.splitext(file.filename)[1].lower()
            if file_ext not in (".pdf", ".doc", ".docx"):
                raise HTTPException(
                    status_code=400,
                    detail=f"不支持的文件类型: {file_ext}。仅支持 .pdf / .doc / .docx",
                )
            saved_path = f"{workspace}/{timestamp}_{file.filename}"
            with open(saved_path, "wb") as buf:
                shutil.copyfileobj(file.file, buf)
            cleanup_files.append(saved_path)
            paper_content = _read_paper_content(saved_path)
            logger.info(f"描述文件已保存并提取文本 ({len(paper_content)} 字符): {saved_path}")

        # ---- 2. 知识库检索 ----
        knowledge_context, references = build_knowledge_context(
            query=free_narrative,
            paper_content=paper_content,
        )
        logger.info(f"知识库检索完成，找到 {len(references)} 条参考资料")

        # ---- 3. 构建 prompt ----
        prompt = get_aml_auto_generate_prompt(
            model_name=model_name,
            free_narrative=free_narrative,
            industry=industry,
            scenario=scenario,
            technology=technology,
            paper_content=paper_content,
            knowledge_context=knowledge_context,
        )

        # ---- 4. 任务配置 ----
        task_name = "aml_auto_generate"
        task_config = {
            "prompt": prompt,
            "meta": {"model_name": model_name},
            "outputs": [
                {
                    "name": "aml_generate_result",
                    "file": str(Path(WORKSPACE_ROOT) / "temp" / "aml_generate_result.json"),
                },
            ],
            "server_config": [
                {
                    "connection_type": "stdio",
                    "server_url": None,
                    "command": None,
                    "args": None,
                    "server_id": None,
                }
            ],
        }

        agent_name = "AML Auto Generate Agent"

        # ---- 5. 流式执行 ----
        stream_generator = create_stream_generator(
            task_name, task_config, agent_name, cleanup_files
        )
        return create_streaming_response(stream_generator)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"算法模型自动生成出错: {str(e)}", exc_info=True)
        for fp in cleanup_files:
            if os.path.exists(fp):
                os.remove(fp)
        raise HTTPException(status_code=500, detail=f"处理请求时出错: {str(e)}")


# 启动应用
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010) 