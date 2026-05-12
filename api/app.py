"""FastAPI 应用入口。

启动方式：
    cd Micro-Agent
    uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from loguru import logger

from api.routes.task import router as task_router
from api.routes.agent import router as agent_router
from micro_agent.core.config import config
from micro_agent.core.skill import SkillRegistry

DEMO_HTML = Path(__file__).parent.parent / "demo" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    skills_dir = Path(config.workspace) / config.skills.directory
    logger.info(f"Skill 目录: {skills_dir} (exists={skills_dir.exists()})")
    count = SkillRegistry.discover(skills_dir)
    if count:
        logger.info(f"已发现 {count} 个 Skill: {SkillRegistry.list_skills()}")
        for name in SkillRegistry.list_skills():
            sk = SkillRegistry.get(name)
            if sk:
                has_prompt = bool(sk.prompt_fragment)
                match_cfg = sk.metadata.get("match", {})
                logger.info(
                    f"  Skill[{name}]: prompt={len(sk.prompt_fragment)}chars, "
                    f"match={match_cfg}"
                )
    else:
        logger.warning(f"未发现任何 Skill! 目录内容: {list(skills_dir.iterdir()) if skills_dir.exists() else '目录不存在'}")
    yield


app = FastAPI(
    title="Micro-Agent V2",
    description="IoEB 众智工场 Agent 服务",
    version="0.2.0",
    docs_url="/docs",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(task_router)
app.include_router(agent_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/demo/info")
async def demo_info():
    """返回当前框架所有组件的状态，供 demo UI 展示。"""
    knowledge_dir = Path(config.workspace) / "knowledge" / "service_packaging"
    kb_count = len(list(knowledge_dir.glob("*.md"))) if knowledge_dir.exists() else 0

    memory_dir = Path(config.workspace) / config.memory.storage_dir
    sessions = sorted(memory_dir.glob("*.json")) if memory_dir.exists() else []

    return {
        "llm_profiles": {
            name: {"model": p.model, "max_tokens": p.max_tokens}
            for name, p in config.llm_profiles.items()
        },
        "skills": SkillRegistry.list_skills(),
        "rag": {
            "embedding_model": config.rag.embedding_model,
            "chunk_size": config.rag.chunk_size,
            "knowledge_docs": kb_count,
        },
        "memory": {
            "type": "FileMemory",
            "storage_dir": config.memory.storage_dir,
            "sessions": [s.stem for s in sessions[-10:]],
        },
        "tools": ["bash", "terminate"],
        "agent": {
            "max_steps": config.agent.max_steps,
            "duplicate_threshold": config.agent.duplicate_threshold,
        },
    }


@app.get("/demo")
async def demo_page():
    return FileResponse(DEMO_HTML, media_type="text/html")
