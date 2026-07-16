"""配置管理：TOML 文件 + 环境变量覆盖。

优先级：环境变量 > TOML 文件 > 默认值。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass
class LLMConfig:
    model: str = "deepseek/deepseek-chat"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 8192
    timeout: int = 60
    num_retries: int = 3
    reasoning_enabled: Optional[bool] = None


@dataclass
class AgentConfig:
    max_steps: int = 30
    max_observe: int = 10000
    duplicate_threshold: int = 2


@dataclass
class MemoryConfig:
    storage_dir: str = "data/memory"


@dataclass
class RAGConfig:
    embedding_model: str = "text-embedding-3-small"
    chunk_size: int = 500


@dataclass
class SkillsConfig:
    directory: str = "skills"


@dataclass
class PlatformConfig:
    ioeb_api_base_url: str = "http://127.0.0.1:5000/api"
    request_timeout: float = 10.0


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    llm_profiles: dict[str, LLMConfig] = field(default_factory=dict)
    agent: AgentConfig = field(default_factory=AgentConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    platform: PlatformConfig = field(default_factory=PlatformConfig)
    workspace: Path = field(default_factory=lambda: PROJECT_ROOT / "workspace")

    def get_llm(self, profile: str = "default") -> LLMConfig:
        """获取指定 profile 的 LLM 配置。未找到则回退到 default。"""
        return self.llm_profiles.get(profile, self.llm)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> AppConfig:
        if config_path is None:
            user_cfg = CONFIG_DIR / "config.toml"
            config_path = user_cfg if user_cfg.exists() else CONFIG_DIR / "default.toml"
        data: dict = {}
        if config_path.exists():
            with open(config_path, "rb") as f:
                data = tomllib.load(f)

        cfg = cls()
        llm_data = data.get("llm", {})

        # 支持两种格式：
        #   旧: [llm] model = "..."（单 profile）
        #   新: [llm.default] model = "..." + [llm.fast] model = "..."（多 profile）
        if llm_data and all(isinstance(v, dict) for v in llm_data.values()):
            for name, profile_data in llm_data.items():
                profile_cfg = LLMConfig()
                _apply_dict(profile_cfg, profile_data)
                cfg.llm_profiles[name] = profile_cfg
            if "default" in cfg.llm_profiles:
                cfg.llm = cfg.llm_profiles["default"]
        else:
            _apply_dict(cfg.llm, llm_data)
            cfg.llm_profiles["default"] = cfg.llm

        _apply_dict(cfg.agent, data.get("agent", {}))
        _apply_dict(cfg.memory, data.get("memory", {}))
        _apply_dict(cfg.rag, data.get("rag", {}))
        _apply_dict(cfg.skills, data.get("skills", {}))
        _apply_dict(cfg.platform, data.get("platform", {}))

        # 环境变量覆盖 default profile
        _apply_env(cfg.llm, "model", "LLM_MODEL")
        _apply_env(cfg.llm, "api_key", "LLM_API_KEY")
        _apply_env(cfg.llm, "base_url", "LLM_BASE_URL")
        _apply_env(cfg.llm, "temperature", "LLM_TEMPERATURE", float)
        _apply_env(cfg.llm, "max_tokens", "LLM_MAX_TOKENS", int)
        _apply_env(
            cfg.llm,
            "reasoning_enabled",
            "LLM_REASONING_ENABLED",
            _parse_bool,
        )
        _apply_env(cfg.agent, "max_steps", "AGENT_MAX_STEPS", int)
        _apply_env(cfg.platform, "ioeb_api_base_url", "IOEB_API_BASE_URL")
        _apply_env(cfg.platform, "request_timeout", "IOEB_REQUEST_TIMEOUT", float)

        # 非 default profile 若未单独配置 api_key/base_url，则继承 default
        for name, prof in cfg.llm_profiles.items():
            if name == "default":
                continue
            if not prof.api_key and cfg.llm.api_key:
                prof.api_key = cfg.llm.api_key
            if not prof.base_url and cfg.llm.base_url:
                prof.base_url = cfg.llm.base_url

        ws = os.getenv("WORKSPACE_ROOT")
        if ws:
            cfg.workspace = Path(ws)

        return cfg


def _apply_dict(obj: object, d: dict) -> None:
    for k, v in d.items():
        if hasattr(obj, k):
            setattr(obj, k, v)


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无效布尔值: {value}")


def _apply_env(
    obj: object, attr: str, env_key: str, cast: type = str
) -> None:
    val = os.getenv(env_key)
    if val:
        setattr(obj, attr, cast(val))


config = AppConfig.load()
