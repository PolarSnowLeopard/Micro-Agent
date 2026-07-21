#!/usr/bin/env python3
"""子 Agent CLI 入口"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import click
from config import LLMConfig, default_config
from src.sandbox.local import LocalSandbox
from src.llm.client import LLMClient
from src.tools.base import ToolRegistry
from src.tools.bash import BashTool
from src.agent.mcp_agent import MCPAgent
from src.prompts import SUB_AGENT_SYSTEM_PROMPT


@click.command()
@click.argument("task")
@click.option("--workdir", default=None, help="工作目录（默认 config.py 中 REPO2MCP_WORKSPACE_BASE_DEFAULT）")
@click.option("--max-steps", default=5, type=int, help="最大执行步数")
def run(task, workdir, max_steps):
    """执行子 Agent 任务"""
    if not sys.stdin.isatty():
        stdin_content = sys.stdin.read()
        if stdin_content:
            task = f"{task}\n\n--- 输入内容 ---\n{stdin_content}"

    default_llm = LLMConfig()
    if default_llm.sub_agent_model:
        llm_config = LLMConfig(model=default_llm.sub_agent_model)
    else:
        llm_config = default_llm

    wd = workdir or default_config.sandbox.workspace_base
    sandbox = LocalSandbox(workdir=os.path.abspath(os.path.expanduser(wd)), timeout=60)
    llm = LLMClient(llm_config)
    tools = ToolRegistry()
    tools.register(BashTool(sandbox))

    agent = MCPAgent(
        llm=llm,
        tools=tools,
        system_prompt=SUB_AGENT_SYSTEM_PROMPT,
        max_steps=max_steps,
        verbose=False,
    )

    with sandbox:
        result = agent.run(task)
        print(result)


if __name__ == "__main__":
    run()
