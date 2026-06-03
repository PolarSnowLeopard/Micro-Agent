"""Tests for config_attachment provenance logic (stdlib unittest)."""
import unittest
from dataclasses import dataclass, field
from typing import Optional

from trace_evidence.config_attachment import build_config_attachment_draft


@dataclass
class FakeToolCall:
    tool_name: str
    direction: str = "call"
    service_id: str = "svc-test"
    source: str = "persisted_metadata"
    channel: Optional[str] = "real_mcp"
    latency_ms: Optional[int] = 10


@dataclass
class FakeCompletion:
    execution_path: list = field(default_factory=lambda: ["phase1"])
    tool_channels: list = field(default_factory=list)
    iterations: int = 1
    elapsed_ms: int = 100
    success: bool = True


@dataclass
class FakeBundle:
    session_id: str = "test-session"
    tool_calls: list = field(default_factory=list)
    completion: Optional[FakeCompletion] = None
    strategy: dict = field(default_factory=dict)
    missing_evidence: list = field(default_factory=list)
    app_name: str = "TestApp"
    domain: str = "test"


@dataclass
class FakeCard:
    evidence_id: str = "ev-test-1234"
    app_name: str = "TestApp"
    domain: str = "test"
    verdict: str = "PASSED"
    verification: Optional[dict] = field(default_factory=lambda: {"status": "PASSED"})
    summary: Optional[dict] = field(default_factory=lambda: {"total_checks": 1, "passed": 1})


class TestProvenanceLogic(unittest.TestCase):
    def test_structured_source_yields_structured_event(self):
        bundle = FakeBundle(
            tool_calls=[FakeToolCall(tool_name="calc_add", source="persisted_metadata")],
            completion=FakeCompletion(),
        )
        d = build_config_attachment_draft(bundle, FakeCard()).to_dict()
        self.assertEqual(d["dispatch_sequence"][0]["evidence_source"], "structured_event")

    def test_internal_service_excluded(self):
        bundle = FakeBundle(
            tool_calls=[
                FakeToolCall(tool_name="internal_log", service_id="internal"),
                FakeToolCall(tool_name="calc_add", service_id="svc-calc"),
            ],
            completion=FakeCompletion(),
        )
        d = build_config_attachment_draft(bundle, FakeCard()).to_dict()
        self.assertEqual(len(d["dispatch_sequence"]), 1)
        self.assertEqual(d["dispatch_sequence"][0]["tool"], "calc_add")

    def test_dedup_by_tool_name(self):
        bundle = FakeBundle(
            tool_calls=[
                FakeToolCall(tool_name="calc_add"),
                FakeToolCall(tool_name="calc_add"),
            ],
            completion=FakeCompletion(),
        )
        d = build_config_attachment_draft(bundle, FakeCard()).to_dict()
        self.assertEqual(len(d["dispatch_sequence"]), 1)

    def test_return_direction_excluded(self):
        bundle = FakeBundle(
            tool_calls=[FakeToolCall(tool_name="calc_add", direction="return")],
            completion=FakeCompletion(),
        )
        d = build_config_attachment_draft(bundle, FakeCard()).to_dict()
        self.assertEqual(len(d["dispatch_sequence"]), 0)


class TestSchemaVersion(unittest.TestCase):
    def test_schema_version_present(self):
        bundle = FakeBundle(
            tool_calls=[FakeToolCall(tool_name="calc_add")],
            completion=FakeCompletion(),
        )
        d = build_config_attachment_draft(bundle, FakeCard()).to_dict()
        self.assertEqual(d.get("schema_version"), "1.0.0")

    def test_schema_version_is_first_key(self):
        bundle = FakeBundle(
            tool_calls=[FakeToolCall(tool_name="calc_add")],
            completion=FakeCompletion(),
        )
        d = build_config_attachment_draft(bundle, FakeCard()).to_dict()
        self.assertEqual(list(d.keys())[0], "schema_version")


if __name__ == "__main__":
    unittest.main()
