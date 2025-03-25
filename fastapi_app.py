import asyncio
import json
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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

# 启动应用
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003) 