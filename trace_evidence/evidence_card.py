"""P0-③ Evidence Card Generator

从 TraceEvidenceBundle 生成 evidence_card（JSON + Markdown）。
Evidence Card 是一次仿真运行的结构化摘要卡片，包含:
- 运行元信息 (session_id, app_name, domain, timestamps)
- 证据统计 (tool_calls数量, services数, phases)
- 缺失标注 (missing_evidence)
- 证据指纹 (用于后续挂载到config)
"""

from __future__ import annotations

import hashlib
import json

try:
    from .sanitize import sanitize_md_cell, sanitize_identifier, sanitize_md_block, sanitize_md_inline, redact_secrets
    from .trace_adapter import TraceEvidenceAdapter, TraceEvidenceBundle
except ImportError:
    from sanitize import sanitize_md_cell, sanitize_identifier, sanitize_md_block, sanitize_md_inline, redact_secrets
    from trace_adapter import TraceEvidenceAdapter, TraceEvidenceBundle
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class EvidenceCard:
    """结构化证据卡片"""
    # 标识
    evidence_id: str
    session_id: str
    generated_at: str

    # 元信息
    app_name: str
    domain: str
    mode: str
    strategy: dict

    # 证据摘要
    summary: dict = field(default_factory=dict)

    # 时间线
    timeline: dict = field(default_factory=dict)

    # 工具调用摘要
    tool_call_summary: list[dict] = field(default_factory=list)

    # 服务发现
    services_discovered: list[dict] = field(default_factory=list)

    # 验证结果
    verification: dict = field(default_factory=dict)

    # 完成状态
    completion: dict = field(default_factory=dict)

    # 证据缺口
    missing_evidence: list[str] = field(default_factory=list)

    # 详细工具调用时间线 (用于 debrief)
    tool_call_details: list[dict] = field(default_factory=list)

    # Planner/Agent 思考过程
    planner_events: list[dict] = field(default_factory=list)

    # §7 证据溯源注解
    provenance: dict = field(default_factory=dict)

    # 证据指纹 (sha256 of bundle content)
    evidence_fingerprint: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["schema_version"] = "1.0.0"
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def generate_evidence_id(session_id: str, bundle: TraceEvidenceBundle) -> str:
    """生成唯一 evidence_id: ev-{session_short}-{hash8}"""
    content = json.dumps(bundle.to_dict(), sort_keys=True, ensure_ascii=False)
    h = hashlib.sha256(content.encode()).hexdigest()[:8]
    session_short = session_id.split("-")[-1][:6] if "-" in session_id else session_id[:6]
    return f"ev-{session_short}-{h}"


