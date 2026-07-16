from types import SimpleNamespace

import pytest

from micro_agent.core.config import LLMConfig
from micro_agent.core.llm import LLM


def _completion(content: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))
        ],
        usage=None,
    )


@pytest.mark.asyncio
async def test_complete_disables_reasoning(monkeypatch):
    captured = {}

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return _completion()

    monkeypatch.setattr("micro_agent.core.llm.litellm.acompletion", fake_completion)
    llm = LLM(
        LLMConfig(
            model="openrouter/qwen/qwen3.6-flash",
            base_url="https://openrouter.ai/api/v1",
            reasoning_enabled=False,
        )
    )

    response = await llm.complete([{"role": "user", "content": "ping"}])

    assert response.content == "ok"
    assert captured["model"] == "openrouter/qwen/qwen3.6-flash"
    assert captured["api_base"] == "https://openrouter.ai/api/v1"
    assert captured["reasoning"] == {"enabled": False}


@pytest.mark.asyncio
async def test_complete_omits_reasoning_when_unconfigured(monkeypatch):
    captured = {}

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return _completion()

    monkeypatch.setattr("micro_agent.core.llm.litellm.acompletion", fake_completion)

    await LLM(LLMConfig()).complete([{"role": "user", "content": "ping"}])

    assert "reasoning" not in captured
