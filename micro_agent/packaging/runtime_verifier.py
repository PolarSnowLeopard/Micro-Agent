"""Isolated build and runtime acceptance gate for generated MCP artifacts."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from micro_agent.packaging.models import PackagingPlan
from micro_agent.packaging.verifier import VerificationReport


PROBE_MARKER = "IOEB_RUNTIME_VERIFICATION="
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class ContainerRuntimeVerifier:
    """Build an artifact and exercise its MCP registry inside an isolated container.

    The submitted algorithm is never imported in the Agent process.  Network is
    disabled for the runtime probe so a ready artifact cannot depend on fetching
    code, models, or data after deployment.
    """

    backend = "docker"

    def __init__(
        self,
        artifact_dir: str | Path,
        plan: PackagingPlan,
        *,
        build_timeout_seconds: int = 1200,
        runtime_timeout_seconds: int = 180,
        smoke_timeout_seconds: int = 60,
        require_full_smoke_coverage: bool = False,
        command_runner: CommandRunner = subprocess.run,
    ) -> None:
        self.root = Path(artifact_dir).resolve()
        self.plan = plan
        self.build_timeout_seconds = build_timeout_seconds
        self.runtime_timeout_seconds = runtime_timeout_seconds
        self.smoke_timeout_seconds = smoke_timeout_seconds
        self.require_full_smoke_coverage = require_full_smoke_coverage
        self.command_runner = command_runner

    async def verify(self) -> VerificationReport:
        return await asyncio.to_thread(self._verify_sync)

    def _verify_sync(self) -> VerificationReport:
        report = VerificationReport(passed=False)
        report.checks["runtimeBackend"] = self.backend
        report.checks["networkDuringProbe"] = False
        smoke_planned = [
            tool["name"]
            for tool in self.plan.tools
            if tool.get("smokeTest", {}).get("enabled")
        ]
        report.checks["plannedToolCount"] = len(self.plan.tools)
        report.checks["smokeTestsPlanned"] = smoke_planned
        report.checks["smokeCoverage"] = round(
            len(smoke_planned) / len(self.plan.tools),
            4,
        ) if self.plan.tools else 0.0
        if self.require_full_smoke_coverage and len(smoke_planned) != len(self.plan.tools):
            missing = sorted(set(self.plan.tool_names) - set(smoke_planned))
            report.errors.append(
                "[smoke_coverage] 生产发布要求每个 MCP 工具都有仓库证据支持的"
                "可执行 smokeTest，缺少: " + ", ".join(missing)
            )
            return report
        image_tag = f"ioeb-runtime-verify:{uuid.uuid4().hex[:12]}"
        built = False
        build_started = time.perf_counter()
        try:
            build = self._run(
                ["docker", "build", "-t", image_tag, "."],
                cwd=self.root,
                timeout=self.build_timeout_seconds,
            )
            report.checks["buildSeconds"] = round(time.perf_counter() - build_started, 3)
            report.checks["buildExitCode"] = build.returncode
            if build.returncode != 0:
                detail = _command_output(build)
                report.errors.append(
                    f"[{_classify_failure(detail, phase='build')}] 容器构建失败：\n"
                    + _tail(detail)
                )
                return report
            built = True

            runtime_started = time.perf_counter()
            runtime = self._run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=512m",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "256",
                    "--memory",
                    "2g",
                    "--cpus",
                    "2",
                    "--env",
                    "HOME=/tmp",
                    "--env",
                    "XDG_CACHE_HOME=/tmp/cache",
                    "--entrypoint",
                    "python",
                    image_tag,
                    "-c",
                    _runtime_probe_source(self.smoke_timeout_seconds),
                ],
                timeout=self.runtime_timeout_seconds,
            )
            report.checks["runtimeSeconds"] = round(time.perf_counter() - runtime_started, 3)
            report.checks["runtimeExitCode"] = runtime.returncode
            detail = _command_output(runtime)
            if runtime.returncode != 0:
                report.errors.append(
                    f"[{_classify_failure(detail, phase='runtime')}] "
                    "容器运行验收失败：\n" + _tail(detail)
                )
                return report

            payload = _parse_probe_payload(detail)
            if payload is None:
                report.errors.append(
                    "[runtime_protocol] 容器探针未返回结构化验收结果：\n" + _tail(detail)
                )
                return report
            report.checks.update(payload)
            report.checks["functionalVerified"] = (
                payload.get("smokeTestCount") == len(self.plan.tools)
            )
            expected = sorted(self.plan.tool_names)
            actual = sorted(payload.get("registeredTools", []))
            if actual != expected:
                report.errors.append(
                    "[tool_registry] 运行时 MCP 工具集合与规划不一致: "
                    f"expected={expected}, actual={actual}"
                )
                return report
            report.passed = True
            return report
        except FileNotFoundError:
            report.errors.append(
                "[runtime_backend_unavailable] 找不到 docker 命令，无法执行强制运行验收"
            )
            return report
        except subprocess.TimeoutExpired as exc:
            phase = "build" if not built else "runtime"
            report.checks[f"{phase}TimedOut"] = True
            report.errors.append(
                f"[{phase}_timeout] {phase} 阶段超过 "
                f"{self.build_timeout_seconds if phase == 'build' else self.runtime_timeout_seconds} 秒"
                + _timeout_detail(exc)
            )
            return report
        except OSError as exc:
            report.errors.append(f"[runtime_backend_error] 无法执行容器验收: {exc}")
            return report
        finally:
            if built:
                try:
                    self._run(
                        ["docker", "image", "rm", "-f", image_tag],
                        timeout=60,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    report.warnings.append(f"未能清理运行验收镜像: {image_tag}")

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        return self.command_runner(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def _runtime_probe_source(smoke_timeout_seconds: int) -> str:
    return f"""
