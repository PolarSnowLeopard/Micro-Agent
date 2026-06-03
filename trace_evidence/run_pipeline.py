#!/usr/bin/env python3
"""P0-⑥ Trace Evidence Pipeline Runner

一键对已有 trace 执行完整的后处理:
1. 从 trace JSON 提取结构化证据 (adapter)
2. 生成 evidence card (JSON + Markdown)
3. 生成 config attachment draft (JSON)
4. 运行 evidence checker (JSON + Markdown report)
5. 输出汇总

Usage:
    python run_pipeline.py <trace.json> [--output-dir <dir>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 确保 trace_evidence/ 在 PATH 中
sys.path.insert(0, str(Path(__file__).parent))

from trace_adapter import TraceEvidenceAdapter
from evidence_card import build_evidence_card, render_evidence_card_markdown
from config_attachment import build_config_attachment_draft
from evidence_checker import EvidenceChecker, render_checker_report_markdown


def main():
    parser = argparse.ArgumentParser(description="Trace Evidence Pipeline")
    parser.add_argument("trace", help="Path to trace JSON file")
    parser.add_argument("--output-dir", "-o", help="Output directory (default: alongside trace)")
    args = parser.parse_args()

    trace_path = Path(args.trace)
    if not trace_path.exists():
        print(f"ERROR: Trace file not found: {trace_path}")
        sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else trace_path.parent / "evidence_output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Clean stale outputs from previous runs of the same trace
    trace_prefix = trace_path.stem.replace("sim-", "")[:6]  # e.g. "b963f6"
    stale = [f for f in output_dir.iterdir() if f.name.startswith(f"ev-{trace_prefix}") or f.name.startswith(f"{trace_path.stem}_")]
    if stale:
        for f in stale:
            f.unlink()
        print(f"  Cleaned {len(stale)} stale output(s) from previous run")
        print()

    print(f"{'='*60}")
    print(f"  Trace Evidence Pipeline v0")
    print(f"{'='*60}")
    print(f"  Input:  {trace_path}")
    print(f"  Output: {output_dir}")
    print()

    # Step 1: Extract evidence bundle
    print("[1/4] Extracting evidence bundle...")
    adapter = TraceEvidenceAdapter.from_file(str(trace_path))
    bundle = adapter.extract()
    bundle_path = output_dir / f"{bundle.session_id}_bundle.json"
    bundle_path.write_text(
        json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"      → {len(bundle.tool_calls)} tool events, {len(bundle.services)} services")
    print(f"      → Bundle: {bundle_path.name}")

    # Step 2: Generate evidence card
    print("[2/4] Generating evidence card...")
    card = build_evidence_card(bundle)
    card_json_path = output_dir / f"{card.evidence_id}.json"
    card_md_path = output_dir / f"{card.evidence_id}.md"
    card_json_path.write_text(card.to_json(), encoding="utf-8")
    card_md_path.write_text(render_evidence_card_markdown(card), encoding="utf-8")
    print(f"      → Evidence ID: {card.evidence_id}")
    print(f"      → Card JSON: {card_json_path.name}")
    print(f"      → Card MD:   {card_md_path.name}")

    # Step 3: Generate config attachment draft
    print("[3/4] Generating config attachment draft...")
    draft = build_config_attachment_draft(bundle, card)
    draft_path = output_dir / f"{card.evidence_id}_config_draft.json"
    draft_path.write_text(draft.to_json(), encoding="utf-8")
    print(f"      → Draft: {draft_path.name}")
    print(f"      → Dispatch sequence: {len(draft.dispatch_sequence)} tools")

    # Step 4: Run checker
    print("[4/4] Running evidence checker...")
    checker = EvidenceChecker(bundle, card)
    report = checker.run_all()
    report_json_path = output_dir / f"{card.evidence_id}_checker_report.json"
    report_md_path = output_dir / f"{card.evidence_id}_checker_report.md"
    report_json_path.write_text(report.to_json(), encoding="utf-8")
    report_md_path.write_text(render_checker_report_markdown(report), encoding="utf-8")
    print(f"      → Overall: {report.overall_status}")
    print(f"      → Report JSON: {report_json_path.name}")
    print(f"      → Report MD:   {report_md_path.name}")

    # Summary
    print()
    print(f"{'='*60}")
    print(f"  PIPELINE COMPLETE")
    print(f"{'='*60}")
    print(f"  Evidence ID:    {card.evidence_id}")
    print(f"  Fingerprint:    {card.evidence_fingerprint[:16]}...")
    print(f"  Session:        {bundle.session_id}")
    print(f"  App:            {bundle.app_name}")
    print(f"  Overall Check:  {report.overall_status}")
    print(f"  Evidence Gaps:  {len(bundle.missing_evidence)}")
    if bundle.missing_evidence:
        for gap in bundle.missing_evidence:
            print(f"    - {gap}")
    print(f"  Output Files:   {len(list(output_dir.iterdir()))}")
    print(f"  Output Dir:     {output_dir}")
    print(f"{'='*60}")

    return 0 if report.overall_status != "FAIL" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, TypeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
