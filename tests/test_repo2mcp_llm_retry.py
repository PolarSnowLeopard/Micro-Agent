from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


VENDOR_ROOT = (
    Path(__file__).resolve().parents[1]
    / "micro_agent"
    / "packaging"
    / "repo2mcp_backend"
    / "vendor"
)


@pytest.fixture
def vendor_client(monkeypatch):
    monkeypatch.syspath_prepend(str(VENDOR_ROOT))
    for name in ("src.llm.client", "src.llm", "config"):
        sys.modules.pop(name, None)
    return importlib.import_module("src.llm.client")


def test_llm_client_retries_transient_rate_limit(vendor_client, monkeypatch):
    calls = []
    sleeps = []

    class RateLimitError(Exception):
        status_code = 429

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise RateLimitError("temporarily rate-limited upstream")
        return type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type(
                                "Message", (), {"content": "ok", "tool_calls": None}
                            )(),
                            "finish_reason": "stop",
                        },
                    )()
                ],
                "usage": None,
                "_hidden_params": {},
            },
        )()

    monkeypatch.setattr(vendor_client, "completion", fake_completion)
    monkeypatch.setattr(vendor_client.time, "sleep", sleeps.append)
    config = vendor_client.LLMConfig(
        model="openrouter/qwen/qwen3.6-flash",
        api_key="test",
        max_retries=3,
        retry_base_seconds=2,
        retry_max_seconds=60,
    )

    response = vendor_client.LLMClient(config).chat([{"role": "user", "content": "x"}])

    assert response.content == "ok"
    assert len(calls) == 3
    assert sleeps == [2, 4]


def test_llm_client_does_not_retry_non_transient_error(vendor_client, monkeypatch):
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        raise ValueError("invalid request")

    monkeypatch.setattr(vendor_client, "completion", fake_completion)
    config = vendor_client.LLMConfig(
        model="openrouter/qwen/qwen3.6-flash",
        api_key="test",
        max_retries=5,
    )

    with pytest.raises(ValueError, match="invalid request"):
        vendor_client.LLMClient(config).chat([{"role": "user", "content": "x"}])

    assert len(calls) == 1
