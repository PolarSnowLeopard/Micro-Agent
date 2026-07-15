"""Minimal deterministic algorithm used by packager contract tests."""


def main_process(text: str, repeat: int = 1) -> dict[str, str]:
    """Repeat input text and return a JSON-compatible result.

    Args:
        text: Text to repeat.
        repeat: Number of repetitions.

    Returns:
        The repeated text and its character count.
    """
    value = text * repeat
    return {"value": value, "length": str(len(value))}
