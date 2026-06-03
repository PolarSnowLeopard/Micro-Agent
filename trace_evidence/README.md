# trace_evidence — Trace Evidence Post-Processing Pipeline

> 让每次仿真运行**真实留下**可检验、可追溯的结构化证据。

## Quick Start

```bash
# Run pipeline on a trace file
python trace_evidence/run_pipeline.py workspace/data/traces/sim-b963f6d83a89.json -o ./output

# Or use as a library
python -c "
from trace_evidence import run_pipeline
result = run_pipeline('path/to/trace.json')
print(result.report.overall_status)  # PASS / WARN / FAIL
result.save_to_dir('./output')
"
```

## Output Artifacts

The pipeline produces 6 files:

| File | Description |
|------|-------------|
| `evidence_card.json` | Structured evidence: tool calls, phases, iterations, planner thoughts, verification |
| `evidence_card.md` | Human-readable markdown version of the evidence card |
| `checker_report.json` | Machine-readable report with 20 check results |
| `checker_report.md` | Human-readable report with pass/warn/fail table |
| `config_attachment_draft.json` | Config attachment draft linking evidence ID + executionEvidence to session config |
| `bundle.json` | Full pipeline bundle (trace + all intermediate data, for replay/debugging) |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Raw Trace JSON                              │
│  (172 events: step/log/complete/service/iteration/phase)        │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  TraceEvidenceAdapter │  ← Normalize + extract
                    │  (trace_adapter.py)   │    tool calls, phases,
                    │                      │    iterations, planner
                    │                      │    thoughts, verification
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
    ┌─────────▼─────┐  ┌──────▼──────┐  ┌──────▼──────────┐
    │ EvidenceCard   │  │ ConfigDraft │  │ EvidenceChecker  │
    │ (evidence_card │  │ (config_    │  │ (evidence_       │
    │  .py)          │  │  attachment │  │  checker.py)     │
    │                │  │  .py)       │  │  20 checks       │
    └────────────────┘  └─────────────┘  └─────────────────┘
```

## The 20 Checks

| # | Check | What it validates |
|---|-------|-------------------|
| 1 | `structural_integrity` | All required top-level fields present in trace |
| 2 | `service_coverage` | Every discovered service has call evidence |
| 3 | `tool_call_pairs` | Each tool call has a matching return event |
| 4 | `phase_completeness` | All phases have running→done lifecycle |
| 5 | `iteration_consistency` | Iteration events with proper state transitions |
| 6 | `verification_presence` | Explicit verification event exists |
| 7 | `evidence_gaps_summary` | No structural evidence gaps |
| 8 | `timeline_sanity` | Duration is within reasonable bounds |
| 9 | `channel_classification` | All calls classified into channels (mcp/local/http) |
| 10 | `tool_io_completeness` | Tool calls have input/output or explicit derivation note |
| 11 | `confidence_distribution` | Evidence confidence levels are consistent |
| 12 | `evidence_source_coverage` | Multiple provenance sources represented |
| 13 | `execution_path` | Logical execution path is reconstructable |
| 14 | `tool_channels_presence` | toolChannel metadata entries exist |
| 15 | `final_result` | Final result with success/failure status |
| 16 | `config_attachment_evidence_id` | Evidence ID available for config linkage |
| 17 | `tool_call_details_consistency` | Detail count matches summary total |
| 18 | `planner_events_completeness` | Planner events have iteration + content |
| 19 | `timeline_monotonicity` | Tool call timestamps are monotonically ordered |
| 20 | `result_hash_integrity` | Result hashes match recomputed sha256 of tool outputs |

Each check returns one of: **PASS**, **WARN** (non-fatal gap), **FAIL** (integrity violation), or **MISSING** (cannot evaluate).

## Python API

```python
from trace_evidence import (
    # Main pipeline
    run_pipeline,          # run_pipeline(path_or_dict) → PipelineResult
    
    # Core types
    PipelineResult,        # .report, .card, .config_draft, .save_to_dir()
    EvidenceCard,          # Structured evidence bundle
    CheckerReport,         # 19 check results + overall status
    ConfigAttachmentDraft, # Links evidence ID to config
    
    # Individual components
    TraceEvidenceAdapter,  # Parse raw trace → structured evidence
    EvidenceChecker,       # Run checks against evidence
    
    # Evidence items
    ToolCallEvidence,      # Individual tool call with source/confidence
    PhaseEvidence,         # Phase lifecycle
    IterationEvidence,     # Agent iteration
    PlannerThoughtEvidence,# Planner reasoning
    VerificationEvidence,  # Verification event
    
    # Utilities
    generate_evidence_id,  # Deterministic evidence ID from trace
    compute_fingerprint,   # SHA256 fingerprint
    sanitize_md_cell,      # Safe markdown rendering
    validate_evidence_card,# JSON Schema validation
    validate_checker_report,
)
```

## Provenance Model

Every evidence item carries:
- `source`: where it came from (`original_trace`, `derived_from_log`, `inferred_from_log`)
- `confidence`: reliability level (`original`, `derived`, `inferred`)

The pipeline never fabricates evidence. When data is missing, it reports `missing_evidence` explicitly rather than synthesizing fake entries.

## Security

- **Secret redaction**: API keys, tokens, JWTs, and credentials are automatically redacted from evidence output via `sanitize.redact_secrets()`
- **Markdown injection prevention**: All user-controlled content is sanitized before markdown rendering
- **Path traversal protection**: Tool names and identifiers are validated before use in file paths

## Testing

```bash
# Run all tests (125 as of v1.0.0)
cd /path/to/Micro-Agent
python -m unittest discover -s trace_evidence/tests -p "test_*.py" -v

