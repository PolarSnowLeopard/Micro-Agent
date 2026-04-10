"""Skill 系统：可复用的 Agent 能力模块。

Skill = prompt 片段 + 工具集合 + 配置。
Agent 加载 Skill 后，会将其 prompt 注入 system_prompt，工具注册到 ToolRegistry。

设计目标：
- 一个 Skill 可以被多个 Agent 加载
- Skill 之间可以组合（一个 Agent 加载多个 Skill）
- 支持从目录自动发现（SKILL.md / skill.toml）

用法：
    skill = Skill(
        name="code_review",
        description="代码审查能力",
        prompt_fragment="你需要遵循以下代码审查规范：...",
        tools=[MyLintTool()],
    )
    SkillRegistry.register(skill)

    # Agent 加载
    agent.load_skill("code_review")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from micro_agent.tool.base import Tool


@dataclass
class Skill:
    """技能定义。"""

    name: str
    description: str = ""
    prompt_fragment: str = ""
    tools: list[Tool] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_directory(cls, path: Path) -> Optional[Skill]:
        """从目录加载 Skill。目录需包含 skill.toml 或 SKILL.md。"""
        toml_path = path / "skill.toml"
        md_path = path / "SKILL.md"

        if toml_path.exists():
            import tomllib
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
            return cls(
                name=data.get("name", path.name),
                description=data.get("description", ""),
                prompt_fragment=data.get("prompt", ""),
                metadata=data.get("metadata", {}),
            )

        if md_path.exists():
            content = md_path.read_text(encoding="utf-8")
            return cls(
                name=path.name,
                description=f"从 {path.name}/SKILL.md 加载",
                prompt_fragment=content,
            )

        return None


class SkillRegistry:
    """全局技能注册表。"""

    _skills: dict[str, Skill] = {}

    @classmethod
    def register(cls, skill: Skill) -> None:
        cls._skills[skill.name] = skill
        logger.debug(f"Skill 已注册: {skill.name}")

    @classmethod
    def get(cls, name: str) -> Optional[Skill]:
        return cls._skills.get(name)

    @classmethod
    def list_skills(cls) -> list[str]:
        return list(cls._skills.keys())

    @classmethod
    def discover(cls, skills_dir: Path) -> int:
        """从目录自动发现并注册 Skill。返回发现数量。"""
        if not skills_dir.exists():
            return 0
        count = 0
        for child in skills_dir.iterdir():
            if child.is_dir():
                skill = Skill.from_directory(child)
                if skill:
                    cls.register(skill)
                    count += 1
        return count

    @classmethod
    def clear(cls) -> None:
        cls._skills.clear()
