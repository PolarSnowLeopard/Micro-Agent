"""Skill 系统：可复用的 Agent 能力模块。

Skill = prompt 片段 + 工具集合 + 配置。
Agent 加载 Skill 后，会将其 prompt 注入 system_prompt，工具注册到 ToolRegistry。

设计目标：
- 一个 Skill 可以被多个 Agent 加载
- Skill 之间可以组合（一个 Agent 加载多个 Skill）
- 支持从目录自动发现（SKILL.md / skill.toml）
- skill.toml 提供结构化元数据（match 条件等），SKILL.md 提供 prompt 内容
  两者可组合使用：toml 声明元数据 + md 提供长文本 prompt

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

    # 按任务参数自动匹配 Skill
    matched = SkillRegistry.find_matching(
        task_name="aml_auto_generate",
        category="classification",
        input_types=["video"],
        text_context="行为识别 arm_flapping",
    )
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
        """从目录加载 Skill。

        支持三种模式：
        1. skill.toml + SKILL.md：toml 提供 name/description/metadata，md 提供 prompt
        2. 仅 skill.toml：toml 中 prompt 字段提供 prompt 内容
        3. 仅 SKILL.md：向后兼容，md 内容作为 prompt
        """
        toml_path = path / "skill.toml"
        md_path = path / "SKILL.md"

        if toml_path.exists():
            import tomllib
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
            prompt = data.get("prompt", "")
            if not prompt and md_path.exists():
                prompt = md_path.read_text(encoding="utf-8")
            return cls(
                name=data.get("name", path.name),
                description=data.get("description", ""),
                prompt_fragment=prompt,
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
    def find_matching(
        cls,
        *,
        task_name: str = "",
        category: str = "",
        input_types: list[str] | None = None,
        labels: list[str] | None = None,
        text_context: str = "",
    ) -> list[str]:
        """根据任务参数自动匹配适用的 Skill。

        遍历所有已注册 Skill 的 metadata.match 配置，满足任一条件即命中。

        Args:
            task_name: 任务标识（如 "aml_auto_generate"），用于匹配 always_for。
            category: 算法类别（如 "classification"），匹配 categories。
            input_types: 用户选择的输入数据类型，匹配 input_types。
            labels: 用户定义的标签列表，纳入 text_context 匹配。
            text_context: 拼接了 model_name + narrative + labels 等的文本，
                          用于关键词匹配。

        Returns:
            匹配到的 Skill 名称列表（去重、保序）。
        """
        matched: list[str] = []
        user_inputs = set(s.lower() for s in (input_types or []))
        ctx_lower = text_context.lower()

        for skill in cls._skills.values():
            match_cfg = skill.metadata.get("match", {})
            if not match_cfg:
                continue

            always_for = match_cfg.get("always_for") or []
            if task_name and task_name in always_for:
                matched.append(skill.name)
                continue

            skill_categories = match_cfg.get("categories") or []
            if category and category in skill_categories:
                matched.append(skill.name)
                continue

            skill_input_types = set(
                s.lower() for s in (match_cfg.get("input_types") or [])
            )
            if user_inputs and skill_input_types & user_inputs:
                matched.append(skill.name)
                continue

            keywords = match_cfg.get("keywords") or []
            if ctx_lower and any(kw.lower() in ctx_lower for kw in keywords):
                matched.append(skill.name)
                continue

        return matched

    @classmethod
    def clear(cls) -> None:
        cls._skills.clear()
