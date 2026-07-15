"""Negative control: submitted algorithm attempts process execution."""

import os


def main_process(command: str) -> dict[str, str]:
    """Execute a shell command, which the production contract forbids.

    Args:
        command: Shell command to execute.

    Returns:
        Process status.
    """
    status = os.system(command)
    return {"status": str(status)}
