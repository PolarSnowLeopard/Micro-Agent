# Repo2MCP v8 provenance

The `vendor/` directory is a source snapshot of the Repo2MCP implementation
used for the AMQ-Bench v8 experiments on the project VPN server.

- Source directory: `/home/lighthouse/github/Repo2MCP`
- Git commit: `24263ba` (`v3: DARP + BAGE context management for large repos`)
- Snapshot date: 2026-07-21
- Experiment evidence: `/mnt/vdb500/work/repo2mcp_v8_full.log` (24/30
  successful constructions with GPT-5.4)

The experiment directory contained uncommitted refinements.  This snapshot
therefore records the actual files used by the experiment instead of pretending
that commit `24263ba` alone represents v8.  No `.env`, logs, generated output,
workspace, Git metadata, or credentials are included.

Local integration changes are deliberately bounded:

1. `vendor/run_request.py` provides a JSON subprocess boundary.
2. `MCPWrapper.run(..., stop_after_analysis=True)` permits the existing frontend
   analysis phase to stop after producing `tool_design.json`.
3. `LLMConfig.reasoning_enabled` forwards the platform's explicit non-thinking
   setting to LiteLLM/OpenRouter.
4. `tool_design_override` reuses the short-lived result of the frontend analysis
   request instead of paying for the same semantic analysis twice.
5. A bounded completion nudge and one-shot JSON compiler prevent smaller
   production models from spending every analysis step on import exploration.
6. Agents return immediately after their required output files exist; later
   deterministic validation remains responsible for accepting those files.
7. Batch and subprocess entry points both honor `LLM_REASONING_ENABLED`, so the
   production Qwen profile cannot silently fall back to provider-default thinking.
8. The reliable package index, timeout, and retry policy is applied before the
   paper build gate, rather than mutating an already-verified Dockerfile later.
9. Common Python import names are normalized to their installable distribution
   names before Docker build, avoiding predictable repair-agent loops.

All application-specific staging, sanitisation, frontend graph conversion, and
deployment metadata live outside `vendor/`.
