"""Static and optional Docker runtime verification for generated artifacts."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class VerificationError(RuntimeError):
    pass


def _check(name: str, success: bool, message: str = "") -> Dict[str, Any]:
    result: Dict[str, Any] = {"name": name, "success": success}
    if message:
        result["message"] = message
    return result


def verify_artifact_static(artifact: Path | str) -> Dict[str, Any]:
    root = Path(artifact).expanduser().resolve()
    checks: List[Dict[str, Any]] = []
    required = [
        "algorithm/main.py",
        "server.py",
        "requirements.txt",
        "Dockerfile",
        "docker-compose.yml",
        "ioeb-service.json",
        "_ioeb_verify.py",
    ]
    for relative in required:
        exists = (root / relative).is_file()
        checks.append(_check(f"file:{relative}", exists, "missing" if not exists else ""))

    for relative in ["algorithm/main.py", "server.py", "_ioeb_verify.py"]:
        path = root / relative
        if not path.is_file():
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            checks.append(_check(f"syntax:{relative}", True))
        except (SyntaxError, UnicodeDecodeError) as exc:
            checks.append(_check(f"syntax:{relative}", False, str(exc)))

    manifest_path = root / "ioeb-service.json"
    manifest: Optional[Dict[str, Any]] = None
    if manifest_path.is_file():
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("manifest root is not an object")
            manifest = value
            checks.append(_check("manifest:json", True))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            checks.append(_check("manifest:json", False, str(exc)))

    requirements_path = root / "requirements.txt"
    if requirements_path.is_file():
        requirements = requirements_path.read_text(encoding="utf-8").splitlines()
        runtime_pins = [line for line in requirements if line.strip().startswith("mcp>=1.27,<2")]
        checks.append(
            _check(
                "runtime:mcp-version-range",
                len(runtime_pins) == 1,
                "" if len(runtime_pins) == 1 else "expected exactly one mcp>=1.27,<2 requirement",
            )
        )

    success = all(check["success"] for check in checks)
    return {
        "verificationVersion": "ioeb.mcp-artifact-verification/v1",
        "mode": "static",
        "artifact": str(root),
        "success": success,
        "manifest": manifest,
        "checks": checks,
    }


def _run(
    command: List[str],
    *,
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=check,
    )


def _wait_for_server(container: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    command = [
        "docker",
        "exec",
        container,
        "python",
        "-c",
        "import socket; socket.create_connection(('127.0.0.1', 8000), 1).close()",
    ]
    while time.monotonic() < deadline:
        result = _run(command, timeout=5, check=False)
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise VerificationError(f"MCP server did not listen on port 8000 within {timeout}s")


def verify_artifact_docker(
    artifact: Path | str,
    *,
    build_timeout: int = 600,
    startup_timeout: int = 60,
    execution_timeout: int = 120,
    keep_image: bool = False,
    no_cache: bool = False,
) -> Dict[str, Any]:
    """Build and verify an artifact in a restricted, disposable container."""
    root = Path(artifact).expanduser().resolve()
    static_report = verify_artifact_static(root)
    report: Dict[str, Any] = {
        "verificationVersion": "ioeb.mcp-artifact-verification/v1",
        "mode": "docker",
        "artifact": str(root),
        "success": False,
        "buildCache": "disabled" if no_cache else "default",
        "executionTimeoutSeconds": execution_timeout,
        "static": static_report,
        "checks": [],
    }
    if not static_report["success"]:
        report["error"] = {
            "type": "StaticVerificationFailed",
            "message": "Docker verification was skipped because static checks failed",
        }
        return report
    if shutil.which("docker") is None:
        report["error"] = {"type": "DockerUnavailable", "message": "docker executable was not found"}
        return report

    suffix = uuid.uuid4().hex[:12]
    image = f"ioeb-mcp-verify:{suffix}"
    container = f"ioeb-mcp-verify-{suffix}"
    container_started = False
    try:
        version = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
        report["checks"].append(
            _check("docker:daemon", True, version.stdout.strip())
        )
        build_command = ["docker", "build", "--pull"]
        if no_cache:
            build_command.append("--no-cache")
        build_command.extend(["--tag", image, str(root)])
        build = _run(build_command, timeout=build_timeout)
        report["checks"].append(_check("docker:build", True))
        report["buildLogTail"] = (build.stdout + build.stderr)[-8000:]

        run_result = _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp:size=64m,mode=1777",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "256",
                "--memory",
                "1g",
                "--cpus",
                "1.0",
                image,
            ],
            timeout=30,
        )
        container_started = True
        report["containerId"] = run_result.stdout.strip()
        report["checks"].append(_check("docker:start", True))
        _wait_for_server(container, startup_timeout)
        report["checks"].append(_check("mcp:listen", True))

        runtime = _run(
            ["docker", "exec", container, "python", "/app/_ioeb_verify.py"],
            timeout=execution_timeout,
            check=False,
        )
        output_lines = [line for line in runtime.stdout.splitlines() if line.strip()]
        if not output_lines:
            raise VerificationError(
                f"runtime verifier produced no JSON output: {runtime.stderr[-2000:]}"
            )
        runtime_report = json.loads(output_lines[-1])
        report["runtime"] = runtime_report
        report["checks"].append(
            _check(
                "mcp:protocol-and-cases",
                runtime.returncode == 0 and runtime_report.get("success") is True,
                runtime.stderr[-2000:],
            )
        )
        report["success"] = all(item["success"] for item in report["checks"])
        if not report["success"]:
            logs = _run(["docker", "logs", container], timeout=15, check=False)
            report["containerLogTail"] = (logs.stdout + logs.stderr)[-8000:]
        return report
    except (subprocess.SubprocessError, OSError, ValueError, VerificationError) as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if container_started:
            logs = _run(["docker", "logs", container], timeout=15, check=False)
            report["containerLogTail"] = (logs.stdout + logs.stderr)[-8000:]
        return report
    finally:
        if container_started:
            _run(["docker", "rm", "--force", container], timeout=30, check=False)
        if not keep_image:
            _run(["docker", "image", "rm", "--force", image], timeout=30, check=False)
