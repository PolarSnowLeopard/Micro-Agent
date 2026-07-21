"""LLM 客户端 - 基于 LiteLLM"""
import json
import re
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

import litellm
from litellm import completion

from src.logger import get_logger
from config import LLMConfig

logger = get_logger(__name__)


@dataclass
class ToolCall:
    """工具调用"""
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    """LLM 响应"""
    content: Optional[str]
    tool_calls: Optional[List[ToolCall]]
    finish_reason: str
    thinking: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0

    @property
    def has_tool_calls(self) -> bool:
        return self.tool_calls is not None and len(self.tool_calls) > 0

    @property
    def has_thinking(self) -> bool:
        return self.thinking is not None and len(self.thinking) > 0


class LLMClient:
    """LLM 客户端，基于 LiteLLM 支持多种模型提供商"""

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()
        if self.config.api_key:
            if "openrouter" in self.config.model:
                litellm.openrouter_api_key = self.config.api_key
            else:
                litellm.api_key = self.config.api_key
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cost = 0.0
        self.call_count = 0

    def chat(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        kwargs = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
        }
        if self.config.api_key:
            kwargs["api_key"] = self.config.api_key
        if self.config.api_base:
            kwargs["api_base"] = self.config.api_base
        if self.config.reasoning_enabled is not None:
            kwargs["reasoning"] = {"enabled": self.config.reasoning_enabled}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_format:
            kwargs["response_format"] = response_format

        logger.debug(f"LLM request: model={self.config.model}, messages={len(messages)}")

        for attempt in range(self.config.max_retries + 1):
            try:
                response = completion(**kwargs)
                parsed = self._parse_response(response)
                self.total_prompt_tokens += parsed.prompt_tokens
                self.total_completion_tokens += parsed.completion_tokens
                self.total_cost += parsed.cost
                self.call_count += 1
                return parsed
            except Exception as e:
                if attempt >= self.config.max_retries or not self._is_retryable(e):
                    logger.error(f"LLM request failed: {e}")
                    raise
                delay = min(
                    self.config.retry_base_seconds * (2 ** attempt),
                    self.config.retry_max_seconds,
                )
                logger.warning(
                    "Transient LLM failure (%s/%s): %s; retrying in %.1fs",
                    attempt + 1,
                    self.config.max_retries,
                    e,
                    delay,
                )
                time.sleep(delay)

        raise RuntimeError("unreachable LLM retry state")

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """Return whether a provider failure is safe to retry unchanged."""
        status_code = getattr(error, "status_code", None)
        if status_code == 429 or (isinstance(status_code, int) and status_code >= 500):
            return True

        name = type(error).__name__.lower()
        message = str(error).lower()
        transient_markers = (
            "ratelimit",
            "rate limit",
            "temporarily rate-limited",
            "timeout",
            "timed out",
            "connection",
            "service unavailable",
            "internal server error",
            "bad gateway",
            "gateway timeout",
        )
        return any(marker in name or marker in message for marker in transient_markers)

    def _parse_response(self, response) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message
        content = message.content or ""

        # 提取 thinking
        thinking = None
        clean_content = content
        match = re.search(r"<thinking>(.*?)</thinking>", content, re.DOTALL)
        if match:
            thinking = match.group(1).strip()
            clean_content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL).strip()

        # 解析 tool calls（过滤空 tool call，LiteLLM >=1.80 的已知 bug）
        tool_calls = None
        if hasattr(message, 'tool_calls') and message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                if not tc.function or not tc.function.name:
                    continue
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {"raw": tc.function.arguments}
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args
                ))
            if not tool_calls:
                tool_calls = None

        # 提取 token 用量和费用
        prompt_tokens = 0
        completion_tokens = 0
        cost = 0.0
        if hasattr(response, 'usage') and response.usage:
            prompt_tokens = getattr(response.usage, 'prompt_tokens', 0) or 0
            completion_tokens = getattr(response.usage, 'completion_tokens', 0) or 0
        try:
            cost = response._hidden_params.get("response_cost", 0.0) or 0.0
        except Exception:
            pass

        return LLMResponse(
            content=clean_content if clean_content else None,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            thinking=thinking,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
        )

    def get_usage(self) -> Dict:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
            "cost": self.total_cost,
            "calls": self.call_count,
        }

    def reset_usage(self):
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cost = 0.0
        self.call_count = 0

    def simple_chat(
        self,
        prompt: str,
        system: Optional[str] = None,
        *,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = self.chat(
            messages,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        return response.content or ""
