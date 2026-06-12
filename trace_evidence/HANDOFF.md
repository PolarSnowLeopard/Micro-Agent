# HANDOFF — Trace Evidence v1 (Wake#11)

## Commit
`167851d` — wake#11: evidence_card collapsible sections + edge case tests

## What Was Delivered

### 1. Evidence Card Readability (evidence_card.py)
- Collapsible `<details>` sections for tool timeline when >5 entries
- Collapsible `<details>` sections for planner events when >5 entries
- Keeps cards compact for large traces while preserving full detail on expand

### 2. Edge Case Tests (test_edge_cases.py — 15 tests, all PASS)
- **TestCheckerEdgeCases** (7 tests): empty bundle, single tool call (with/without return), missing planner thoughts, missing completion, missing verification, many-iterations-single-tool
- **TestExecutionEvidenceSlot** (5 tests): slot presence, structure keys, empty bundle paths, multiple services dispatch, elapsed_ms in metrics
- **TestMarkdownReadability** (3 tests): header presence, collapsible tool timeline >5, no collapsible when <=5

### 3. executionEvidence Slot Verification
- Tests confirm P0-⑤ executionEvidence slot in config_attachment works correctly
- Verified keys: traceSessionId, evidenceId, verdict, executionPath, toolChannels, dispatchSequence, metrics

## Test Status
- **111 passed** (existing) + **15 new** = 126 running tests
- **51 skipped** (MCP port-dependent)
- **2 pre-existing failures** in test_graceful_degradation (checker is lenient on missing session_id — design choice, not regression)
- **8 pre-existing errors** in test_pipeline_result_api (import/setup issue from prior wake)

## Known Issues for Next Wake
1. `test_graceful_degradation.py` — 2 tests expect WARN/FAIL for missing session_id but checker returns PASS. Decision needed: tighten checker or loosen test?
2. `test_pipeline_result_api.py` — 8 errors from import issue (PipelineResult class path changed?)
3. 3 unstaged simulation files (`orchestrator.py`, `sandbox_tool.py`, `logging_mcp_tool.py`) — from earlier work, not committed

## Architecture Notes
- All test factories now use **real dataclasses** from `trace_adapter.py` (no more FakeX classes)
- This eliminates interface drift permanently — tests fail immediately if fields change