def compute_fingerprint(bundle: TraceEvidenceBundle) -> str:
    """计算证据包指纹"""
    content = json.dumps(bundle.to_dict(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()


def build_evidence_card(bundle: TraceEvidenceBundle) -> EvidenceCard:
    """从 evidence bundle 构建 evidence card"""
    evidence_id = generate_evidence_id(bundle.session_id, bundle)

    # 时间线
    timestamps = []
    if bundle.services:
        timestamps.append(bundle.services[0].timestamp)
    if bundle.completion:
        timestamps.append(bundle.completion.timestamp)
    for tc in bundle.tool_calls:
        timestamps.append(tc.timestamp)

    timeline = {}
    if timestamps:
        start_ts = min(timestamps)
        end_ts = max(timestamps)
        timeline = {
            "start": start_ts,
            "end": end_ts,
            "duration_sec": round(end_ts - start_ts, 2),
            "start_iso": datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
            "end_iso": datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
        }

    # 工具调用摘要: 按 service 聚合
    from collections import Counter
    call_events = [tc for tc in bundle.tool_calls if tc.direction == "call"]
    service_calls = Counter(tc.service_id for tc in call_events)
    tool_call_summary = [
        {"service_id": sid, "call_count": cnt}
        for sid, cnt in service_calls.most_common()
    ]

    # Exclude internal pseudo-services from "unique services called" count
    external_service_count = sum(1 for sid in service_calls if sid != "internal")

    # 服务发现
    services_discovered = [
        {
            "service_id": s.service_id,
            "status": s.status,
            "channel": s.channel,
            "tool_count": len(s.tools),
            "latency_ms": s.latency_ms,
        }
        for s in bundle.services
    ]

    # 验证
    verification = {}
    if bundle.verification:
        verification = {
            "status": bundle.verification.status,
            "reason": bundle.verification.reason,
            "has_raw_text": bundle.verification.raw_text is not None,
            "missing": bundle.verification.missing_evidence,
        }

    # 完成状态
    completion = {}
    if bundle.completion:
        completion = {
            "success": bundle.completion.success,
            "iterations": bundle.completion.iterations,
            "elapsed_ms": bundle.completion.elapsed_ms,
            "execution_path": bundle.completion.execution_path,
        }

    # 摘要统计
    summary = {
        "total_tool_calls": len(call_events),
        "total_tool_returns": len([tc for tc in bundle.tool_calls if tc.direction == "return"]),
        "unique_services_called": external_service_count,
        "services_discovered": len(bundle.services),
        "planner_thoughts": len(bundle.planner_thoughts),
        "phases_recorded": len(bundle.phases),
        "iterations_recorded": len(bundle.iterations),
        "evidence_gaps": len(bundle.missing_evidence),
    }

    # §7 证据溯源注解: 统计 source/confidence 分布
    source_counter: Counter = Counter()
    confidence_counter: Counter = Counter()
    channel_counter: Counter = Counter()
    total_evidence_items = 0

    for tc in bundle.tool_calls:
        source_counter[tc.source] += 1
        confidence_counter[tc.confidence] += 1
        channel_counter[tc.channel] += 1
        total_evidence_items += 1
    for s in bundle.services:
        source_counter[s.source] += 1
        confidence_counter[s.confidence] += 1
        channel_counter[s.channel] += 1
        total_evidence_items += 1
    for p in bundle.phases:
        source_counter[p.source] += 1
        confidence_counter[p.confidence] += 1
        total_evidence_items += 1
    for it in bundle.iterations:
        source_counter[it.source] += 1
        confidence_counter[it.confidence] += 1
        total_evidence_items += 1
    if bundle.verification:
        source_counter[bundle.verification.source] += 1
        confidence_counter[bundle.verification.confidence] += 1
        total_evidence_items += 1
    if bundle.completion:
        source_counter[bundle.completion.source] += 1
        confidence_counter[bundle.completion.confidence] += 1
        total_evidence_items += 1
    for pt in bundle.planner_thoughts:
        source_counter[pt.source] += 1
        confidence_counter[pt.confidence] += 1
        total_evidence_items += 1

    original_pct = round(100 * confidence_counter.get("original", 0) / max(total_evidence_items, 1))
    inferred_pct = round(100 * confidence_counter.get("inferred", 0) / max(total_evidence_items, 1))
    # Determine if this is a log-parsed trace (most evidence is inferred, which is expected)
    is_log_parsed = (source_counter.get("inferred_from_log", 0) + source_counter.get("log_parsed", 0)) > total_evidence_items * 0.5
    if is_log_parsed:
        # For log-parsed traces, high inferred % is normal and expected
        provenance_note = (
            "good — fully reconstructed from execution logs" if inferred_pct >= 70 else
            "adequate — partially reconstructed from logs" if inferred_pct >= 40 else
            "mixed — some original metadata available"
        )
    else:
        provenance_note = (
            "high — most evidence from original metadata" if original_pct >= 70 else
            "medium" if original_pct >= 40 else
            "low — limited original metadata"
        )
    provenance = {
        "total_evidence_items": total_evidence_items,
        "source_distribution": dict(source_counter.most_common()),
        "confidence_distribution": dict(confidence_counter.most_common()),
        "channel_distribution": dict(channel_counter.most_common()),
        "original_confidence_pct": original_pct,
        "is_log_parsed": is_log_parsed,
        "provenance_note": provenance_note,
    }

    # 详细工具调用时间线 (只展示 call 方向, 按时间排序)
    tool_call_details = []
    for tc in sorted(call_events, key=lambda x: x.timestamp or 0):
        detail = {
            "tool_name": tc.tool_name,
            "service_id": tc.service_id,
            "channel": tc.channel,
            "timestamp_iso": (
                datetime.fromtimestamp(tc.timestamp, tz=timezone.utc).strftime("%H:%M:%S")
                if tc.timestamp else "N/A"
            ),
            "latency_ms": tc.latency_ms,
            "has_result": tc.result is not None,
            "has_error": tc.error is not None,
            "source": tc.source,
        }
        tool_call_details.append(detail)

    # Planner/Agent 思考事件
    planner_events = []
    for pt in bundle.planner_thoughts:
        # 截取前 200 字符作为摘要
        content_preview = redact_secrets((pt.content or "")[:200])
        if len(pt.content or "") > 200:
            content_preview += "..."
        planner_events.append({
            "iteration": pt.iteration,
            "timestamp_iso": (
                datetime.fromtimestamp(pt.timestamp, tz=timezone.utc).strftime("%H:%M:%S")
                if pt.timestamp else "N/A"
            ),
            "preview": content_preview,
        })

    return EvidenceCard(
        evidence_id=evidence_id,
        session_id=bundle.session_id,
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        app_name=bundle.app_name,
        domain=bundle.domain,
        mode=bundle.mode,
        strategy=bundle.strategy,
        summary=summary,
        timeline=timeline,
        tool_call_summary=tool_call_summary,
        services_discovered=services_discovered,
        verification=verification,
        completion=completion,
        missing_evidence=bundle.missing_evidence,
        tool_call_details=tool_call_details,
        planner_events=planner_events,
        provenance=provenance,
        evidence_fingerprint=compute_fingerprint(bundle),
    )


def render_evidence_card_markdown(card: EvidenceCard) -> str:
    """渲染 evidence card 为 Markdown"""
    lines = [
        f"# Evidence Card: {card.evidence_id}",
        "",
        f"**Session**: `{card.session_id}`  ",
        f"**App**: {card.app_name}  ",
        f"**Domain**: {card.domain} | **Mode**: {card.mode}  ",
        f"**Evidence Generated**: {card.generated_at}  ",
        f"**Fingerprint**: `{card.evidence_fingerprint}`",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
    ]

    for k, v in card.summary.items():
        lines.append(f"| {sanitize_md_cell(k.replace('_', ' ').title())} | {sanitize_md_cell(str(v))} |")

    lines.extend([
        "",
        "## Timeline",
        "",
    ])
    if card.timeline:
        lines.append(f"- **Start**: {card.timeline.get('start_iso', 'N/A')}")
        lines.append(f"- **End**: {card.timeline.get('end_iso', 'N/A')}")
        lines.append(f"- **Duration**: {card.timeline.get('duration_sec', 0)}s")

    lines.extend([
        "",
        "## Services Discovered",
        "",
        "| Service | Status | Channel | Tools | Latency |",
        "|---------|--------|---------|-------|---------|",
    ])
    for s in card.services_discovered:
        lines.append(f"| {sanitize_md_cell(s['service_id'], max_len=40)} | {sanitize_md_cell(s['status'])} | {sanitize_md_cell(s['channel'])} | {s['tool_count']} | {s['latency_ms']}ms |")

    lines.extend([
        "",
        "## Tool Call Summary (by service)",
        "",
        "| Service | Calls |",
        "|---------|-------|",
    ])
    visible_calls = 0
    for tc in card.tool_call_summary:
        if tc['service_id'] in ('internal', 'unresolved'):
            continue
        lines.append(f"| {sanitize_md_cell(tc['service_id'], max_len=40)} | {tc['call_count']} |")
        visible_calls += tc['call_count']
    total_calls = card.summary.get('total_tool_calls', visible_calls)
    if total_calls > visible_calls:
        lines.append("")
        lines.append(f"*{total_calls - visible_calls} additional internal/system call(s) not shown.*")

    # Detailed tool call timeline for run debrief
    if card.tool_call_details:
        # Determine which optional columns have data
        has_latency = any(tc.get('latency_ms') for tc in card.tool_call_details)
        has_result = any(tc.get('has_result') or tc.get('has_error') for tc in card.tool_call_details)

        # Build adaptive header
        hdr_cols = ["#", "Time", "Service", "Tool"]
        sep_cols = ["---", "------", "---------", "------"]
        if has_latency:
            hdr_cols.append("Latency")
            sep_cols.append("---------")
        if has_result:
            hdr_cols.append("Result")
            sep_cols.append("--------")
        hdr_cols.append("Source")
        sep_cols.append("--------")

        lines.extend([
            "",
            "## 🔧 Tool Call Timeline",
            "",
            "| " + " | ".join(hdr_cols) + " |",
            "| " + " | ".join(sep_cols) + " |",
        ])
        for i, tc in enumerate(card.tool_call_details, 1):
            # Show only HH:MM:SS from ISO timestamp for readability
            ts_iso = tc.get('timestamp_iso', '?')
            time_short = ts_iso[11:19] if len(ts_iso) >= 19 else ts_iso

            row = [
                str(i),
                time_short,
                sanitize_md_cell(tc.get('service_id', '?'), max_len=35),
                f"`{sanitize_md_cell(tc.get('tool_name', '?'), max_len=60)}`",
            ]
            if has_latency:
                lat = f"{tc.get('latency_ms')}ms" if tc.get('latency_ms') else "—"
                row.append(lat)
            if has_result:
                status = "✅" if tc.get("has_result") else ("❌" if tc.get("has_error") else "—")
                row.append(status)
            row.append(tc.get('source', '?'))

            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        lines.append(f"*{len(card.tool_call_details)} tool calls recorded.*")

    lines.extend([
        "",
        "## Verification",
        "",
        f"- **Status**: {card.verification.get('status', 'N/A')}",
    ])
    reason = card.verification.get("reason")
    if reason:
        lines.append(f"- **Reason**: {sanitize_md_inline(reason)}")
    else:
        lines.append("- **Reason**: not available")
    if card.verification.get("missing"):
        lines.append(f"- **Missing**: {', '.join(sanitize_md_cell(m, max_len=60) for m in card.verification['missing'])}")

    lines.extend([
        "",
        "## Completion",
        "",
        f"- **Success**: {card.completion.get('success', 'N/A')}",
        f"- **Iterations**: {card.completion.get('iterations', 'N/A')}",
        f"- **Elapsed**: {card.completion.get('elapsed_ms', 'N/A')}ms",
    ])
    if card.completion.get("execution_path"):
        safe_path = [sanitize_md_cell(p, max_len=60) for p in card.completion['execution_path']]
        lines.append(f"- **Path**: {' → '.join(safe_path)}")

    # Planner/Agent reasoning events for debrief
    if card.planner_events:
        lines.extend([
            "",
            "## 🧠 Planner Events",
            "",
        ])
        for pe in card.planner_events:
            iter_label = f"Iteration {pe.get('iteration', '?')}" if pe.get('iteration') else "Init"
            # Show only HH:MM:SS for readability
            ts_full = pe.get('timestamp_iso', '')
            time_label = ts_full[11:19] if len(ts_full) >= 19 else ts_full
            lines.append(f"### {iter_label} — {time_label}")
            lines.append("")
            # Render the preview as a blockquote; use generous limit for debrief value
            preview = sanitize_md_block(pe.get('preview', ''), max_len=500)
            for pline in preview.split('\n'):
                lines.append(f"> {pline}")
            lines.append("")

    if card.missing_evidence:
        lines.extend([
            "",
            "## ⚠️ Evidence Gaps",
            "",
        ])
        for gap in card.missing_evidence:
            lines.append(f"- `{gap}`")

    # §7 Provenance annotations
    if card.provenance:
        lines.extend([
            "",
            "## 📋 Provenance",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Total Evidence Items | {card.provenance.get('total_evidence_items', 'N/A')} |",
            f"| Original Metadata % | {card.provenance.get('original_confidence_pct', 'N/A')}% |",
            f"| Reconstruction Quality | {card.provenance.get('provenance_note', 'N/A')} |",
            "",
            "**Source Distribution**:",
            "",
        ])
        for src, cnt in card.provenance.get("source_distribution", {}).items():
            lines.append(f"- `{src}`: {cnt}")
        lines.extend(["", "**Channel Distribution**:", ""])
        for ch, cnt in card.provenance.get("channel_distribution", {}).items():
            lines.append(f"- `{ch}`: {cnt}")

    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    trace_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not trace_path:
        print("Usage: python evidence_card.py <trace.json>")
        sys.exit(1)

    adapter = TraceEvidenceAdapter.from_file(trace_path)
    bundle = adapter.extract()
    card = build_evidence_card(bundle)

    out_dir = Path(trace_path).parent
    card_json_path = out_dir / f"{card.evidence_id}.json"
    card_md_path = out_dir / f"{card.evidence_id}.md"

    card_json_path.write_text(card.to_json(), encoding="utf-8")
    card_md_path.write_text(render_evidence_card_markdown(card), encoding="utf-8")

    print(f"Evidence card generated:")
    print(f"  JSON: {card_json_path}")
    print(f"  MD:   {card_md_path}")
