#!/usr/bin/env python3
"""JSON request boundary for the vendored Repo2MCP v8 implementation.

This file is intentionally tiny.  The application invokes the paper backend in
an isolated subprocess so its historical top-level ``config``/``src`` imports
cannot collide with Micro-Agent's modules.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from config import AgentConfig, LLMConfig
from src.wrapper import MCPWrapper


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: run_request.py REQUEST_JSON RESULT_JSON")

    request_path = Path(sys.argv[1]).resolve()
    result_path = Path(sys.argv[2]).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))

    api_key = os.environ.pop("REPO2MCP_API_KEY", None)
    llm = LLMConfig(
        model=request["model"],
        api_key=api_key,
        temperature=float(request.get("temperature", 0)),
        max_tokens=int(request.get("max_tokens", 8192)),
        reasoning_enabled=request.get("reasoning_enabled", False),
    )
    agent = AgentConfig(
        analysis_steps=int(request.get("analysis_steps", 15)),
        generation_steps=int(request.get("generation_steps", 20)),
        fix_steps=int(request.get("fix_steps", 15)),
        max_fix_retries=int(request.get("max_fix_retries", 3)),
        verbose=bool(request.get("verbose", True)),
    )
    wrapper = MCPWrapper(
        llm_config=llm,
        output_dir=request["output_dir"],
        workspace_base=request["workspace_base"],
        agent_config=agent,
    )
    result = wrapper.run(
        repo_url="local://uploaded-repository",
        wrap_intent=request["wrap_intent"],
        sample_id=request["sample_id"],
        stop_after_analysis=bool(request.get("analysis_only", False)),
        tool_design_override=request.get("tool_design"),
    )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
