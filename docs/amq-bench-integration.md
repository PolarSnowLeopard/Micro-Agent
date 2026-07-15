# AMQ-Bench Integration Design

## Objective

AMQ-Bench becomes the shared measurement layer between the research benchmark and IoEB's production MCP packaging feature:

- AMQ-Bench contributes Availability, Usability, Utility, failure attribution, and executable oracles to IoEB.
- IoEB contributes a real template-constrained repository-to-service system and reproducible production traces back to AMQ-Bench research.

The integration must not make product success depend on an LLM judge, leak holdout answers into prompts, or claim that arbitrary repository wrapping and template-constrained packaging are the same task.

## Two Tracks

### IoEB Template Track

This is the production acceptance track. Inputs already satisfy the versioned IoEB contract (`main.py`, `main_process`, exact dependencies, metadata, and deterministic tests). A conforming input is expected to package successfully; a non-conforming input must be rejected before build.

Positive Template Track cases live under `benchmarks/amq_template/development/`; explicit rejection controls live under `benchmarks/amq_template/negative/`. Holdout cases are selected by ID and repository but are not copied into the source tree.

### AMQ Open-Repository Track

This is the broader research track defined by AMQ-Bench: a pinned repository plus wrapping intent is converted into an MCP service. It measures code understanding and tool design in addition to packaging. It is not the initial product SLA.

Both tracks produce the same quality report so results remain comparable at the service boundary.

## Quality Contract

`mcp-packager score verification.json` produces `ioeb.amq-quality/v2`:

- `d1Availability`: Docker build and real MCP health (`initialize` plus non-empty `tools/list`).
- `d2Usability`: GoE schema quality plus GoV valid/invalid active probes.
- `d3Utility`: deterministic sample oracle and direct-function/MCP differential equality.
- `aqs`: `d1 * (0.4 * d2 + 0.6 * d3)`; null when Docker build has not run.
- `failureCategory`: build, dependency, import, protocol/runtime, or functional mismatch.
- `inputValidationGate`: every generated invalid-input probe must be rejected with a parameter-specific error.
- `publishable`: hard gate requiring D1, D3, and the input-validation gate to pass.
- `qualityGatePassed`: publishable plus D2 >= 0.7.

Unlike the current AMQ research harness, the IoEB strict profile has no LLM-verification fallback and no keyword fallback. Expected answers are used only by the verifier and must never enter generation prompts.

## Dataset Selection

The read-only `amq-suite` adapter uses AMQ's dataset, sample status, and a visible development-ID seed. It:

1. accepts only `specific_numeric`, `domain_structural`, or `exact_string` oracle tiers;
2. excludes samples requiring external credentials from the first track;
3. recognizes L0 negative controls by category or the dataset's `meb_l0_*` naming contract;
4. expands development membership by repository so sibling tasks cannot leak into holdout;
5. records SHA-256 fingerprints for every input file;
6. emits only summaries and intent hashes, not holdout intent or oracle content.

For the current local AMQ improvement snapshot, this yields 29 development samples, 119 holdout samples, 16 negative-control candidates, and 105 exclusions. L0 candidates still require manual audit because the AMQ audit found mislabeled boundary cases.

```bash
mcp-packager amq-suite \
  /path/to/AMQ-Bench/data/amq_bench.jsonl \
  --status /path/to/AMQ-Bench/data/sample_status.json \
  --development-ids /path/to/AMQ-Bench/data/subsets/mini30_ids.json \
  --output /tmp/ioeb-amq-suite.json
```

## Iteration Rules

- Fix categories and general transformations, never individual sample IDs.
- Development cases may be inspected; holdout intent, code, and oracle details must not enter prompts or repair context.
- Every production bug becomes a separate regression case.
- The original three demo GTs are regression fixtures, not benchmark evidence.
- Report first-pass success, repaired success, false-success rate, false-rejection rate, D1/D2/D3, latency, and cost separately.
- AQS is a comparative research metric; the production publish decision remains a hard gate.

