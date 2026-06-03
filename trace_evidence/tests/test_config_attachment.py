"""Tests for config_attachment provenance logic.

Validates that dispatch_sequence.evidence_source correctly reflects
the authoritative tc.source field from the adapter.
"""
import pytest
from dataclasses import dataclass, field
from typing import Optional
from trace_evidence.config_attachment import build_config_attachment_draft


# Minimal stubs to avoid coupling to full adapter internals
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


class TestProvenanceLogic:
    """evidence_source should reflect tc.source, not channel/latency."""

    def test_structured_source_yields_structured_event(self):
        bundle = FakeBundle(
            tool_calls=[FakeToolCall(tool_name="calc_add", source="persisted_metadata")],
            completion=FakeCompletion(),
        )
        card = FakeCard()
        draft = build_config_attachment_draft(bundle, card)
        d = draft.to_dict()
        assert d["dispatch_sequence"][0]["evidence_source"] == "structured_event"

    def test_log_derived_source_yields_log_text_extraction(self):
        bundle = FakeBundle(
            tool_calls=[FakeToolCall(
                tool_name="calc_add",
                source="derived_from_log",
                channel="real_mcp",  # even with channel present
                latency_ms=15,
            )],
            completion=FakeCompletion(),
        )
        card = FakeCard()
        draft = build_config_attachment_draft(bundle, card)
        d = draft.to_dict()
        assert d["dispatch_sequence"][0]["evidence_source"] == "log_text_extraction"

    def test_unknown_source_with_channel_falls_back_to_structured(self):
        """If source is empty but channel exists, fallback = structured_event."""
        bundle = FakeBundle(
            tool_calls=[FakeToolCall(
                tool_name="calc_add",
                source="",
                channel="real_mcp",
            )],
            completion=FakeCompletion(),
        )
        card = FakeCard()
        draft = build_config_attachment_draft(bundle, card)
        d = draft.to_dict()
        assert d["dispatch_sequence"][0]["evidence_source"] == "structured_event"

    def test_unknown_source_no_channel_falls_back_to_log(self):
        """If source is empty and no channel, fallback = log_text_extraction."""
        bundle = FakeBundle(
            tool_calls=[FakeToolCall(
                tool_name="calc_add",
                source="",
                channel=None,
            )],
            completion=FakeCompletion(),
        )
        card = FakeCard()
        draft = build_config_attachment_draft(bundle, card)
        d = draft.to_dict()
        assert d["dispatch_sequence"][0]["evidence_source"] == "log_text_extraction"

    def test_internal_service_excluded(self):
        """tool_calls with service_id='internal' are excluded from dispatch."""
        bundle = FakeBundle(
            tool_calls=[
                FakeToolCall(tool_name="internal_log", service_id="internal"),
                FakeToolCall(tool_name="calc_add", service_id="svc-calc"),
            ],
            completion=FakeCompletion(),
        )
        card = FakeCard()
        draft = build_config_attachment_draft(bundle, card)
        d = draft.to_dict()
        assert len(d["dispatch_sequence"]) == 1
        assert d["dispatch_sequence"][0]["tool"] == "calc_add"

    def test_dedup_by_tool_name(self):
        """Only first call per tool_name is included."""
        bundle = FakeBundle(
            tool_calls=[
                FakeToolCall(tool_name="calc_add"),
                FakeToolCall(tool_name="calc_add"),  # duplicate
            ],
            completion=FakeCompletion(),
        )
        card = FakeCard()
        draft = build_config_attachment_draft(bundle, card)
        d = draft.to_dict()
        assert len(d["dispatch_sequence"]) == 1

    def test_return_direction_excluded(self):
        """Only 'call' direction appears in dispatch_sequence."""
        bundle = FakeBundle(
            tool_calls=[
                FakeToolCall(tool_name="calc_add", direction="return"),
            ],
            completion=FakeCompletion(),
        )
        card = FakeCard()
        draft = build_config_attachment_draft(bundle, card)
        d = draft.to_dict()
        assert len(d["dispatch_sequence"]) == 0


class TestSchemaVersion:
    """Verify schema_version is present and positioned first in output."""

    def test_schema_version_present(self):
        """config_attachment_draft must include schema_version."""
        bundle = FakeBundle(
            tool_calls=[FakeToolCall(tool_name="calc_add")],
            completion=FakeCompletion(),
        )
        card = FakeCard()
        draft = build_config_attachment_draft(bundle, card)
        d = draft.to_dict()
        assert "schema_version" in d
        assert d["schema_version"] == "1.0.0"

    def test_schema_version_is_first_key(self):
        """schema_version should be the first key in serialized output (convention)."""
        bundle = FakeBundle(
            tool_calls=[FakeToolCall(tool_name="calc_add")],
            completion=FakeCompletion(),
        )
        card = FakeCard()
        draft = build_config_attachment_draft(bundle, card)
        d = draft.to_dict()
        assert list(d.keys())[0] == "schema_version"
