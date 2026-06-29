from __future__ import annotations

import json

import httpx
import pytest

from api.app import app
from micro_agent.meta_app.published_config import (
    PublishedMetaAppError,
    load_published_artifact,
)
from micro_agent.simulation.service_tool_session import (
    ServiceConnectionError,
    ServiceToolSession,
)


ARTIFACT = {
    "schemaVersion": "meta_app_artifact.v1",
    "artifactId": "app-functional",
    "app": {"name": "Functional App", "domain": "test", "description": ""},
    "taskContract": {"inputSlots": [], "outputSlots": []},
    "runtime": {"mode": "agent_only", "serviceBindings": []},
    "goldenPaths": [],
}


@pytest.mark.asyncio
async def test_published_artifact_is_loaded_from_service_detail(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/services/meta-1"
        return httpx.Response(200, json={
            "status": "success",
            "service": {
                "id": "meta-1",
                "type": "meta",
                "apiList": [{"metaAppArtifact": ARTIFACT}],
            },
        })

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ioeb.test") as client:
        artifact = await load_published_artifact("meta-1", client=client)

    assert artifact == ARTIFACT


@pytest.mark.asyncio
async def test_published_artifact_rejects_missing_artifact():
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={
        "status": "success",
        "service": {"id": "meta-1", "type": "meta", "apiList": [{}]},
    }))
    async with httpx.AsyncClient(transport=transport, base_url="http://ioeb.test") as client:
        with pytest.raises(PublishedMetaAppError, match="未关联 Artifact"):
            await load_published_artifact("meta-1", client=client)


@pytest.mark.asyncio
async def test_published_artifact_rejects_invalid_schema():
    invalid = {**ARTIFACT, "runtime": {"mode": "agent_only"}}
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={
        "status": "success",
        "service": {
            "id": "meta-1",
            "type": "meta",
            "apiList": [{"metaAppArtifact": invalid}],
        },
    }))
    async with httpx.AsyncClient(transport=transport, base_url="http://ioeb.test") as client:
        with pytest.raises(PublishedMetaAppError, match="serviceBindings"):
            await load_published_artifact("meta-1", client=client)


@pytest.mark.asyncio
async def test_meta_app_run_public_sse_contract(monkeypatch):
    from api.routes import agent as route

    async def load(_meta_app_id: str):
        return ARTIFACT

    async def run(artifact, message, *, prefer_golden_path):
        assert artifact == ARTIFACT
        assert message == "hello"
        assert prefer_golden_path is False
        return {
            "success": True,
            "result": "done",
            "events": [
                {"type": "think", "step": 1, "data": {"thought": "working"}},
                {"type": "done", "step": 1, "data": {"result": "done"}},
            ],
        }

    monkeypatch.setattr(route, "load_published_artifact", load)
    monkeypatch.setattr(route, "run_artifact", run)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ma.test") as client:
        response = await client.post(
            "/api/agent/meta_app/run",
            data={"message": "hello", "meta_app_id": "meta-1"},
        )

    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[0] == {"status": "start"}
    assert any(event.get("thought") == "working" for event in events)
    assert events[-1]["final_results"]["text_result"] == "done"


@pytest.mark.asyncio
async def test_explicit_fake_is_the_only_sandbox_entry():
    fake = ServiceToolSession([{"id": "fake", "name": "Fake", "isFake": True}])
    await fake.connect()
    assert fake.statuses()[0]["channel"] == "sandbox"

    real = ServiceToolSession([{"id": "real", "name": "Real", "isFake": False}])
    with pytest.raises(ServiceConnectionError, match="真实 MCP 配置无效"):
        await real.connect()
    assert real.statuses() == []
