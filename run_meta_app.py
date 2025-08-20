#!/usr/bin/env python
import asyncio
import json

from app.agent.meta_app import MetaAppAgent
from app.logger import logger
from app.schema import AgentState


class MetaAppRunner:
    """Runner for MetaAppAgent with streaming support."""

    def __init__(self, agent_name: str = "Meta App Agent"):
        self.agent = MetaAppAgent(name=agent_name)

    async def initialize(self, meta_config: dict, use_sim_only: bool = True) -> None:
        await self.agent.initialize_from_config(meta_config, use_sim_only=use_sim_only)

    async def run_stream(self, prompt: str):
        """Stream step-by-step execution results from the agent."""
        try:
            # Initialize running state
            self.agent.current_step = 0
            self.agent.state = AgentState.IDLE

            if prompt:
                self.agent.update_memory("user", prompt)

            async with self.agent.state_context(AgentState.RUNNING):
                while (
                    self.agent.current_step < self.agent.max_steps
                    and self.agent.state != AgentState.FINISHED
                ):
                    self.agent.current_step += 1
                    logger.info(
                        f"执行步骤 {self.agent.current_step}/{self.agent.max_steps}"
                    )

                    step_result = await self.agent.step()
                    step_result["step"] = self.agent.current_step

                    if self.agent.is_stuck():
                        self.agent.handle_stuck_state()

                    yield step_result

                # 不在此处发送最终事件，交由上层API统一发送带 final_results 的最后消息

        except Exception as e:
            logger.exception(f"运行MetaAppAgent时出错: {str(e)}")
            yield {"error": f"运行出错: {str(e)}", "is_last": True}

    async def cleanup(self) -> None:
        try:
            logger.info("正在清理MetaAppAgent资源")

            async def detached_cleanup():
                try:
                    if hasattr(self.agent, "cleanup"):
                        await self.agent.cleanup()
                except Exception as e:
                    logger.error(f"代理清理过程中出错: {str(e)}")

            asyncio.create_task(detached_cleanup())
            logger.info("清理过程已在后台启动")
        except Exception as e:
            logger.error(f"启动清理过程时出错: {str(e)}")


