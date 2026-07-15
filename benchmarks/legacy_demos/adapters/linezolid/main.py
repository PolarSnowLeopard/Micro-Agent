"""Production adapter for the historical linezolid dose algorithm."""

from __future__ import annotations

from typing import Literal

from api.calculator import _calculate_linezolid_dose_impl


def main_process(
    sex: Literal[0, 1],
    age: int,
    height: int,
    weight: int,
    scr: float,
    tb: float,
    auc_min: float = 160.0,
    auc_max: float = 240.0,
) -> dict[str, float | int]:
    """Calculate an individualized linezolid dose and predicted AUC24.

    Args:
        sex: Biological sex used by the model, 0 for female and 1 for male.
        age: Patient age in years.
        height: Patient height in centimeters.
        weight: Patient weight in kilograms.
        scr: Serum creatinine in micromoles per liter.
        tb: Total bilirubin in micromoles per liter.
        auc_min: Lower target AUC24 bound.
        auc_max: Upper target AUC24 bound.

    Returns:
        Dose, interval, exposure prediction, BSA, and eGFR values.
    """
    return _calculate_linezolid_dose_impl(
        sex=sex,
        age=age,
        height=height,
        weight=weight,
        scr=scr,
        tb=tb,
        auc_range=[auc_min, auc_max],
    )
