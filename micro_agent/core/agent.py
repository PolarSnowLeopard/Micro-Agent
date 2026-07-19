"""Agent 基类：async generator 模式的核心执行引擎。

核心设计：Agent.run() 是 async generator，yield AgentEvent。
- 流式消费：async for event in agent.run(prompt): yield event.to_sse()
- 批量消费：results = [e async for e in agent.run(prompt)]
不需要额外的 Runner 类。
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Any, Optional

from loguru import logger

from micro_agent.core.llm import LLM, LLMResponse
from micro_agent.core.memory.base import MemoryProvider
from micro_agent.core.memory.short_term import ShortTermMemory
from micro_agent.core.rag.base import Retriever
from micro_agent.core.schema import AgentEvent, Message, Role
from micro_agent.tool.base import ToolResult
from micro_agent.tool.registry import ToolRegistry


class Agent:
    """通用 Agent 基类。子类（如 MCPAgent）只需扩展连接管理，不要重写 run loop。"""

    def __init__(
        self,
        *,
        name: str = "agent",
        llm: LLM,
        tools: Optional[ToolRegistry] = None,
        memory: Optional[MemoryProvider] = None,
        retriever: Optional[Retriever] = None,
        system_prompt: str = "",
        next_step_prompt: str = "",
        max_steps: int = 30,
        max_observe: int = 10000,
        terminal_tools: Optional[set[str]] = None,
        require_terminal_tool: bool = False,
        no_tool_retry_limit: int = 2,
    ):
        self.name = name
        self.llm = llm
        self.tools = tools or ToolRegistry()
        self.memory: MemoryProvider = memory if memory is not None else ShortTermMemory()
        self.retriever = retriever
        self.system_prompt = system_prompt
        self.next_step_prompt = next_step_prompt
        self.max_steps = max_steps
        self.max_observe = max_observe
        self.terminal_tools = terminal_tools or {"terminate"}
        self.require_terminal_tool = require_terminal_tool
        self.no_tool_retry_limit = max(1, no_tool_retry_limit)
        self._cancelled = False

    def cancel(self) -> None:
        """取消当前执行。下一个 step 开始前生效。"""
        self._cancelled = True

    async def run(self, request: str) -> AsyncIterator[AgentEvent]:
        """主循环。每个 step 产出 think/tool_call/tool_result 事件，最终产出 done。"""
        self.memory.add(Message.user(request))
        tool_call_signatures: set[str] = set()
        duplicate_tool_call_blocks = 0
        no_tool_response_blocks = 0

        # RAG：第一步前检索相关文档，注入 system prompt，并 yield 可见事件
        if self.retriever:
            last_user = self._last_user_content()
            if last_user:
                docs = await self.retriever.retrieve(last_user, top_k=5)
                if docs:
                    context = "\n---\n".join(
                        f"[{d.source}] {d.content}" for d in docs
                    )
                    self.system_prompt += f"\n\n### 相关参考资料\n{context}"
                    yield AgentEvent(
                        type="think", step=0,
                        data={
                            "thought": f"[RAG 知识检索] 从知识库中检索到 {len(docs)} 篇相关文档",
                            "rag_docs": [
                                {"source": d.source, "score": round(d.score, 2),
                                 "preview": d.content[:80]}
                                for d in docs
                            ],
                        },
                    )

        for step in range(1, self.max_steps + 1):
            if self._cancelled:
                yield AgentEvent(
                    type="done", step=step,
                    data={"result": "任务已取消", "reason": "cancelled"},
                )
                return
            logger.info(f"[{self.name}] step {step}/{self.max_steps}")

            # === Think ===
            try:
                response = await self._think(step)
            except Exception as e:
                logger.error(f"[{self.name}] think 阶段异常: {e}")
                yield AgentEvent(type="error", step=step, data={"error": str(e)})
                return

            if response.content:
                yield AgentEvent(
                    type="think",
                    step=step,
                    data={"thought": response.content, "usage": response.usage},
                )

            # 没有工具调用 → 任务结束
            if not response.tool_calls:
                if (
                    self.require_terminal_tool
                    and no_tool_response_blocks < self.no_tool_retry_limit
                ):
                    no_tool_response_blocks += 1
                    if response.content:
                        self.memory.add(
                            Message.assistant(content=response.content)
                        )
                    warning = (
                        "[终止工具契约] 纯文本不能完成当前任务。必须实际调用工具修改产物，"
                        "并最终调用以下终止工具之一: "
                        + ", ".join(sorted(self.terminal_tools))
                    )
                    self.memory.add(Message.user(warning))
                    yield AgentEvent(
                        type="think",
                        step=step,
                        data={
                            "thought": warning,
                            "noToolResponseBlocks": no_tool_response_blocks,
                        },
                    )
                    continue
                if response.content:
                    self.memory.add(Message.assistant(content=response.content))
                yield AgentEvent(
                    type="done",
                    step=step,
                    data={"result": response.content or ""},
                )
                return
            no_tool_response_blocks = 0

            # === Act ===
            response_signatures = [
                json.dumps(
                    {
                        "name": tool_call.name,
                        "arguments": tool_call.parse_arguments(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                for tool_call in response.tool_calls
            ]
            seen_in_response: set[str] = set()
            duplicate_indexes: set[int] = set()
            novel_signatures: set[str] = set()
            for index, signature in enumerate(response_signatures):
                if (
                    signature in tool_call_signatures
                    or signature in seen_in_response
                ):
                    duplicate_indexes.add(index)
                else:
                    seen_in_response.add(signature)
                    novel_signatures.add(signature)
            duplicated = sorted(
                {response_signatures[index] for index in duplicate_indexes}
            )
            if duplicated:
                warning = (
                    "[重复工具调用阻断] 完全相同的工具与参数已执行过，"
                    "本次不会再次执行重复项。请使用已有结果，改用不同参数，"
                    "或继续执行同一响应中的其他新工具调用。"
                )
                self.memory.add(Message.user(warning))
                yield AgentEvent(
                    type="think",
                    step=step,
                    data={
                        "thought": warning,
                        "duplicateToolCalls": duplicated,
                    },
                )
                if novel_signatures:
                    duplicate_tool_call_blocks = 0
                else:
                    duplicate_tool_call_blocks += 1
                if not novel_signatures and duplicate_tool_call_blocks >= 2:
                    yield AgentEvent(
                        type="done",
                        step=step,
                        data={
                            "result": "重复工具调用已连续阻断，结束本轮并交由外层质量循环处理。",
                            "reason": "repeated_tool_call",
                        },
                    )
                    return
                if not novel_signatures:
                    continue
            else:
                duplicate_tool_call_blocks = 0
            tool_call_signatures.update(novel_signatures)
            self.memory.add(
                Message.assistant(
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )

            should_stop = False
            for index, tc in enumerate(response.tool_calls):
                yield AgentEvent(
                    type="tool_call",
                    step=step,
                    data={"tool": tc.name, "arguments": tc.parse_arguments()},
                )

                repeated_call = index in duplicate_indexes
                tool_result = (
                    ToolResult(
                        error=(
                            "该工具与参数此前已执行；已跳过重复调用，"
                            "请复用已有结果。"
                        )
                    )
                    if repeated_call
                    else await self.tools.execute(
                        tc.name,
                        **tc.parse_arguments(),
                    )
                )

                output = str(tool_result)
                if len(output) > self.max_observe:
                    output = output[: self.max_observe] + "\n...(truncated)"

                self.memory.add(
                    Message.tool(content=output, tool_call_id=tc.id, name=tc.name)
                )

                yield AgentEvent(
                    type="tool_result",
                    step=step,
                    data={"tool": tc.name, "result": output},
                )

                if not repeated_call and self._is_terminal(tc.name):
                    done_data: dict[str, Any] = {"result": output, "tool": tc.name}
                    if tool_result.meta:
                        done_data.update(tool_result.meta)
                    yield AgentEvent(
                        type="done",
                        step=step,
                        data=done_data,
                    )
                    should_stop = True
                    break

            if should_stop:
                return

            if self._is_stuck():
                logger.warning(f"[{self.name}] 检测到重复响应，注入提示")
                self.memory.add(
                    Message.user("你似乎在重复相同的操作。请尝试不同的方法。")
                )

        # 超出最大步数
        yield AgentEvent(
            type="done",
            step=self.max_steps,
            data={"result": f"已达到最大步数限制 ({self.max_steps})", "reason": "max_steps"},
        )

    async def _think(self, step: int) -> LLMResponse:
        """构建消息列表并调用 LLM。next_step_prompt 不写入 memory。"""
        messages: list[dict] = []

        sys_prompt = self.system_prompt
        if sys_prompt:
            messages.append(Message.system(sys_prompt).to_dict())

        messages.extend(self.memory.to_list())

        if self.next_step_prompt and step > 1:
            messages.append(Message.user(self.next_step_prompt).to_dict())

        tools = self.tools.to_llm_format() if len(self.tools) > 0 else None

        return await self.llm.complete(
            messages=messages,
            tools=tools,
            tool_choice=(
                "required"
                if tools and self.require_terminal_tool
                else "auto" if tools else None
            ),
        )

    def _last_user_content(self) -> Optional[str]:
        """获取最后一条用户消息内容，用于 RAG 检索。"""
        for msg in reversed(self.memory.get_messages()):
            if msg.role == Role.USER and msg.content:
                return msg.content
        return None

    def _is_terminal(self, tool_name: str) -> bool:
        """判断工具是否应终止 agent。支持 namespace 前缀（如 server1_terminate）。"""
        normalized = tool_name.lower()
        for term in self.terminal_tools:
            t = term.lower()
            if normalized == t or normalized.endswith(f"_{t}"):
                return True
        return False

    def _is_stuck(self, threshold: int = 2) -> bool:
        """检测 LLM 是否在重复相同内容。"""
        assistant_contents = [
            m.content
            for m in self.memory.get_messages()
            if m.role == Role.ASSISTANT and m.content
        ]
        if len(assistant_contents) < threshold + 1:
            return False
        return len(set(assistant_contents[-(threshold + 1) :])) == 1

    def load_skill(self, skill_name: str) -> None:
        """加载 Skill：注入 prompt 片段 + 注册工具。"""
        from micro_agent.core.skill import SkillRegistry
        skill = SkillRegistry.get(skill_name)
        if not skill:
            logger.warning(f"Skill '{skill_name}' 未找到")
            return
        if skill.prompt_fragment:
            self.system_prompt += f"\n\n### Skill: {skill.name}\n{skill.prompt_fragment}"
        for tool in skill.tools:
            self.tools.register(tool)
        logger.info(f"[{self.name}] 已加载 Skill: {skill.name}")

    def reset(self) -> None:
        """重置 agent 状态，用于复用同一实例处理多个请求。"""
        self.memory.clear()
        self._cancelled = False
