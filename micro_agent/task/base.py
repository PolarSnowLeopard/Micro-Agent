"""任务配置与注册系统。

每个"任务"是一个 TaskConfig——描述用什么 prompt、什么 agent 参数来执行。
Prompt 用 Jinja2 模板管理，告别旧版 f-string 中的 {{ }} 转义地狱。

用法：
    from task import register_task, get_task, render_prompt, TaskConfig

    register_task(TaskConfig(
        name="code_analysis",
        prompt_template="code_analysis.md.j2",
        system_prompt="你是代码分析专家。",
    ))

    config = get_task("code_analysis")
    prompt = render_prompt(config.prompt_template, workspace="/tmp", main_code="main.py")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import jinja2

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "tasks" / "templates"

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
    undefined=jinja2.StrictUndefined,
    keep_trailing_newline=True,
)


@dataclass
class TaskConfig:
    """任务配置。一个 TaskConfig 对应一种 Agent 任务。"""

    name: str
    description: str = ""
    prompt_template: str = ""       # Jinja2 模板文件名或内联模板字符串
    system_prompt: str = ""
    next_step_prompt: str = ""
    max_steps: int = 30
    use_mcp: bool = True
    mcp_servers: list[dict] = field(default_factory=list)
    llm_profile: str = "default"


def render_prompt(template_name_or_str: str, **kwargs: Any) -> str:
    """渲染 Jinja2 模板。

    如果 template_name_or_str 对应 templates/ 下的文件，从文件加载；
    否则当作内联模板字符串处理。
    """
    template_path = TEMPLATE_DIR / template_name_or_str
    if template_path.exists():
        template = _jinja_env.get_template(template_name_or_str)
    else:
        template = jinja2.Template(template_name_or_str)
    return template.render(**kwargs)


# === 任务注册表 ===

_registry: dict[str, TaskConfig] = {}


def register_task(config: TaskConfig) -> None:
    _registry[config.name] = config


def get_task(name: str) -> Optional[TaskConfig]:
    return _registry.get(name)


def list_tasks() -> list[str]:
    return list(_registry.keys())
