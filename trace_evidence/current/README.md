# Trace Evidence — Current Baseline

This directory holds the single current baseline artifact set produced from one
complete meta-app simulation run against the real MCP services. It exists to
verify that the trace evidence infrastructure is ready to support later
trace-grounded artifact experiments.

This is not a versioned or archived output. There is exactly one current
baseline here; re-running the pipeline overwrites it.

## Files

| File | What it is |
|------|------------|
| `trace.json` | Raw structured v1 trace from the simulation run (source of truth) |
| `evidence_card.json` / `.md` | Human-reviewable evidence card: timeline, tool I/O summary, services, verification, provenance grading |
| `checker_report.json` / `.md` | Completeness-gated quality checker result over the evidence |
| `config_attachment_draft.json` | Config attachment draft with traceable evidence ids and on-disk evidence paths |
| `pipeline_command.txt` | Exact commands to reproduce this baseline |
| `current_baseline_report.md` | Capability / gap report for this baseline |

## Run source

real_mcp — the run connected to three live MCP services
(svc-medical-calc, svc-linezolid, svc-healthcovered) over MCP transport.
Provenance grading: 100% original confidence, channel distribution
real_mcp=16 / mcp=3, not log-parsed.

## Checker status

PASS — trace is COMPLETE (reached both planning and verification phases),
21/21 quality checks pass, 0 warnings, 0 evidence gaps. The run itself
completed successfully (success=True, 1 iteration).

## Reproduce

See `pipeline_command.txt`.
