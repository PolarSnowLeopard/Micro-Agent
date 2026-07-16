"""Agent-driven MCP packaging pipeline.

The modules in this package deliberately separate deterministic guardrails from
semantic decisions: static analysis supplies evidence, while an Agent decides
service boundaries and MCP capabilities.
"""

from micro_agent.packaging.analyzer import RepositoryAnalyzer, RepositoryIR
from micro_agent.packaging.models import PackagingPlan, PlanValidationError

__all__ = [
    "PackagingPlan",
    "PlanValidationError",
    "RepositoryAnalyzer",
    "RepositoryIR",
]
