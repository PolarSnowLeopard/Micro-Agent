"""IoEB template adaptation of AMQ-Bench sample meb_biopython_002."""

import asyncio
from typing import Literal

from Bio.SeqUtils.ProtParam import ProteinAnalysis


async def main_process(
    sequence: str,
    properties: list[Literal["molecular_weight", "isoelectric_point"]],
) -> dict[str, str]:
    """Compute selected physicochemical properties of a protein sequence.

    Args:
        sequence: Protein sequence using the twenty standard amino-acid symbols.
        properties: Unique protein properties to compute.

    Returns:
        Selected property names mapped to deterministic decimal strings.
    """
    await asyncio.sleep(0)
    analysis = ProteinAnalysis(sequence)
    result: dict[str, str] = {}
    if "molecular_weight" in properties:
        result["molecular_weight"] = f"{analysis.molecular_weight():.6f}"
    if "isoelectric_point" in properties:
        result["isoelectric_point"] = f"{analysis.isoelectric_point():.6f}"
    return result
