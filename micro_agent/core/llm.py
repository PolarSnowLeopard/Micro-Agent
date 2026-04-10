"""LLM 调用层：基于 litellm 的统一模型接口。

通过 litellm 支持 OpenAI / DeepSeek / Claude / Ollama / vLLM 等。
model 格式：provider/model（如 deepseek/deepseek-chat、openai/gpt-4o、ollama/qwen2.5）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import litellm
from loguru import logger

from micro_agent.core.config import LLMConfig
from micro_agent.core.schema import ToolCall

litellm.suppress_debug_info = True


@dataclass
class LLMResponse:
    content: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None
    usage: Optional[dict[str, Any]] = None


class LLM:
    def __init__(self, cfg: LLMConfig):
        self.model = cfg.model
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens
        self.timeout = cfg.timeout
        self.num_retries = cfg.num_retries
        self.api_key = cfg.api_key
        self.base_url = cfg.base_url

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = "auto",
        **kwargs: Any,
    ) -> LLMResponse:
        """统一的 LLM 调用入口。所有 agent 通过此方法与模型交互。"""
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "num_retries": self.num_retries,
            **kwargs,
        }
        if self.api_key:
            params["api_key"] = self.api_key
        if self.base_url:
            params["api_base"] = self.base_url
        if tools:
            params["tools"] = tools
            params["tool_choice"] = tool_choice

        try:
            response = await litellm.acompletion(**params)
        except Exception as e:
            logger.error(f"LLM 调用失败 [{self.model}]: {e}")
            raise

        choice = response.choices[0]
        msg = choice.message

        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments or "{}",
                )
                for tc in msg.tool_calls
            ]

        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            usage=dict(response.usage) if response.usage else None,
        )
