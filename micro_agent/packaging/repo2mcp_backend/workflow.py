"""SSE-compatible workflows backed by the Repo2MCP v8 subprocess."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, AsyncIterator

from micro_agent.core.config import LLMConfig
from micro_agent.core.schema import AgentEvent
from micro_agent.packaging.repo2mcp_backend.backend import (
    Repo2MCPBackend,
    Repo2MCPBackendConfig,
    Repo2MCPRun,
    tool_design_to_frontend_graph,
)


class ToolDesignCache:
    """Short-lived content-addressed cache joining the two unchanged UI calls."""

    def __init__(self, *, max_entries: int = 32, ttl_seconds: int = 1800) -> None:
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        item = self._items.pop(fingerprint, None)
        if item is None:
            return None
        created, design = item
        if time.monotonic() - created > self.ttl_seconds:
            return None
        self._items[fingerprint] = item
        return json.loads(json.dumps(design, ensure_ascii=False))

    def put(self, fingerprint: str, design: dict[str, Any]) -> None:
        self._items.pop(fingerprint, None)
        self._items[fingerprint] = (
            time.monotonic(),
            json.loads(json.dumps(design, ensure_ascii=False)),
        )
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)


tool_design_cache = ToolDesignCache()


class _Repo2MCPWorkflowBase:
    def __init__(self, *, llm_config: LLMConfig) -> None:
        self.backend = Repo2MCPBackend(
            Repo2MCPBackendConfig.from_llm_config(llm_config)
        )
        self._process: asyncio.subprocess.Process | None = None

    def cancel(self) -> None:
        if self._process is not None and self._process.returncode is None:
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self._process.terminate()


class Repo2MCPAnalysisWorkflow(_Repo2MCPWorkflowBase):
    """Run DARP/BAGE plus the paper's tool-design Agent and produce UI graph."""

    def __init__(
        self,
        *,
        project_dir: str | Path,
        graph_path: str | Path,
        repository_fingerprint: str,
        llm_config: LLMConfig,
    ) -> None:
        super().__init__(llm_config=llm_config)
        self.project_dir = Path(project_dir).resolve()
        self.graph_path = Path(graph_path).resolve()
        self.repository_fingerprint = repository_fingerprint
        self.result: dict[str, Any] | None = None

    async def run(self, request: str) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            type="think",
            step=0,
            data={
                "thought": (
                    "[Repo2MCP v8] 开始 DARP/BAGE 仓库上下文构造，随后由 Agent "
                    "进行能力抽象和 MCP 工具设计。"
                )
            },
        )
        run = self.backend.prepare_run(
            project_dir=self.project_dir,
            job_root=self.graph_path.parent,
            sample_id=self.repository_fingerprint[:24],
            wrap_intent=_wrap_intent(request),
            analysis_only=True,
        )
        async for event in self._execute_and_capture(run):
            yield event
        assert self.result is not None
        if not self.result.get("success"):
            yield AgentEvent(
                type="error",
                step=99,
                data={"error": _failure_message(self.result)},
            )
            return
        tool_design = self.result.get("tool_design")
        if not isinstance(tool_design, dict):
            yield AgentEvent(
                type="error",
                step=99,
                data={"error": "Repo2MCP 分析完成但未返回有效 tool_design.json"},
            )
            return
        graph = tool_design_to_frontend_graph(tool_design)
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.graph_path.write_text(
            json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tool_design_cache.put(self.repository_fingerprint, tool_design)
        yield AgentEvent(
            type="done",
            step=100,
            data={
                "result": (
                    f"Repo2MCP v8 Agent 分析完成：抽象出 {graph['meta']['toolCount']} 个 MCP 工具。"
                )
            },
        )

    async def _execute_and_capture(
        self, run: Repo2MCPRun
    ) -> AsyncIterator[AgentEvent]:
        async for event in _execute_process(self, run):
            yield event
        self.result = run.load_result()


class Repo2MCPPackagingWorkflow(_Repo2MCPWorkflowBase):
    """Generate, build, repair, and collect a platform-deployable artifact."""

    def __init__(
        self,
        *,
        project_dir: str | Path,
        artifact_dir: str | Path,
        repository_fingerprint: str,
        llm_config: LLMConfig,
        tool_design: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(llm_config=llm_config)
        self.project_dir = Path(project_dir).resolve()
        self.artifact_dir = Path(artifact_dir).resolve()
        self.repository_fingerprint = repository_fingerprint
        self.tool_design = tool_design
        self.result: dict[str, Any] | None = None

    async def run(self, request: str) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            type="think",
            step=0,
            data={
                "thought": (
                    "[Repo2MCP v8] 正在执行工具设计、服务代码生成、Docker 构建与"
                    "有界诊断修复；只有构建和健康检查通过才会发布产物。"
                )
            },
        )
        run = self.backend.prepare_run(
            project_dir=self.project_dir,
            job_root=self.artifact_dir.parent,
            sample_id=self.repository_fingerprint[:24],
            wrap_intent=_wrap_intent(request),
            analysis_only=False,
            tool_design=self.tool_design,
        )
        async for event in _execute_process(self, run):
            yield event
        self.result = run.load_result()
        if not self.result.get("success"):
            yield AgentEvent(
                type="error",
                step=99,
                data={"error": _failure_message(self.result)},
            )
            return
        try:
            artifact = self.backend.finalize_artifact(run, self.result)
        except Exception as exc:
            yield AgentEvent(
                type="error",
                step=99,
                data={"error": f"Repo2MCP 产物收集失败: {exc}"},
            )
            return
        if artifact != self.artifact_dir:
            yield AgentEvent(
                type="error",
                step=99,
                data={"error": "Repo2MCP 产物目录与平台任务目录不一致"},
            )
            return
        graph = json.loads((artifact / "function.json").read_text(encoding="utf-8"))
        yield AgentEvent(
            type="done",
            step=100,
            data={
                "result": (
                    f"Repo2MCP v8 封装完成：{graph['meta']['toolCount']} 个工具，"
                    "Docker 构建与健康检查已通过。"
                )
            },
        )


async def _execute_process(
    workflow: _Repo2MCPWorkflowBase,
    run: Repo2MCPRun,
) -> AsyncIterator[AgentEvent]:
    workflow._process = await asyncio.create_subprocess_exec(
        *run.command,
        cwd=str(run.cwd),
        env=run.env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    assert workflow._process.stdout is not None
    step = 1
    last_stage = ""
    async for raw_line in workflow._process.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        stage = _stage_label(line)
        if stage and stage != last_stage:
            last_stage = stage
            yield AgentEvent(type="think", step=step, data={"thought": stage})
            step += 1
        elif _is_progress_line(line):
            yield AgentEvent(
                type="tool_result",
                step=step,
                data={"tool": "repo2mcp_v8", "result": line[:2000]},
            )
            step += 1
    await workflow._process.wait()
    workflow._process = None


def _stage_label(line: str) -> str:
    labels = {
        "Stage 0:": "[仓库上下文] 正在执行 AST 清单、DARP 相关度传播和 BAGE 预算编码。",
        "Stage 1:": "[能力抽象] Agent 正在核对源码并设计 MCP 工具接口。",
        "Stage 2:": "[服务重构] Agent 正在生成 MCP Server、依赖和容器配置。",
        "Stage 3:": "[构建验收] 正在 Docker 构建、健康检查并按日志修复。",
        "Stage 4:": "[产物收集] 构建已结束，正在生成平台部署包。",
    }
    for prefix, label in labels.items():
        if prefix in line:
            return label
    return ""


def _is_progress_line(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            "工具数:",
            "构建尝试",
            "Docker 构建成功",
            "Docker 构建失败",
            "健康检查通过",
            "健康检查失败",
            "启动构建修复",
            "启动健康检查修复",
        )
    )


def _wrap_intent(request: str) -> str:
    request = (request or "").strip()
    if not request or request.lower().endswith((".zip", ".tar", ".gz")):
        return (
            "分析完整算法仓库，识别面向最终用户的独立领域能力；根据输入输出语义、"
            "共享状态和调用关系完成接口抽象与必要重构，并生成可部署的 MCP 服务。"
        )
    return request


def _failure_message(result: dict[str, Any]) -> str:
    stage = str(result.get("stage") or "unknown")
    message = str(result.get("message") or "unknown failure")
    return f"Repo2MCP v8 在 {stage} 阶段失败: {message}"
