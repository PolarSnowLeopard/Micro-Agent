"""Phase 5 测试：多 LLM profile、Memory、Skill、RAG。"""

import json
import tempfile
from pathlib import Path

import pytest

from micro_agent.core.config import AppConfig, LLMConfig


# === 1. 多 LLM Profile ===

def test_multi_llm_profile_from_toml():
    """新格式 TOML: [llm.default] + [llm.fast]。"""
    cfg = AppConfig.load()
    assert "default" in cfg.llm_profiles
    assert cfg.llm.model == cfg.llm_profiles["default"].model
    assert cfg.get_llm("default") is cfg.llm_profiles["default"]


def test_get_llm_fallback():
    """不存在的 profile 回退到 default。"""
    cfg = AppConfig.load()
    assert cfg.get_llm("nonexistent") is cfg.llm


def test_llm_profiles_multiple():
    """确保多 profile 都被加载。"""
    cfg = AppConfig.load()
    assert "fast" in cfg.llm_profiles
    assert "reasoning" in cfg.llm_profiles
    assert cfg.llm_profiles["fast"].max_tokens == 4096
    reasoning = cfg.llm_profiles["reasoning"]
    assert reasoning.model == "openrouter/qwen/qwen3.6-flash"
    assert reasoning.base_url == "https://openrouter.ai/api/v1"
    assert reasoning.reasoning_enabled is False


def test_llm_reasoning_env_override(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[llm]\nmodel = "legacy/model"\nbase_url = "http://legacy.invalid/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_MODEL", "openrouter/qwen/qwen3.6-flash")
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("LLM_REASONING_ENABLED", "false")

    cfg = AppConfig.load(config_path)

    assert cfg.llm.model == "openrouter/qwen/qwen3.6-flash"
    assert cfg.llm.base_url == "https://openrouter.ai/api/v1"
    assert cfg.llm.reasoning_enabled is False


async def test_build_agent_with_profile():
    from api.deps import build_agent
    agent, _ = await build_agent(name="test", llm_profile="fast")
    assert agent.llm.max_tokens == 4096


def test_task_config_llm_profile():
    from micro_agent.task.base import TaskConfig
    tc = TaskConfig(name="test", llm_profile="reasoning")
    assert tc.llm_profile == "reasoning"


# === 2. Memory ===

def test_short_term_memory():
    from micro_agent.core.memory import ShortTermMemory
    from micro_agent.core.schema import Message

    mem = ShortTermMemory(max_messages=5)
    for i in range(8):
        mem.add(Message.user(f"msg {i}"))
    assert len(mem) == 5
    assert mem.get_messages()[-1].content == "msg 7"


def test_short_term_preserves_system():
    from micro_agent.core.memory import ShortTermMemory
    from micro_agent.core.schema import Message

    mem = ShortTermMemory(max_messages=3)
    mem.add(Message.system("system"))
    for i in range(5):
        mem.add(Message.user(f"msg {i}"))
    assert len(mem) == 3
    assert mem.get_messages()[0].content == "system"


@pytest.mark.asyncio
async def test_file_memory_persist_load():
    from micro_agent.core.memory import FileMemory
    from micro_agent.core.schema import Message

    with tempfile.TemporaryDirectory() as tmpdir:
        mem = FileMemory(Path(tmpdir))
        await mem.load("session_1")
        mem.add(Message.user("你好"))
        mem.add(Message.assistant(content="你好！"))
        await mem.persist()

        # 加载到新实例
        mem2 = FileMemory(Path(tmpdir))
        await mem2.load("session_1")
        assert len(mem2) == 2
        assert mem2.get_messages()[0].content == "你好"


def test_agent_accepts_memory():
    from micro_agent.core.config import config
    from micro_agent.core.llm import LLM
    from micro_agent.core.agent import Agent
    from micro_agent.core.memory import ShortTermMemory

    llm = LLM(config.llm)
    mem = ShortTermMemory(max_messages=10)
    agent = Agent(llm=llm, memory=mem)
    assert agent.memory is mem


def test_memory_provider_interface():
    from micro_agent.core.memory.base import MemoryProvider
    from micro_agent.core.memory import ShortTermMemory, FileMemory
    assert issubclass(ShortTermMemory, MemoryProvider)
    assert issubclass(FileMemory, MemoryProvider)


# === 3. Skill ===

def test_skill_register_and_get():
    from micro_agent.core.skill import Skill, SkillRegistry

    SkillRegistry.clear()
    skill = Skill(name="test_skill", description="测试", prompt_fragment="请遵循测试规范")
    SkillRegistry.register(skill)
    assert SkillRegistry.get("test_skill") is skill
    assert "test_skill" in SkillRegistry.list_skills()
    SkillRegistry.clear()


def test_skill_from_directory():
    from micro_agent.core.skill import Skill

    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir) / "my_skill"
        p.mkdir()
        (p / "SKILL.md").write_text("你是代码审查专家。\n请检查以下规范...")
        skill = Skill.from_directory(p)
        assert skill is not None
        assert skill.name == "my_skill"
        assert "代码审查" in skill.prompt_fragment


def test_skill_discover():
    from micro_agent.core.skill import Skill, SkillRegistry

    SkillRegistry.clear()
    with tempfile.TemporaryDirectory() as tmpdir:
        for name in ["skill_a", "skill_b"]:
            p = Path(tmpdir) / name
            p.mkdir()
            (p / "SKILL.md").write_text(f"这是 {name}")
        count = SkillRegistry.discover(Path(tmpdir))
        assert count == 2
        assert "skill_a" in SkillRegistry.list_skills()
    SkillRegistry.clear()


def test_agent_load_skill():
    from micro_agent.core.config import config
    from micro_agent.core.llm import LLM
    from micro_agent.core.agent import Agent
    from micro_agent.core.skill import Skill, SkillRegistry

    SkillRegistry.clear()
    SkillRegistry.register(Skill(
        name="code_review",
        prompt_fragment="请严格遵循 PEP8 规范。",
    ))

    llm = LLM(config.llm)
    agent = Agent(llm=llm, system_prompt="你是助手。")
    agent.load_skill("code_review")
    assert "PEP8" in agent.system_prompt
    SkillRegistry.clear()


# === 4. RAG ===

@pytest.mark.asyncio
async def test_simple_retriever():
    from micro_agent.core.rag import SimpleRetriever

    retriever = SimpleRetriever()
    await retriever.add("Python 的 GIL 限制了多线程并行", source="python_faq.md")
    await retriever.add("Flask 是一个轻量级 Web 框架", source="flask_intro.md")
    await retriever.add("React 是一个前端 JavaScript 库", source="react_intro.md")

    docs = await retriever.retrieve("Python 多线程")
    assert len(docs) > 0
    assert docs[0].source == "python_faq.md"


@pytest.mark.asyncio
async def test_retriever_empty():
    from micro_agent.core.rag import SimpleRetriever

    retriever = SimpleRetriever()
    docs = await retriever.retrieve("不存在的内容")
    assert docs == []


def test_agent_accepts_retriever():
    from micro_agent.core.config import config
    from micro_agent.core.llm import LLM
    from micro_agent.core.agent import Agent
    from micro_agent.core.rag import SimpleRetriever

    llm = LLM(config.llm)
    retriever = SimpleRetriever()
    agent = Agent(llm=llm, retriever=retriever)
    assert agent.retriever is retriever


def test_retriever_interface():
    from micro_agent.core.rag.base import Retriever
    from micro_agent.core.rag import SimpleRetriever
    assert issubclass(SimpleRetriever, Retriever)
