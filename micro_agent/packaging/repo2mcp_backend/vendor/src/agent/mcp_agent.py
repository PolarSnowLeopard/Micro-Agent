"""
MCP 封装 Agent

基于 ReAct 架构，通过 bash 工具完成代码分析、生成和修复任务。
支持子 Agent 委托机制避免上下文膨胀。
内置上下文管理：工具输出截断 + 滑动窗口压缩。
"""
import json
from typing import Optional, List, Dict, Callable

from src.agent.base import BaseAgent
from src.llm.client import LLMClient, LLMResponse
from src.tools.base import ToolRegistry
from src.logger import get_logger

logger = get_logger(__name__)

MAX_NUDGES = 3
DEFAULT_TOOL_OUTPUT_LIMIT = 2000
DEFAULT_CONTEXT_WINDOW = 6


class MCPAgent(BaseAgent):
    """
    MCP 封装 Agent

    简化的 ReAct 循环，专注于 MCP 服务封装任务。
    每个阶段创建一个新实例，保持上下文干净。

    上下文管理策略：
    - 工具输出截断：单次 tool result 超过 tool_output_limit 字符时保留头尾
    - 滑动窗口：超出 context_window 的旧轮次中 tool result 替换为摘要标记
    """

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        system_prompt: Optional[str] = None,
        max_steps: int = 30,
        verbose: bool = True,
        completion_check: Optional[Callable[[], bool]] = None,
        completion_nudge: Optional[str] = None,
        tool_output_limit: int = DEFAULT_TOOL_OUTPUT_LIMIT,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        force_completion_after: Optional[int] = None,
        compact_initial_task_after: Optional[int] = None,
    ):
        super().__init__(llm=llm, tools=tools, system_prompt=system_prompt)
        self.max_steps = max_steps
        self.verbose = verbose
        self.completion_check = completion_check
        self.completion_nudge = completion_nudge or (
            "你还没有完成任务的关键步骤——必须用 bash 工具将结果文件写入磁盘。"
            "请立即调用 bash 工具执行写入操作，不要只在文字中描述。"
        )
        self.tool_output_limit = tool_output_limit
        self.context_window = context_window
        self.force_completion_after = force_completion_after
        self.compact_initial_task_after = compact_initial_task_after
        self._initial_task_compacted = False
        self.messages: List[Dict] = [{"role": "system", "content": self.system_prompt}]
        self._consecutive_empty_calls = 0

    @staticmethod
    def _truncate_output(text: str, limit: int) -> str:
        """截断过长的工具输出，保留头尾"""
        if not text or len(text) <= limit:
            return text
        keep = limit // 2
        return (
            text[:keep]
            + f"\n\n... [截断: 原始输出 {len(text)} 字符, 已省略中间 {len(text) - limit} 字符] ...\n\n"
            + text[-keep:]
        )

    def _compress_old_turns(self):
        """滑动窗口压缩：将超出窗口的旧轮次中的 tool result 替换为简短标记。

        保留策略：
        - messages[0] (system prompt) 始终保留
        - messages[1] (user task) 始终保留
        - 最近 context_window 轮的 tool result 保留完整内容
        - 更早轮次的 tool result 替换为 "[历史工具输出已压缩]"
        - 所有 assistant reasoning 始终保留（这是 agent 的思考结论）
        """
        tool_msg_indices = [
            i for i, m in enumerate(self.messages) if m.get("role") == "tool"
        ]
        if len(tool_msg_indices) <= self.context_window:
            return

        cutoff = len(tool_msg_indices) - self.context_window
        for idx in tool_msg_indices[:cutoff]:
            original = self.messages[idx].get("content", "")
            if original and not original.startswith("[历史工具输出已压缩]"):
                self.messages[idx]["content"] = "[历史工具输出已压缩]"

    def run(self, task: str) -> str:
        """执行任务，返回最终回复"""
        self.messages.append({"role": "user", "content": task})

        if self.verbose:
            print(f"\n{'─'*60}")
            print(f"🤖 Agent 开始执行 (max_steps={self.max_steps})")
            print(f"{'─'*60}")

        nudge_count = 0
        consecutive_empty_responses = 0

        for step in range(self.max_steps):
            if (
                self.force_completion_after is not None
                and step >= self.force_completion_after
                and self.completion_check
                and not self.completion_check()
            ):
                if self.verbose:
                    print(
                        "\n  ⚠️ 探索预算已用完，转入确定性的结构化产物编译"
                    )
                return ""
            logger.debug(f"Step {step + 1}/{self.max_steps}")

            self._compress_old_turns()

            try:
                response = self.llm.chat(
                    messages=self.messages,
                    tools=self.tools.list_schemas() if len(self.tools) > 0 else None
                )
            except Exception as e:
                logger.error(f"LLM request failed: {e}")
                return f"LLM 调用失败: {e}"

            if response.has_tool_calls:
                consecutive_empty_responses = 0
                self._handle_tool_calls(response, step)
                self._compact_initial_task(step + 1)

                if self.completion_check and self.completion_check():
                    if self.verbose:
                        print(f"\n✅ Agent 完成条件已满足 (step {step + 1})")
                    return "completion artifact written"

                if (
                    self.force_completion_after is not None
                    and step + 1 >= self.force_completion_after
                    and self.completion_check
                    and not self.completion_check()
                ):
                    if self.verbose:
                        print(
                            "\n  ⚠️ 探索预算已用完，转入确定性的结构化产物编译"
                        )
                    return ""

                if self._consecutive_empty_calls >= 3:
                    self._consecutive_empty_calls = 0
                    if self.verbose:
                        print(f"\n  ⚠️ 检测到连续空工具调用，注入修正提示")
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "你连续发送了空的工具调用。请停止发送空命令。"
                            "如果你已经完成了分析，请直接用 bash 工具将结果写入文件。"
                            "如果你需要执行命令，请在 command 参数中提供具体的命令字符串。"
                        ),
                    })
            else:
                if response.content:
                    consecutive_empty_responses = 0
                    self.messages.append({
                        "role": "assistant",
                        "content": response.content
                    })

                    if (
                        self.force_completion_after is not None
                        and step + 1 >= self.force_completion_after
                        and self.completion_check
                        and not self.completion_check()
                    ):
                        if self.verbose:
                            print(
                                "\n  ⚠️ 探索预算已用完，转入确定性的结构化产物编译"
                            )
                        return ""

                    if self.completion_check and nudge_count < MAX_NUDGES and not self.completion_check():
                        nudge_count += 1
                        if self.verbose:
                            print(f"\n  ⚠️ 完成条件未满足，注入提醒 ({nudge_count}/{MAX_NUDGES})")
                        self.messages.append({
                            "role": "user",
                            "content": self.completion_nudge,
                        })
                        continue

                    if self.verbose:
                        preview = response.content[:200]
                        print(f"\n✅ Agent 完成 (step {step + 1}): {preview}...")
                    return response.content

                consecutive_empty_responses += 1
                logger.warning(f"Empty response at step {step + 1}")
                if consecutive_empty_responses >= 2:
                    logger.warning("Stopping agent after two consecutive empty responses")
                    return ""

        # 达到最大步数
        if self.verbose:
            print(f"\n⚠️ 达到最大步数 ({self.max_steps})")
        return self._get_final_response()

    def _compact_initial_task(self, completed_steps: int) -> None:
        """Drop the repeated DARP body after the Agent has selected evidence."""
        if (
            self._initial_task_compacted
            or self.compact_initial_task_after is None
            or completed_steps < self.compact_initial_task_after
            or len(self.messages) < 2
        ):
            return
        message = self.messages[1]
        content = message.get("content", "") if message.get("role") == "user" else ""
        if len(content) <= 12_000:
            return
        omitted = len(content) - 8_000
        message["content"] = (
            content[:6_000]
            + f"\n\n[初始 DARP/BAGE 摘要已在首轮审阅，压缩 {omitted} 字符；"
            "后续以已选择文件和 code_explorer 证据为准。]\n\n"
            + content[-2_000:]
        )
        self._initial_task_compacted = True

    def _handle_tool_calls(self, response: LLMResponse, step: int):
        """处理工具调用"""
        # 添加 assistant 消息（含 tool_calls）
        assistant_message = {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": (
                            json.dumps(tc.arguments)
                            if isinstance(tc.arguments, dict)
                            else str(tc.arguments)
                        )
                    }
                }
                for tc in response.tool_calls
            ]
        }
        self.messages.append(assistant_message)

        # 执行每个工具调用
        for tc in response.tool_calls:
            is_empty_call = (
                tc.name == "bash"
                and not tc.arguments.get("command", "").strip()
            )
            if is_empty_call:
                self._consecutive_empty_calls += 1
                if self.verbose:
                    print(f"  🔧 [{step + 1}] bash: (空调用, 跳过执行)")
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": (
                        "ERROR: Empty bash command. You sent an empty 'command' argument. "
                        "This is likely a formatting error. Please provide the actual command "
                        "string you want to execute, or if you are done with exploration, "
                        "write the result file using: cat > FILE_PATH << 'EOF'\n...content...\nEOF"
                    ),
                })
                continue

            self._consecutive_empty_calls = 0

            if self.verbose:
                cmd = tc.arguments.get("command", str(tc.arguments))
                cmd_display = cmd[:120] + "..." if len(cmd) > 120 else cmd
                print(f"  🔧 [{step + 1}] {tc.name}: {cmd_display}")

            result = self.tools.execute(tc.name, **tc.arguments)

            if self.verbose:
                status = "✓" if result.success else "✗"
                output_preview = result.output[:100] if result.output else "(empty)"
                print(f"     {status} {output_preview}")

            truncated_content = self._truncate_output(
                result.to_message(), self.tool_output_limit
            )
            self.messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": truncated_content
            })

    def _get_final_response(self) -> str:
        """达到最大步数时获取总结"""
        self.messages.append({
            "role": "user",
            "content": "你已达到执行步数上限。请返回当前已完成的工作总结。"
        })
        try:
            response = self.llm.chat(messages=self.messages)
            return response.content or "任务未完成 - 已达到最大步数"
        except Exception as e:
            return f"任务执行中断: {e}"

    def evidence_digest(self, max_chars: int = 16_000) -> str:
        """Return recent concrete tool evidence without replaying the full dialogue."""
        evidence: list[str] = []
        for message in self.messages:
            if message.get("role") != "tool":
                continue
            content = str(message.get("content", "")).strip()
            if not content or content.startswith("[历史工具输出已压缩]"):
                continue
            evidence.append(content)
        digest = "\n\n--- tool evidence ---\n".join(evidence[-8:])
        if len(digest) <= max_chars:
            return digest
        head = max_chars * 3 // 4
        tail = max_chars - head
        return digest[:head] + "\n\n[older evidence truncated]\n\n" + digest[-tail:]
