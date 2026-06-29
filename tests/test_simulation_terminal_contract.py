"""SSE complete 只能在 BuildBundle 已落盘后发出。"""

from __future__ import annotations

import asyncio
import json
import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from micro_agent.simulation.build_bundle import BuildBundleStore
from micro_agent.simulation.orchestrator import SimulationEvent


async def _response_body(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def _complete_payload(body: str) -> dict:
    blocks = [block for block in body.split("\n\n") if block.startswith("event: complete\n")]
    assert len(blocks) == 1
    data_line = next(line for line in blocks[0].splitlines() if line.startswith("data: "))
    return json.loads(data_line[6:])


def test_complete_is_emitted_after_publishable_bundle_is_saved(tmp_path, monkeypatch):
    from api.routes import simulation as route

    class FakeOrchestrator:
        def __init__(self, _config):
            pass

        async def run(self):
            yield SimulationEvent("tool_call_record", {
                "call_id": "call-1",
                "tool_name": "fake-service_execute",
                "service_id": "fake-service",
                "service_name": "Fake Service",
                "channel": "sandbox",
                "source": "sandbox",
                "iteration": 1,
                "arguments": {"action": "execute"},
                "result": "ok",
                "success": True,
                "timestamp": 1.0,
            })
            yield SimulationEvent("verifier_result", {
                "iteration": 1,
                "status": "PASSED",
                "summary": "ok",
            })
            yield SimulationEvent("complete", {
                "success": True,
                "metrics": {"iterations": 1, "elapsedMs": 1},
            })

        def call_records(self):
            return []

        def cancel(self):
            pass

    store = BuildBundleStore(root=tmp_path)
    monkeypatch.setattr(route, "_store", store)
    monkeypatch.setattr(route, "SimulationOrchestrator", FakeOrchestrator)

    original_to_sse = SimulationEvent.to_sse

    def guarded_to_sse(event):
        if event.type == "complete":
            assert event.data.get("buildId")
            assert store.load_part(event.data["buildId"], "manifest")
        return original_to_sse(event)

    monkeypatch.setattr(SimulationEvent, "to_sse", guarded_to_sse)

    request = route.SimulationStartRequest(**{
        "appName": "terminal contract",
        "domain": "generic",
        "scenarioParsed": {
            "goal": "execute fake service",
            "description": "deterministic terminal contract",
        },
        "servicesMeta": [{
            "id": "fake-service",
            "name": "Fake Service",
            "isFake": True,
        }],
    })
    start = asyncio.run(route.start_simulation(request))

    build_id = start["buildId"]
    response = asyncio.run(route.simulation_stream(build_id))
    body = asyncio.run(_response_body(response))

    complete = _complete_payload(body)
    assert complete["success"] is True
    assert complete["publishable"] is True
    assert complete["buildId"] == build_id
    assert complete["artifactId"]
    assert store.load_part(build_id, "manifest")["artifactId"] == complete["artifactId"]
    assert build_id not in route._sessions
