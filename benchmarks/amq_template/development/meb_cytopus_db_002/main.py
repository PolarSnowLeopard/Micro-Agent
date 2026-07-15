"""IoEB template adaptation of AMQ-Bench sample meb_cytopus_db_002."""


def main_process(
    gene_set_a: list[str],
    gene_set_b: list[str],
) -> dict[str, str | list[str]]:
    """Compute the overlap coefficient between two non-empty gene sets.

    Args:
        gene_set_a: Unique uppercase gene symbols in the first set.
        gene_set_b: Unique uppercase gene symbols in the second set.

    Returns:
        Common genes, their count, and the overlap coefficient.
    """
    left = set(gene_set_a)
    right = set(gene_set_b)
    common = sorted(left.intersection(right))
    coefficient = len(common) / min(len(left), len(right))
    return {
        "common_genes": common,
        "common_count": str(len(common)),
        "overlap_coefficient": f"{coefficient:.6f}",
    }
