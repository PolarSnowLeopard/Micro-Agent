# Current Trace Evidence Baseline Report

Generated from a single complete meta-app simulation run against the live MCP
services. Purpose: verify the trace evidence infrastructure is ready to support
later trace-grounded artifact experiments. This is a one-shot current baseline,
not a versioned or archived output.

## 1. Scope

- One real-MCP simulation run, one pipeline pass, one artifact set in `trace_evidence/current/`.
- No version management, no history archiving, no multi-output retention.
- Goal: confirm the chain trace -> evidence bundle -> evidence card -> checker -> config attachment works end to end on real tool calls.

## 2. Run identity

- Session: `sim-headless-a77ddf82cc70`
- Evidence id: `ev-a77ddf-288964e3`
- Fingerprint: `288964e3e197ed2efa90411f...`
- App: 乡村医疗AI辅助诊断（headless证据采集）, domain health, mode production
- Strategy: minIterations=1, verificationMode=strict
- Outcome: success=True, 1 iteration, 61878 ms, 91 trace events (8 tool_call_records)

## 3. Run source

real_mcp. Three live MCP services were discovered online and called over MCP transport:

| Service | Status | Tools | Calls |
|---------|--------|-------|-------|
| svc-medical-calc | online | 6 | 5 |
| svc-healthcovered (ACA) | online | 3 | 2 |
| svc-linezolid | online | 1 | 1 |

Provenance grading: 30 evidence items, 100% original confidence,
channel distribution real_mcp=16 / mcp=3, not log-parsed. No sandbox / fixture / fallback.

## 4. Checker status

PASS.

- Completeness gate: COMPLETE — trace reached both planning and verification phases.
- Quality: 21/21 checks PASS, 0 warnings, 0 failed, 0 missing evidence.
- verifier_result: PASSED. All required services were invoked (ACA eligibility, linezolid dosing, medical calculator), data flow and call ordering judged coherent. The verifier reason is a planner/verifier natural-language summary, not chain-of-thought.

## 5. Evidence traceability

- toolCallEvidenceIds, plannerDecisionEvidenceIds, verificationEvidenceIds all present in `config_attachment_draft.json`.
- executionEvidence paths (tracePath / evidenceCardPath / checkerReportPath) backfilled to the on-disk `current/` files after pipeline run; missingEvidenceIds is now empty.
- evidence_card carries per-call tool I/O summary (8 tool_call_details), execution path, services discovered, and verification block, each traceable to a call id.

## 6. Current capability

- End-to-end chain works on a real-MCP trace: structured v1 trace -> evidence bundle -> reviewable evidence card -> completeness-gated checker -> config attachment draft with traceable ids and resolved on-disk paths.
- Checker correctly separates "incomplete trace" from "complete-but-failed run", and now yields a clean PASS on a successful run.
- Provenance grading distinguishes real_mcp vs other channels and flags log-parsed vs structured evidence.

## 7. Still missing / weaknesses

- verifier checks are present but lean; richer per-check `evidence_refs` linking each verification claim back to specific tool-call ids would strengthen verification traceability.
- Baseline rests on a single scenario (rural-medical). Breadth across domains is out of scope this round but untested.

## 8. Next step

- Treat this PASS baseline as the reference for later trace-grounded artifact experiments.
- When extending the verifier, add explicit evidence_refs per check. Do not continue designing; run and report.
