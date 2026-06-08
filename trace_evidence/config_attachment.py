"""P0-④ Config Attachment Draft Generator

生成带 evidence_id 的元应用配置草稿。
这不是完整的配置产物——只是一个"配置挂载点"，证明 trace evidence 可以
被关联到元应用配置中。

输出: config_attachment_draft.json，包含:
- evidence_id 引用
- 从 trace 提取的配置参数 (executionPath, toolChannels, strategy)
- 草稿状态标记 (draft: true)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .trace_adapter import TraceEvidenceBundle
    from .evidence_card import EvidenceCard
except ImportError:
    from trace_adapter import TraceEvidenceBundle
    from evidence_card import EvidenceCard


@dataclass
class ConfigAttachmentDraft:
    """带 evidence_id 的配置草稿"""
    # 关联标识 (required)
    evidence_id: str
    session_id: str

    # 元数据
    schema_version: str = "1.0.0"
    draft: bool = True
    generated_at: str = ""

    # 从 trace 提取的配置参数
    app_name: str = ""
    domain: str = ""
    execution_path: list[str] = field(default_factory=list)
    tool_channels: list[dict] = field(default_factory=list)
    strategy: dict = field(default_factory=dict)

    # 运行时指标 (来自 evidence)
    runtime_metrics: dict = field(default_factory=dict)

    # 服务调度配置 (从 tool_call 顺序推断)
    dispatch_sequence: list[dict] = field(default_factory=list)

    # P0-⑤ executionEvidence 槽位: 打包执行证据供下游配置系统消费
    execution_evidence: dict = field(default_factory=dict)

    # 草稿状态说明
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Ensure schema_version is first key (convention matching evidence_card/checker)
        ordered = {"schema_version": d.pop("schema_version")}
        ordered.update(d)
        return ordered

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def build_config_attachment_draft(
    bundle: TraceEvidenceBundle,
    card: EvidenceCard,
    *,
    trace_path: str | None = None,
    evidence_card_path: str | None = None,
    checker_report_path: str | None = None,
    checker_status: str | None = None,
    quality: str | None = None,
) -> ConfigAttachmentDraft:
    """从证据包和卡片构建配置挂载草稿

    §8 executionEvidence 字段说明:
    - 路径/checker 状态由 pipeline 在写盘时注入 (bundle/card 自身不知道落盘位置)；
      未提供时相应字段记入 missingEvidenceIds，绝不伪造。
    - toolCallEvidenceIds 使用 trace 中真实的 call_id。
    - planner/verifier 事件无原生 id，使用按迭代派生的可追溯引用
      (planner_decision#iterN / verifier_result#iterN)，可在 trace["events"] 中核对。
    """

    # 从 tool_calls 构建 dispatch_sequence
    dispatch_sequence = []
    seen_tools = set()
    for tc in bundle.tool_calls:
        if tc.direction == "call" and tc.tool_name not in seen_tools:
            if tc.service_id in ('internal', 'unresolved'):
                continue
            seen_tools.add(tc.tool_name)
            ev_source = "structured_event"
            dispatch_sequence.append({
                "tool": tc.tool_name,
                "service_id": tc.service_id,
                "evidence_source": ev_source,
            })

    # 运行时指标
    runtime_metrics = {}
    if bundle.completion:
        runtime_metrics = {
            "iterations": bundle.completion.iterations,
            "elapsed_ms": bundle.completion.elapsed_ms,
            "success": bundle.completion.success,
        }

    # 注意事项
    notes = [
        "This is a DRAFT — not a deployable configuration.",
        f"Evidence gaps: {len(bundle.missing_evidence)} items.",
        "Use evidence_id to trace back to full evidence bundle.",
    ]

    # §8 executionEvidence: 供下游元应用配置系统消费的可追溯证据引用
    # toolCallEvidenceIds: trace 中真实存在的 call_id (call 方向，去重保序)
    tool_call_ids: list[str] = []
    _seen_calls: set[str] = set()
    for tc in bundle.tool_calls:
        cid = getattr(tc, "call_id", None)
        if cid and cid not in _seen_calls and tc.service_id not in ("internal", "unresolved"):
            _seen_calls.add(cid)
            tool_call_ids.append(cid)

    # planner/verifier 事件无原生 id -> 按迭代派生可追溯引用 (可在 trace.events 核对)
    planner_ids: list[str] = []
    for pt in getattr(bundle, "planner_thoughts", []) or []:
        it = getattr(pt, "iteration", None)
        ref = f"planner_decision#iter{it}" if it is not None else "planner_decision"
        if ref not in planner_ids:
            planner_ids.append(ref)
    verification_ids: list[str] = []
    _verif = getattr(bundle, "verification", None)
    if _verif and getattr(_verif, "status", "UNKNOWN") != "UNKNOWN":
        vit = getattr(_verif, "iteration", None)
        verification_ids.append(
            f"verifier_result#iter{vit}" if vit is not None else "verifier_result"
        )

    # 缺失项: 不造假 id, 明确列出哪些证据不可追溯
    missing_ids: list[str] = []
    if not tool_call_ids:
        missing_ids.append("toolCallEvidenceIds")
    if not planner_ids:
        missing_ids.append("plannerDecisionEvidenceIds")
    if not verification_ids:
        missing_ids.append("verificationEvidenceIds")
    # 路径/checker 状态由 pipeline 注入; 未提供则记缺失而非伪造
    if not trace_path:
        missing_ids.append("tracePath")
    if not evidence_card_path:
        missing_ids.append("evidenceCardPath")
    if not checker_report_path:
        missing_ids.append("checkerReportPath")

    execution_evidence = {
        "traceSessionId": bundle.session_id,
        "tracePath": trace_path,
        "evidenceCardPath": evidence_card_path,
        "checkerReportPath": checker_report_path,
        "evidenceId": card.evidence_id,
        "toolCallEvidenceIds": tool_call_ids,
        "plannerDecisionEvidenceIds": planner_ids,
        "verificationEvidenceIds": verification_ids,
        "missingEvidenceIds": missing_ids,
        "quality": quality,
        "checkerStatus": checker_status,
        # 辅助上下文 (非 §8 必填, 便于下游消费)
        "verdict": card.verification.get("status", "unknown") if card.verification else "unknown",
        "executionPath": bundle.completion.execution_path if bundle.completion else [],
        "toolChannels": bundle.completion.tool_channels if bundle.completion else [],
        "dispatchSequence": [
            {"tool": d["tool"], "serviceId": d["service_id"]}
            for d in dispatch_sequence
        ],
        "metrics": runtime_metrics,
        "generatedAt": datetime.now(tz=timezone.utc).isoformat(),
        "draft": True,
    }

    return ConfigAttachmentDraft(
        evidence_id=card.evidence_id,
        session_id=bundle.session_id,
        draft=True,
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        app_name=bundle.app_name,
        domain=bundle.domain,
        execution_path=bundle.completion.execution_path if bundle.completion else [],
        tool_channels=bundle.completion.tool_channels if bundle.completion else [],
        strategy=bundle.strategy,
        runtime_metrics=runtime_metrics,
        dispatch_sequence=dispatch_sequence,
        execution_evidence=execution_evidence,
        notes=notes,
    )
