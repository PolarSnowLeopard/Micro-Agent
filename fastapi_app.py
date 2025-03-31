import asyncio
import json
import os
from pathlib import Path
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 导入你的模块
from app.prompt.task import (
    CODE_ANALYSIS_PROMPT,
    SERVICE_PACKAGING_PROMPT,
    REMOTE_DEPLOY_PROMPT
)
from main import run_agent

# 创建FastAPI应用
app = FastAPI(
    title="代码解析-微服务封装-远程部署 Agent调用",
    description="run ioeb agent",
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

# 响应模型
class AgentResponse(BaseModel):
    result: Dict[str, Any]

class ErrorResponse(BaseModel):
    error: str

# 路由实现
@app.get("/agent/code_analysis", response_model=AgentResponse, tags=["agent"])
async def code_analysis():
    """代码分析接口"""
    task_name = "code_analysis"
    prompt = CODE_ANALYSIS_PROMPT
    
    try:
        # 直接调用异步函数，无需额外线程
        await run_agent(task_name, prompt)
        
        # 读取结果
        function_path = Path('visualization/function.json')
        record_path = Path(f'visualization/{task_name}_record.json')
        
        if not function_path.exists() or not record_path.exists():
            raise HTTPException(status_code=500, detail="结果文件不存在")
            
        with open(function_path, 'r', encoding='utf-8') as f:
            function = f.read()
        with open(record_path, 'r', encoding='utf-8') as f:
            record = f.read()
            
        return {"result": {"function": function, "record": record}}
    
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="任务执行超时")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"执行失败: {str(e)}")

@app.get("/agent/service_packaging", response_model=AgentResponse, tags=["agent"])
async def service_packaging():
    """服务封装接口"""
    task_name = "service_packaging"
    prompt = SERVICE_PACKAGING_PROMPT
    
    try:
        # 直接调用异步函数
        await run_agent(task_name, prompt)
        
        # 读取结果
        record_path = Path(f'visualization/{task_name}_record.json')
        
        if not record_path.exists():
            raise HTTPException(status_code=500, detail="结果文件不存在")
            
        with open(record_path, 'r', encoding='utf-8') as f:
            record = f.read()
            
        return {"result": {"record": record}}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"执行失败: {str(e)}")

@app.get("/agent/remote_deploy", response_model=AgentResponse, tags=["agent"])
async def remote_deploy():
    """远程部署接口"""
    task_name = "remote_deploy"
    prompt = REMOTE_DEPLOY_PROMPT
    
    try:
        # 直接调用异步函数
        await run_agent(task_name, prompt)
        
        # 读取结果
        record_path = Path(f'visualization/{task_name}_record.json')
        
        if not record_path.exists():
            raise HTTPException(status_code=500, detail="结果文件不存在")
            
        with open(record_path, 'r', encoding='utf-8') as f:
            record = f.read()
            
        return {"result": {"record": record}}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"执行失败: {str(e)}")

# 新增流式处理的路由
@app.get("/stream/run/{task_name}", tags=["stream"])
async def stream_run(task_name: str):
    """
    以流式方式运行Agent
    
    参数:
        task_name: 任务名称，可选值: code_analysis, service_packaging, remote_deploy, system_info
    
    返回:
        流式SSE响应，每个step完成后返回一个事件
    """
    from run_mcp import MCPRunner
    from app.prompt.task import (
        CODE_ANALYSIS_PROMPT,
        SERVICE_PACKAGING_PROMPT,
        REMOTE_DEPLOY_PROMPT
    )
    
    # 根据任务名称选择prompt
    prompts = {
        "code_analysis": CODE_ANALYSIS_PROMPT,
        "service_packaging": SERVICE_PACKAGING_PROMPT,
        "remote_deploy": REMOTE_DEPLOY_PROMPT,
        "system_info": "我想知道当前机器的一些信息，比如cpu、内存、磁盘、网络等"
    }
    
    if task_name not in prompts:
        raise HTTPException(status_code=400, detail=f"未知的任务名称: {task_name}")
        
    prompt = prompts[task_name]
    agent_name = f'{task_name.replace("_", " ").capitalize()} Agent'
    
    # 创建异步流式生成器
    async def generate_stream():
        runner = None
        try:
            runner = MCPRunner(agent_name)
            await runner.initialize("stdio", None)
            
            # 运行流式Agent
            async for step_result in runner.run_stream(prompt):
                # 将结果转为SSE格式
                json_result = json.dumps(step_result, ensure_ascii=False)
                yield f"data: {json_result}\n\n"
                
                # 如果是最后一个结果，保存完整记录
                if step_result.get("is_last", False):
                    # 获取完整结果但不重新执行
                    full_result = await runner.agent.run(None) 
                    full_json = json.dumps(full_result, ensure_ascii=False)
                    # 保存完整记录到文件
                    from app.utils.visualize_record import save_record_to_json, generate_visualization_html
                    save_record_to_json(task_name, full_json)
                    generate_visualization_html(task_name)
                
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