import asyncio
import json

import server

def structured_value(result, schema):
    value = result
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        if structured is not None:
            value = structured
        else:
            value = content
    if (
        schema.get("type") != "object"
        and isinstance(value, dict)
        and set(value) == {{"result"}}
    ):
        value = value["result"]
    if isinstance(value, (list, tuple)) and value and hasattr(value[0], "text"):
        texts = [item.text for item in value if hasattr(item, "text")]
        try:
            value = json.loads(texts[0]) if len(texts) == 1 else [json.loads(text) for text in texts]
        except (TypeError, json.JSONDecodeError):
            value = texts[0] if len(texts) == 1 else texts
    return value

def assert_schema(value, schema, path="$"):
    expected = schema.get("type")
    matches = (
        (expected == "object" and isinstance(value, dict))
        or (expected == "array" and isinstance(value, list))
        or (expected == "string" and isinstance(value, str))
        or (expected == "boolean" and isinstance(value, bool))
        or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (
            expected == "number"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
        or expected in (None, "null")
    )
    if expected == "null":
        matches = value is None
    if not matches:
        raise TypeError(
            f"smoke output schema mismatch at {{path}}: expected {{expected}}, "
            f"got {{type(value).__name__}}"
        )
    if expected == "object":
        required = schema.get("required", [])
        missing = sorted(set(required) - set(value))
        if missing:
            raise TypeError(
                f"smoke output schema mismatch at {{path}}: missing required {{missing}}"
            )
        for name, child_schema in schema.get("properties", {{}}).items():
            if name in value:
                assert_schema(value[name], child_schema, path + "." + name)
    elif expected == "array" and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            assert_schema(item, schema["items"], f"{{path}}[{{index}}]")

async def verify():
    plan = json.load(open("/app/packaging_plan.json", encoding="utf-8"))
    planned = [
        tool
        for service in plan.get("services", [])
        for tool in service.get("tools", [])
    ]
    registered = sorted(tool.name for tool in await server.mcp.list_tools())
    expected = sorted(tool.get("name") for tool in planned)
    if registered != expected:
        raise RuntimeError(
            "runtime tool registry mismatch: "
            + json.dumps({{"expected": expected, "actual": registered}})
        )
    smoke = []
    smoke_failures = {{}}
    for tool in planned:
        case = tool.get("smokeTest", {{}})
        if not case.get("enabled"):
            continue
        try:
            result = await asyncio.wait_for(
                server.mcp.call_tool(tool["name"], case.get("input", {{}})),
                timeout={int(smoke_timeout_seconds)},
            )
            assert_schema(
                structured_value(result, tool.get("outputSchema", {{}})),
                tool.get("outputSchema", {{}}),
            )
            smoke.append(tool["name"])
        except Exception as exc:
            smoke_failures[tool["name"]] = f"{{type(exc).__name__}}: {{exc}}"
    if smoke_failures:
        raise RuntimeError(
            "smoke test failures: "
            + json.dumps(smoke_failures, sort_keys=True)
        )
    print(
        {PROBE_MARKER!r}
        + json.dumps(
            {{
                "registeredTools": registered,
                "smokeTestsExecuted": smoke,
                "smokeTestCount": len(smoke),
            }},
            sort_keys=True,
        )
    )

asyncio.run(verify())
""".strip()


def _parse_probe_payload(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        if not line.startswith(PROBE_MARKER):
            continue
        try:
            value = json.loads(line[len(PROBE_MARKER):])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def _command_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part).strip()


def _tail(text: str, limit: int = 12_000) -> str:
    return text[-limit:] if text else "(no output)"


def _timeout_detail(exc: subprocess.TimeoutExpired) -> str:
    parts: list[str] = []
    for value in (exc.stdout, exc.stderr):
        if isinstance(value, bytes):
            parts.append(value.decode(errors="replace"))
        elif isinstance(value, str):
            parts.append(value)
    detail = "\n".join(parts)
    return "\n" + _tail(detail) if detail else ""


def _classify_failure(text: str, *, phase: str) -> str:
    normalized = text.lower()
    if "no matching distribution found" in normalized or "could not find a version" in normalized:
        return "dependency_resolution"
    if "modulenotfounderror" in normalized or "no module named" in normalized:
        return "python_dependency_or_import"
    if "importerror" in normalized or "cannot import name" in normalized:
        return "source_import"
    if "apt-get" in normalized or "unable to locate package" in normalized:
        return "system_dependency"
    if "permission denied" in normalized or "read-only file system" in normalized:
        return "runtime_filesystem"
    if "runtime tool registry mismatch" in normalized:
        return "tool_registry"
    if (
        "validation error" in normalized
        or "smoketest" in normalized
        or "smoke output" in normalized
    ):
        return "smoke_test"
    return f"{phase}_unknown"
