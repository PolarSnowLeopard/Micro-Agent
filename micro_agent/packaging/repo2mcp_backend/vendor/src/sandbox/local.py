"""本地沙箱环境 - 直接在主机上执行命令"""
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from config import default_config
from src.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ExecutionResult:
    success: bool
    stdout: str
    stderr: str
    exit_code: int


class LocalSandbox:
    """本地执行沙箱：有状态的 bash 会话"""

    def __init__(self, workdir: str | None = None, timeout: int = 1200):
        self.workdir = (
            workdir
            if workdir is not None
            else default_config.sandbox.workspace_base
        )
        self.timeout = timeout
        self._cwd = self.workdir
        self._env = os.environ.copy()
        self._session_active = False

    def start_session(self) -> bool:
        self._session_active = True
        self._cwd = self.workdir
        os.makedirs(self.workdir, exist_ok=True)
        logger.info(f"LocalSandbox session started, workdir: {self.workdir}")
        return True

    def stop_session(self) -> bool:
        self._session_active = False
        logger.info("LocalSandbox session stopped")
        return True

    def exec(self, command: str, timeout: Optional[int] = None) -> ExecutionResult:
        timeout = timeout or self.timeout

        full_command = f"""
cd "{self._cwd}" 2>/dev/null || cd "{self.workdir}"
{command}
__EXIT_CODE__=$?
echo "___PWD___:$(pwd)"
exit $__EXIT_CODE__
"""
        try:
            result = subprocess.run(
                ["/bin/bash", "-c", full_command],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env,
            )

            stdout = result.stdout

            if "___PWD___:" in stdout:
                lines = stdout.split("\n")
                new_lines = []
                for line in lines:
                    if line.startswith("___PWD___:"):
                        self._cwd = line.replace("___PWD___:", "").strip()
                    else:
                        new_lines.append(line)
                stdout = "\n".join(new_lines).rstrip("\n")

            return ExecutionResult(
                success=result.returncode == 0,
                stdout=stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
            )

        except subprocess.TimeoutExpired:
            logger.warning(f"Command timed out after {timeout}s: {command[:80]}...")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr=f"Command timed out after {timeout} seconds",
                exit_code=-1,
            )
        except Exception as e:
            logger.error(f"Command execution failed: {e}")
            return ExecutionResult(
                success=False, stdout="", stderr=str(e), exit_code=-1
            )

    def __enter__(self):
        self.start_session()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_session()
        return False
