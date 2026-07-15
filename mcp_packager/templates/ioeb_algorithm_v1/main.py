"""Minimal IoEB production algorithm package."""


def main_process(text: str, repeat: int = 1) -> dict[str, str]:
    """Repeat text and return a structured result.

    Args:
        text: Non-empty text to repeat.
        repeat: Number of repetitions, from 1 to 10.

    Returns:
        Repeated text and its character count.
    """
    value = text * repeat
    return {"value": value, "length": str(len(value))}
