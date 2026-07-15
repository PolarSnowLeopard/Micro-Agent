"""IoEB template adaptation of AMQ-Bench sample meb_networkx_002."""

from typing import Literal

import networkx as nx


def main_process(
    edges: list[tuple[str, str, float]],
    source: str,
    target: str,
    algorithm: Literal["dijkstra", "bellman_ford"] = "dijkstra",
) -> dict[str, str | list[str]]:
    """Compute the shortest path through a weighted undirected graph.

    Args:
        edges: Weighted edges represented as source, target, and positive weight triples.
        source: Source node for the path computation.
        target: Target node for the path computation.
        algorithm: Shortest-path algorithm to use.

    Returns:
        Structured shortest path, total weight, and selected algorithm.
    """
    graph = nx.Graph()
    graph.add_weighted_edges_from(edges)
    if algorithm == "dijkstra":
        path = nx.dijkstra_path(graph, source, target, weight="weight")
        total_weight = nx.dijkstra_path_length(graph, source, target, weight="weight")
    else:
        path = nx.bellman_ford_path(graph, source, target, weight="weight")
        total_weight = nx.bellman_ford_path_length(
            graph, source, target, weight="weight"
        )
    return {
        "path": [str(node) for node in path],
        "total_weight": f"{float(total_weight):.6f}",
        "algorithm": algorithm,
    }
