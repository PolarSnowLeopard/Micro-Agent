"""IoEB template adaptation of AMQ-Bench sample meb_pydy_001."""

from typing import Literal

import numpy as np
from pydy.models import multi_mass_spring_damper


def main_process(
    system: Literal["mass_spring_damper"],
    initial_conditions: dict[str, float],
    simulation_time: float,
) -> dict[str, str]:
    """Simulate a one-mass spring-damper system with PyDy.

    Args:
        system: Supported multibody system; currently mass_spring_damper.
        initial_conditions: Numeric position, velocity, mass, stiffness, and damping values.
        simulation_time: Simulation duration in seconds, from 0.1 to 60.

    Returns:
        Structured trajectory summary with final position, velocity, and extrema.
    """
    required = {"position", "velocity", "mass", "stiffness", "damping"}
    missing = sorted(required - set(initial_conditions))
    if missing:
        raise ValueError(
            "initial_conditions is missing required values: " + ", ".join(missing)
        )
    if initial_conditions["mass"] <= 0:
        raise ValueError("initial_conditions.mass must be greater than zero")
    if initial_conditions["stiffness"] <= 0:
        raise ValueError("initial_conditions.stiffness must be greater than zero")
    if initial_conditions["damping"] < 0:
        raise ValueError("initial_conditions.damping must be non-negative")

    model = multi_mass_spring_damper()
    constants = {str(symbol): symbol for symbol in model.constants_symbols}
    model.constants = {
        constants["m0"]: initial_conditions["mass"],
        constants["k0"]: initial_conditions["stiffness"],
        constants["c0"]: initial_conditions["damping"],
    }
    model.initial_conditions = {
        model.coordinates[0]: initial_conditions["position"],
        model.speeds[0]: initial_conditions["velocity"],
    }
    model.times = np.linspace(0.0, simulation_time, 101)
    trajectory = model.integrate()
    positions = trajectory[:, 0]
    return {
        "system": system,
        "samples": str(len(model.times)),
        "final_position": f"{float(trajectory[-1, 0]):.6f}",
        "final_velocity": f"{float(trajectory[-1, 1]):.6f}",
        "min_position": f"{float(positions.min()):.6f}",
        "max_position": f"{float(positions.max()):.6f}",
    }
