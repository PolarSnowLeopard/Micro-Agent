import asyncio
import json
import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import WORKSPACE_ROOT
# 导入任务提示
from app.prompt.task import (
    CODE_ANALYSIS_PROMPT,
    SERVICE_PACKAGING_PROMPT,
    REMOTE_DEPLOY_PROMPT
)

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

# 新增流式处理的路由
@app.get("/stream/run/{task_name}", tags=["stream"])
async def stream_run(task_name: str):
    """
    以流式方式运行Agent
    
    参数:
        task_name: 任务名称，可选值: code_analysis, service_packaging, remote_deploy, system_info
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
        最后一个事件包含任务特定的最终结果
    """
    from run_mcp import MCPRunner
    
    # 任务配置映射：每个任务包含prompt和最终输出文件配置
    task_configs = {
        "code_analysis": {
            "prompt": CODE_ANALYSIS_PROMPT,
            "outputs": [
                {"name": "function", "file": f"{WORKSPACE_ROOT}/visualization/function.json"}
            ]
        },
        "service_packaging": {
            "prompt": SERVICE_PACKAGING_PROMPT,
            "outputs": [
                # 当前没有特定的最终输出文件，如果将来有可以在这里添加
            ]
        },
        "remote_deploy": {
            "prompt": REMOTE_DEPLOY_PROMPT,
            "outputs": [
                # 当前没有特定的最终输出文件，如果将来有可以在这里添加
            ]
        },
        "system_info": {
            "prompt": "列出你可以使用的工具，然后直接结束",
            # "prompt": "我想知道当前机器的一些信息，比如cpu、内存、磁盘、网络等",
            "outputs": [
                # 当前没有特定的最终输出文件，如果将来有可以在这里添加
                {"name": "function", "file": f"{WORKSPACE_ROOT}/visualization/function.json"}
            ]
        }
    }
    
    # 检查任务是否存在
    if task_name not in task_configs:
        raise HTTPException(status_code=400, detail=f"未知的任务名称: {task_name}")
        
    # 获取任务配置
    task_config = task_configs[task_name]
    prompt = task_config["prompt"]
    agent_name = f'{task_name.replace("_", " ").capitalize()} Agent'
    
    # 创建异步流式生成器
    async def generate_stream():
        runner = None
        full_result = []
        try:
            runner = MCPRunner(agent_name)
            await runner.initialize("stdio", None)
            
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
            yield f"data: {json.dumps({'error': error_msg, 'is_last': True})}\n\n"
        finally:
            if runner:
                await runner.cleanup()
    
    # 返回流式响应
    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # 禁用Nginx缓冲
        }
    )

# 添加演示页面路由
@app.get("/stream_demo", tags=["demo"])
async def stream_demo():
    """
    返回流式演示页面
    """
    return FileResponse("static/stream_demo.html")

# 启动应用
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003) 