# Run just the pipeline E2E test
python -m unittest trace_evidence.tests.test_pipeline -v

# Run schema validation tests only
python -m unittest trace_evidence.tests.test_schema_validation -v
```

Test coverage includes: unit tests, adversarial inputs, JSON Schema validation (both evidence_card and checker_report), E2E pipeline, CLI interface, and secret redaction.

## File Structure

```
trace_evidence/
├── __init__.py              # Pipeline orchestration + public API
├── trace_adapter.py         # Raw trace → structured evidence
├── evidence_card.py         # Evidence card builder + markdown renderer
├── evidence_checker.py      # 19-check verification engine
├── config_attachment.py     # Config draft builder
├── sanitize.py              # Secret redaction + markdown safety
├── schema_validator.py      # JSON Schema validation
├── run_pipeline.py          # CLI entry point
├── headless_run.py          # Headless simulation runner (optional)
├── README.md                # This file（对外说明）
# PROGRESS.md / INFRASTRUCTURE_REPORT.md — 本地开发笔记，已 gitignore
├── schemas/
│   ├── evidence_card_schema.json        # JSON Schema for evidence card output
│   └── checker_report_schema.json       # JSON Schema for checker report output
└── tests/
    ├── __init__.py
    ├── test_pipeline.py             # E2E pipeline tests
    ├── test_pipeline_result_api.py  # PipelineResult API tests
    ├── test_e2e_cli.py              # CLI integration tests
    ├── test_adversarial.py          # Adversarial/malicious input tests
    ├── test_sanitize.py             # Sanitization unit tests
    ├── test_redact_secrets.py       # Secret redaction tests
    └── test_schema_validation.py    # JSON Schema contract tests
```

## Integration

After trace metadata enhancement, new simulation runs automatically persist:
- Structured `tool_call_record` events (arguments, result, latency_ms)
- Full `metadata` (config snapshot, runtime env, tool_call_count)
- Verification event with status and reason
- Planner thought events with iteration context

For pre-enhancement traces, the adapter derives evidence from log text with `source="derived_from_log"` provenance markers.
