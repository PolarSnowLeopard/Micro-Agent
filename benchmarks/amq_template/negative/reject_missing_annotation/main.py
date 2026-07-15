"""Negative control: an exposed parameter has no type annotation."""


def main_process(value) -> dict[str, str]:
    """Echo a value without a machine-readable input type.

    Args:
        value: Value that intentionally lacks a type annotation.

    Returns:
        Echoed value.
    """
    return {"value": str(value)}
