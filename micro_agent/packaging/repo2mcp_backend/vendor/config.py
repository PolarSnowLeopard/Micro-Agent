"""配置管理"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# 克隆与 Docker 构建工作区根目录（每个样本为其子目录）。可直接改这里，或用环境变量 REPO2MCP_WORKSPACE_BASE 覆盖（也支持写在 .env）
REPO2MCP_WORKSPACE_BASE_DEFAULT = "~/data/repo2mcp_workspace"


def default_workspace_base() -> str:
    env = os.getenv("REPO2MCP_WORKSPACE_BASE")
    if env:
        return str(Path(env).expanduser().resolve())
    return str(Path(REPO2MCP_WORKSPACE_BASE_DEFAULT).expanduser().resolve())


@dataclass
class LLMConfig:
    """LLM 配置"""
    model: str = "openrouter/openai/gpt-4o-2024-05-13"
    sub_agent_model: Optional[str] = os.getenv("SUB_AGENT_MODEL")
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    temperature: float = 0
    max_tokens: int = 4096
    reasoning_enabled: Optional[bool] = None
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("LLM_MAX_RETRIES", "5"))
    )
    retry_base_seconds: float = field(
        default_factory=lambda: float(os.getenv("LLM_RETRY_BASE_SECONDS", "2"))
    )
    retry_max_seconds: float = field(
        default_factory=lambda: float(os.getenv("LLM_RETRY_MAX_SECONDS", "60"))
    )

    def __post_init__(self):
        if self.reasoning_enabled is None:
            raw_reasoning = os.getenv("LLM_REASONING_ENABLED")
            if raw_reasoning:
                normalized = raw_reasoning.strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    self.reasoning_enabled = True
                elif normalized in {"0", "false", "no", "off"}:
                    self.reasoning_enabled = False
        if self.model.startswith("openrouter/"):
            if self.api_key is None:
                self.api_key = os.getenv("OPENROUTER_API_KEY")
            self.api_base = None
        else:
            if self.api_key is None:
                self.api_key = os.getenv("PRIVATE_API_KEY")
            if self.api_base is None:
                self.api_base = os.getenv("PRIVATE_API_BASE")


@dataclass
class SandboxConfig:
    """沙箱配置（workspace_base 默认见本文件 REPO2MCP_WORKSPACE_BASE_DEFAULT）"""
    workspace_base: str = field(default_factory=default_workspace_base)
    timeout: int = 1200


@dataclass
class AgentConfig:
    """Agent 配置"""
    analysis_steps: int = 15
    generation_steps: int = 20
    fix_steps: int = 15
    max_fix_retries: int = 3
    verbose: bool = True


@dataclass
class Config:
    """全局配置"""
    llm: LLMConfig = field(default_factory=LLMConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)


default_config = Config()
