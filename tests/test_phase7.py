"""Phase 7 测试：EmbeddingRetriever、Session Memory、Skill 发现、build_agent 增强。"""

import json
from pathlib import Path

import pytest

from core.schema import Message, Role

# ── EmbeddingRetriever 基础 ──

def test_embedding_retriever_import():
    from core.rag.embedding import EmbeddingRetriever
    r = EmbeddingRetriever(model="test-model")
    assert r.model == "test-model"
    assert r._docs == []


def test_embedding_retriever_split_text():
    from core.rag.embedding import EmbeddingRetriever
    r = EmbeddingRetriever(chunk_size=50, chunk_overlap=10)
    chunks = r._split_text(
        "第一段内容比较短。\n\n第二段内容也很短。\n\n第三段内容同样很短。",
        source="test.md",
        metadata={"tag": "test"},
    )
    assert len(chunks) >= 1
    assert all(c.source == "test.md" for c in chunks)


# ── Config 新字段 ──

def test_config_memory_field():
    from core.config import config
    assert hasattr(config, "memory")
    assert hasattr(config.memory, "storage_dir")


def test_config_rag_field():
    from core.config import config
    assert hasattr(config, "rag")
    assert config.rag.embedding_model == "openrouter/openai/text-embedding-3-small"


def test_config_skills_field():
    from core.config import config
    assert hasattr(config, "skills")
    assert config.skills.directory == "skills"


# ── Skill 发现 ──

def test_skill_discover_from_workspace(tmp_path):
    from core.skill import SkillRegistry, Skill
    SkillRegistry.clear()

    skill_dir = tmp_path / "test_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("你是测试专家。请仔细测试代码。")

    count = SkillRegistry.discover(tmp_path)
    assert count == 1
    skill = SkillRegistry.get("test_skill")
    assert skill is not None
    assert "测试专家" in skill.prompt_fragment
    SkillRegistry.clear()


def test_skill_discover_real_skills():
    from core.skill import SkillRegistry
    from core.config import config
    SkillRegistry.clear()

    skills_dir = Path(config.workspace) / config.skills.directory
    count = SkillRegistry.discover(skills_dir)
    assert count == 4
    assert "mcp_protocol" in SkillRegistry.list_skills()
    assert "docker_packaging" in SkillRegistry.list_skills()
    assert "code_analysis_patterns" in SkillRegistry.list_skills()
    assert "algorithm_code_standards" in SkillRegistry.list_skills()
    SkillRegistry.clear()


# ── Session Memory（FileMemory） ──

async def test_file_memory_persist_and_load(tmp_path):
    from core.memory.persistent import FileMemory
    from core.schema import Message

    mem = FileMemory(tmp_path)
    await mem.load("sess_001")
    assert len(mem) == 0

    mem.add(Message.user("你好"))
    mem.add(Message.assistant("你好！有什么可以帮你？"))
    await mem.persist()

    mem2 = FileMemory(tmp_path)
    await mem2.load("sess_001")
    assert len(mem2) == 2
    msgs = mem2.get_messages()
    assert msgs[0].content == "你好"
    assert msgs[1].content == "你好！有什么可以帮你？"


# ── build_agent 增强 ──

async def test_build_agent_with_session(tmp_path, monkeypatch):
    from api import deps
    monkeypatch.setattr(deps, "MEMORY_DIR", tmp_path)

    from api.deps import build_agent
    agent, sid = await build_agent(name="test", enable_session=True)
    assert sid is not None
    assert len(sid) == 12

    from core.memory.persistent import FileMemory
    assert isinstance(agent.memory, FileMemory)


async def test_build_agent_with_existing_session(tmp_path, monkeypatch):
    from core.memory.persistent import FileMemory
    from core.schema import Message

    mem = FileMemory(tmp_path)
    await mem.load("existing_session")
    mem.add(Message.user("历史消息"))
    await mem.persist()

    from api import deps
    monkeypatch.setattr(deps, "MEMORY_DIR", tmp_path)

    from api.deps import build_agent
    agent, sid = await build_agent(name="test", enable_session=True, session_id="existing_session")
    assert sid == "existing_session"
    assert len(agent.memory) == 1


async def test_build_agent_without_session():
    from api.deps import build_agent
    agent, sid = await build_agent(name="test")
    assert sid is None

    from core.memory.short_term import ShortTermMemory
    assert isinstance(agent.memory, ShortTermMemory)


async def test_build_agent_with_skills(tmp_path):
    from core.skill import SkillRegistry, Skill
    SkillRegistry.clear()
    SkillRegistry.register(Skill(
        name="test_skill",
        prompt_fragment="你必须遵循测试规范。",
    ))

    from api.deps import build_agent
    agent, _ = await build_agent(name="test", skills=["test_skill"])
    assert "测试规范" in agent.system_prompt
    SkillRegistry.clear()


# ── TaskManager memory persist ──

async def test_task_manager_persists_memory(tmp_path):
    from unittest.mock import AsyncMock, MagicMock
    from core.task import TaskManager
    from core.agent import Agent
    from core.memory.persistent import FileMemory
    from core.schema import AgentEvent

    mem = FileMemory(tmp_path)
    await mem.load("persist_test")

    agent = MagicMock(spec=Agent)
    agent.memory = mem
    agent.name = "test"

    async def fake_run(request):
        mem.add(Message(role=Role.USER, content=request))
        mem.add(Message(role=Role.ASSISTANT, content="done"))
        yield AgentEvent(type="done", step=1, data={"result": "done"})

    agent.run = fake_run

    mgr = TaskManager()
    ctx = await mgr.submit(agent, "测试持久化")

    import asyncio
    for _ in range(50):
        if ctx.status != "running":
            break
        await asyncio.sleep(0.1)

    mem2 = FileMemory(tmp_path)
    await mem2.load("persist_test")
    assert len(mem2) >= 1


# ── Knowledge directory existence ──

def test_knowledge_directory_exists():
    from core.config import config
    knowledge_dir = Path(config.workspace) / "knowledge" / "service_packaging"
    assert knowledge_dir.exists()
    md_files = list(knowledge_dir.glob("*.md"))
    assert len(md_files) >= 4


# ── SSE response session header ──

def test_sse_response_has_session_id_param():
    import inspect
    from api.services.sse import sse_response
    sig = inspect.signature(sse_response)
    assert "session_id" in sig.parameters


# ── App endpoints include new ones ──

def test_app_has_all_endpoints():
    from api.app import app
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    expected = [
        "/api/agent/code_analysis",
        "/api/agent/service_packaging",
        "/api/agent/mcp_test",
        "/api/agent/service_evaluation",
        "/api/agent/mcp_service_recommendation",
        "/api/agent/meta_app_validation",
        "/api/agent/aml_report",
        "/api/agent/aml_model_evaluation",
        "/api/agent/meta_app/run",
        "/api/agent/capability_describe",
        "/api/agent/capability_chat",
    ]
    for ep in expected:
        assert ep in paths, f"缺少端点: {ep}"
