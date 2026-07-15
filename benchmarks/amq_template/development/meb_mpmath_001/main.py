"""IoEB template adaptation of AMQ-Bench sample meb_mpmath_001."""

from typing import Literal

import mpmath as mp


def main_process(
    expression: Literal["pi", "e"], precision: int
) -> dict[str, str]:
    """Compute a supported mathematical constant at arbitrary precision.

    Args:
        expression: Constant to compute; currently pi or e.
        precision: Number of digits after the decimal point, from 1 to 200.

    Returns:
        Structured result containing the expression, precision, and computed value.
    """
    if not 1 <= precision <= 200:
        raise ValueError("precision must be between 1 and 200")

    with mp.workdps(precision + 1):
        constant = mp.pi if expression == "pi" else mp.e
        value = str(constant)
    return {
        "expression": expression,
        "precision": str(precision),
        "value": value,
    }
