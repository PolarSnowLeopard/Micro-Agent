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
    # 关联标识
    evidence_id: str
    session_id: str
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

    # 草稿状态说明
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def build_config_attachment_draft(
    bundle: TraceEvidenceBundle,
    card: EvidenceCard,
) -> ConfigAttachmentDraft:
    """从证据包和卡片构建配置挂载草稿"""

    # 从 tool_calls 构建 dispatch_sequence
    dispatch_sequence = []
    seen_tools = set()
    for tc in bundle.tool_calls:
        if tc.direction == "call" and tc.tool_name not in seen_tools:
            if tc.service_id in ('internal', 'unresolved'):
                continue
            seen_tools.add(tc.tool_name)
            dispatch_sequence.append({
                "tool": tc.tool_name,
                "service_id": tc.service_id,
                "evidence_source": "log_text_extraction",
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
        "tool_call arguments/results not available in current trace format.",
        "Use evidence_id to trace back to full evidence bundle.",
    ]

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
        notes=notes,
    )
