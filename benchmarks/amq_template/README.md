# AMQ-Bench Template Track

This directory contains audited AMQ-Bench capabilities adapted to the strict IoEB algorithm submission contract. It evaluates the production promise "template-conforming algorithm packages are packaged successfully" and is intentionally separate from AMQ-Bench's broader raw repository-to-service track.

Every case uses the production v1 contract, including deterministic test oracles and machine-readable parameter constraints where applicable. The legacy-compatible profile is not benchmarked as a publishable service path.

Run the development suite with cold Docker builds:

```bash
mcp-packager batch . --docker --no-cache --output /tmp/ioeb-amq-batch.json
```

Committed results under `results/` are condensed baselines. The command output remains the full diagnostic report containing validation issues, Docker checks, MCP runtime traces, quality scores, and per-stage timings.

Rules:

- `development/` cases may be inspected while improving the packager.
- `negative/` cases declare `expectedDisposition=reject` and measure whether unsafe or incomplete submissions are rejected before Docker build.
- Holdout samples are selected by `mcp-packager amq-suite` and must not be copied into prompts or committed here.
- Every package records its AMQ sample ID and pinned source repository commit.
- Expected results belong only in `ioeb_algorithm.json` test oracles, never in generation prompts or special-case code.
- Fixes must be general rules and must pass all existing regression cases.
