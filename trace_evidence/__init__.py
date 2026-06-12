"""trace_evidence — Trace Evidence Post-Processing Pipeline

Provides structured evidence extraction, validation, and reporting
for meta-application simulation traces.

Quick start (one call):
    from trace_evidence import run_pipeline
    result = run_pipeline(trace_data)  # dict or path to JSON
    # result.card_md, result.report_md, result.bundle, result.card, result.report

Quick start (step by step):
    from trace_evidence import TraceEvidenceAdapter, build_evidence_card
    adapter = TraceEvidenceAdapter(trace_data)
    bundle = adapter.extract()
    card = build_evidence_card(bundle)

Modules:
    trace_adapter       — Extract structured evidence from raw trace JSON
    evidence_card       — Generate evidence cards (JSON + Markdown)
    config_attachment   — Generate config attachment drafts
    evidence_checker    — Validate evidence completeness and quality
    schema_validator    — JSON Schema validation for output artifacts
    sanitize            — Input sanitization utilities
    run_pipeline        — CLI entry point for full pipeline
"""

from __future__ import annotations

# Adapter layer
from .trace_adapter import (
    TraceEvidenceAdapter,
    TraceEvidenceBundle,
    ToolCallEvidence,
    ServiceEvidence,
    PlannerThoughtEvidence,
    PhaseEvidence,
    IterationEvidence,
    VerificationEvidence,
    CompleteEvidence,
)

# Evidence card
from .evidence_card import (
    EvidenceCard,
    build_evidence_card,
    render_evidence_card_markdown,
    generate_evidence_id,
    compute_fingerprint,
)

# Config attachment
from .config_attachment import (
    ConfigAttachmentDraft,
    build_config_attachment_draft,
)

# Checker
from .evidence_checker import (
    EvidenceChecker,
    CheckResult,
    CheckerReport,
    check_category,
    summarize_evidence_dimensions,
    render_checker_report_markdown,
)

# Schema validation
from .schema_validator import (
    ValidationResult,
    validate_evidence_card,
    validate_checker_report,
    validate_file,
)

# Sanitization
from .sanitize import (
    sanitize_md_cell,
    sanitize_identifier,
    sanitize_md_block,
    sanitize_md_inline,
    validate_tool_name,
)

# ---------------------------------------------------------------------------
# Convenience: one-call pipeline
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dataclass, asdict as _asdict, fields as _fields
from pathlib import Path as _Path
import json as _json
import os as _os
from typing import Union as _Union, List as _List


def _safe_asdict(obj):
    """Recursively convert a dataclass to a dict, handling non-dataclass leaves."""
    if hasattr(obj, '__dataclass_fields__'):
        result = {}
        for f in _fields(obj):
            val = getattr(obj, f.name)
            result[f.name] = _safe_asdict(val)
        return result
    elif isinstance(obj, list):
        return [_safe_asdict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: _safe_asdict(v) for k, v in obj.items()}
    else:
        return obj