## Verified Template Baseline

The first Template Track case was cold-built and executed on the same VPN experiment server class used for AMQ-Bench. The verifier removed its disposable image and container after the run.

| Sample | D1 | D2 | D3 | AQS | Publish gate |
| --- | ---: | ---: | ---: | ---: | --- |
| `meb_mpmath_001` | 1.0000 | 1.0000 | 1.0000 | 1.0000 | passed |

The run covered Docker build/start, an isolated read-only container, MCP initialization, `tools/list`, a deterministic valid call, direct-function/MCP/oracle equality, and missing/type/constraint-invalid argument probes. The real MCP schema exposed both the `pi`/`e` enum and the `1..200` precision bounds. The machine-readable result is stored in `benchmarks/amq_template/results/meb_mpmath_001.vpn.json`.

This single case proves the end-to-end measurement path, not the overall success rate. A publishable product claim requires a larger repository-separated Template Track suite and confidence intervals.

### Development batch

The strict sequential batch runner now covers five accepted capabilities and two expected rejections. The accepted cases span synchronous and asynchronous entrypoints, scalar and enum parameters, closed nested objects, constrained arrays, fixed-length tuple arrays, pure-Python wheels, and scientific dependency stacks. Publication also requires every generated invalid-input probe to be rejected with a parameter-specific message.

| Sample | Profile | D1 | D2 | D3 | AQS | Cold total | Warm total |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `meb_biopython_002` | async entrypoint, enum array, binary wheel | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 19.6860 s | 18.6710 s |
| `meb_cytopus_db_002` | constrained string arrays, standard library | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 12.4868 s | 14.9438 s |
| `meb_mpmath_001` | pure-Python wheel, scalar inputs | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 13.7345 s | 3.6167 s |
| `meb_networkx_002` | nested tuple arrays, enum default | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 14.4084 s | 3.4791 s |
| `meb_pydy_001` | scientific transitive dependencies, closed object | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 38.5127 s | 5.2423 s |

Both cold (`--no-cache`) and default-cache runs achieved 100% acceptance, verification, input-validation, publishable, quality-gate, and expected-rejection rates. The final quality-v2 cold run generated and passed 23 invalid-input probes across five services; its mean was 19.7657 seconds. The default-cache mean was 9.1906 seconds. The two invalid packages were rejected in a mean of 0.0010 seconds without building an image. These timings are engineering baselines rather than statistically stable latency claims.

This batch exposed and fixed two general contract defects. A single-value `Literal` must use JSON Schema `const`, and a typed dictionary still needs a way to declare required keys, per-key bounds, and rejection of extra properties. Recursive `parameterConstraints` now flow through static test validation, generated Pydantic validation, MCP `tools/list`, and active invalid-input probes. That change raised the PyDy D2 score from 0.9792 to 1.0000. The condensed machine-readable batch result is stored in `benchmarks/amq_template/results/development_batch.vpn.json`.

Five visible positive cases are still too few for a broad production success-rate claim. The next statistical milestone is a repository-separated holdout run with confidence intervals; the current result establishes the deterministic packaging path and its measurable acceptance boundary.

## Current System Integration Boundary

The existing frontend-compatible code-analysis and packaging endpoints now use the standalone deterministic engine. They expose the original function-graph and Base64 service-package fields, while also returning validation, plan, and static-verification data. The existing backend deploy flow reads the generated artifact manifest so the catalog points to the real Streamable HTTP endpoint and Tool.

This first integration intentionally does not refactor the system deployment flow. It performs static verification before handing the artifact to the existing asynchronous Docker Compose deployment path; the full Docker quality report is therefore not yet persisted as a publication record, and the current frontend completion signal still means “deployment accepted” rather than “container health verified.” A later system-side iteration should persist the complete quality report and allow catalog publication only when `publishable=true`; `qualityGatePassed` can drive review or quality labels without changing the existing frontend layout.
