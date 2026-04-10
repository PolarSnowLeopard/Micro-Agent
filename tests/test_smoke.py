"""Phase 1 冒烟测试：验证 Agent + Terminate 工具能跑通完整 loop。

运行方式：
    cd Micro-Agent
    python -m tests.test_smoke

需要配置 .env 中的 LLM_API_KEY。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from micro_agent.core.agent import Agent
from micro_agent.core.config import config
from micro_agent.core.llm import LLM
from micro_agent.tool.registry import ToolRegistry
from micro_agent.tool.terminate import Terminate


async def main():
    llm = LLM(config.llm)
    tools = ToolRegistry()
    tools.register(Terminate())

    agent = Agent(
        name="smoke-test",
        llm=llm,
        tools=tools,
        system_prompt="你是一个有用的助手。完成用户请求后，使用 terminate 工具返回最终结果。",
    )

    print(f"模型: {config.llm.model}")
    print(f"工具: {tools.list_names()}")
    print("---")

    async for event in agent.run("请计算 17 * 24，然后返回结果。"):
        print(f"[{event.type}] step={event.step}")
        for k, v in event.data.items():
            if k == "usage":
                continue
            val = str(v)
            if len(val) > 200:
                val = val[:200] + "..."
            print(f"  {k}: {val}")
        print()

    print("--- 冒烟测试完成 ---")


if __name__ == "__main__":
    asyncio.run(main())