@_dataclass
class PipelineResult:
    """All artifacts from a single pipeline run."""
    bundle: TraceEvidenceBundle
    card: EvidenceCard
    card_md: str
    report: "CheckerReport"
    report_md: str
    config_draft: ConfigAttachmentDraft

    @property
    def evidence_events(self) -> _List[dict]:
        """Flattened list of all evidence items from the bundle as dicts.

        Each dict includes an ``event_type`` key for discriminating the kind:
        tool_call, service, planner_thought, phase, iteration, verification, completion.
        """
        events = []
        for tc in self.bundle.tool_calls:
            d = _safe_asdict(tc)
            d["event_type"] = "tool_call"
            events.append(d)
        for svc in self.bundle.services:
            d = _safe_asdict(svc)
            d["event_type"] = "service"
            events.append(d)
        for pt in self.bundle.planner_thoughts:
            d = _safe_asdict(pt)
            d["event_type"] = "planner_thought"
            events.append(d)
        for ph in self.bundle.phases:
            d = _safe_asdict(ph)
            d["event_type"] = "phase"
            events.append(d)
        for it in self.bundle.iterations:
            d = _safe_asdict(it)
            d["event_type"] = "iteration"
            events.append(d)
        if self.bundle.verification:
            d = _safe_asdict(self.bundle.verification)
            d["event_type"] = "verification"
            events.append(d)
        if self.bundle.completion:
            d = _safe_asdict(self.bundle.completion)
            d["event_type"] = "completion"
            events.append(d)
        return events

    def to_dict(self) -> dict:
        """Serialize all artifacts to a single dict (JSON-safe).

        Returns a dict with keys: evidence_id, session_id, card, report,
        config_draft, evidence_events, diagnostics, missing_evidence.
        """
        return {
            "evidence_id": self.card.evidence_id,
            "session_id": self.card.session_id,
            "card": _safe_asdict(self.card),
            "report": _safe_asdict(self.report),
            "config_draft": _safe_asdict(self.config_draft),
            "evidence_events": self.evidence_events,
            "diagnostics": self.bundle.diagnostics,
            "missing_evidence": self.bundle.missing_evidence,
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize all artifacts to a JSON string."""
        return _json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def save_to_dir(self, output_dir: _Union[str, _Path]) -> dict:
        """Write all artifacts to a directory.

        Creates the directory if it doesn't exist. Writes:
        - evidence_card.md
        - evidence_card.json
        - checker_report.md
        - checker_report.json
        - config_draft.json
        - pipeline_result.json (complete bundle)

        Returns a dict mapping artifact name to file path.
        """
        out = _Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        files = {}

        # Card markdown
        card_md_path = out / "evidence_card.md"
        card_md_path.write_text(self.card_md, encoding="utf-8")
        files["card_md"] = str(card_md_path)

        # Card JSON
        card_json_path = out / "evidence_card.json"
        card_json_path.write_text(
            _json.dumps(_safe_asdict(self.card), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        files["card_json"] = str(card_json_path)

        # Report markdown
        report_md_path = out / "checker_report.md"
        report_md_path.write_text(self.report_md, encoding="utf-8")
        files["report_md"] = str(report_md_path)

        # Report JSON
        report_json_path = out / "checker_report.json"
        report_json_path.write_text(
            _json.dumps(_safe_asdict(self.report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        files["report_json"] = str(report_json_path)

        # Config draft JSON
        config_path = out / "config_draft.json"
        config_path.write_text(
            _json.dumps(_safe_asdict(self.config_draft), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        files["config_draft"] = str(config_path)

        # Full pipeline result JSON
        full_path = out / "pipeline_result.json"
        full_path.write_text(self.to_json(), encoding="utf-8")
        files["pipeline_result"] = str(full_path)

        return files


def run_pipeline(
    trace: _Union[dict, str, _Path],
) -> PipelineResult:
    """Run the full evidence pipeline in one call.

    Args:
        trace: Raw trace data as a dict, or a path (str/Path) to a JSON file.

    Returns:
        PipelineResult with all generated artifacts.

    Example:
        >>> from trace_evidence import run_pipeline
        >>> result = run_pipeline("path/to/trace.json")
        >>> print(result.card_md)
    """
    # Load trace data
    if isinstance(trace, (str, _Path)):
        path = _Path(trace)
        with open(path, "r", encoding="utf-8") as f:
            trace_data = _json.load(f)
    elif isinstance(trace, dict):
        trace_data = trace
    else:
        raise TypeError(
            f"run_pipeline() expects a dict or path (str/Path), got {type(trace).__name__}"
        )

    # Extract evidence
    adapter = TraceEvidenceAdapter(trace_data)
    bundle = adapter.extract()

    # Build card
    card = build_evidence_card(bundle)
    card_md = render_evidence_card_markdown(card)

    # Build config attachment
    config_draft = build_config_attachment_draft(bundle, card)

    # Run checker
    checker = EvidenceChecker(bundle, card)
    report = checker.run_all()
    report_md = render_checker_report_markdown(report)

    return PipelineResult(
        bundle=bundle,
        card=card,
        card_md=card_md,
        report=report,
        report_md=report_md,
        config_draft=config_draft,
    )


__all__ = [
    # Adapter
    "TraceEvidenceAdapter",
    "TraceEvidenceBundle",
    "ToolCallEvidence",
    "ServiceEvidence",
    "PlannerThoughtEvidence",
    "PhaseEvidence",
    "IterationEvidence",
    "VerificationEvidence",
    "CompleteEvidence",
    # Evidence card
    "EvidenceCard",
    "build_evidence_card",
    "render_evidence_card_markdown",
    "generate_evidence_id",
    "compute_fingerprint",
    # Config attachment
    "ConfigAttachmentDraft",
    "build_config_attachment_draft",
    # Checker
    "EvidenceChecker",
    "CheckResult",
    "CheckerReport",
    "check_category",
    "summarize_evidence_dimensions",
    "render_checker_report_markdown",
    # Schema validation
    "ValidationResult",
    "validate_evidence_card",
    "validate_checker_report",
    "validate_file",
    # Sanitization
    "sanitize_md_cell",
    "sanitize_identifier",
    "sanitize_md_block",
    "sanitize_md_inline",
    "validate_tool_name",
    # Pipeline convenience
    "PipelineResult",
    "run_pipeline",
]
