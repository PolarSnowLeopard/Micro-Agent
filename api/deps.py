"""API 层公共依赖：TaskManager 单例、Agent 构建工厂。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from loguru import logger

from core.config import config
from core.llm import LLM
from core.agent import Agent
from core.mcp_agent import MCPAgent
from core.memory.persistent import FileMemory
from core.rag.base import Retriever
from core.skill import SkillRegistry
from core.task import TaskManager
from tool.registry import ToolRegistry
from tool.terminate import Terminate
from tool.bash import Bash

task_manager = TaskManager()

MEMORY_DIR = Path(config.workspace) / config.memory.storage_dir


async def build_agent(
    name: str = "agent",
    system_prompt: str = "",
    next_step_prompt: str = "",
    max_steps: Optional[int] = None,
    use_mcp: bool = False,
    llm_profile: str = "default",
    enable_session: bool = False,
    session_id: Optional[str] = None,
    skills: Optional[list[str]] = None,
    retriever: Optional[Retriever] = None,
) -> tuple[Agent, Optional[str]]:
    """构建 Agent 实例。返回 (agent, session_id)。

    参数:
        enable_session: 是否启用会话记忆。True 时创建 FileMemory。
        session_id: 续接已有会话。为 None 且 enable_session=True 时自动生成。
        skills: Skill 名称列表，加载后注入 prompt 和工具。
        retriever: 向量检索器，Agent 第一步自动检索相关文档。
    """
    llm = LLM(config.get_llm(llm_profile))
    tools = ToolRegistry()
    tools.register(Terminate())
    tools.register(Bash())

    memory = None
    resolved_session_id = None
    if enable_session:
        resolved_session_id = session_id or uuid.uuid4().hex[:12]
        memory = FileMemory(MEMORY_DIR)
        await memory.load(resolved_session_id)
        logger.info(f"会话记忆: {resolved_session_id} ({len(memory)} 条历史)")

    cls = MCPAgent if use_mcp else Agent
    agent = cls(
        name=name,
        llm=llm,
        tools=tools,
        memory=memory,
        retriever=retriever,
        system_prompt=system_prompt,
        next_step_prompt=next_step_prompt,
        max_steps=max_steps or config.agent.max_steps,
    )

    if skills:
        for skill_name in skills:
            agent.load_skill(skill_name)

    return agent, resolved_session_id
