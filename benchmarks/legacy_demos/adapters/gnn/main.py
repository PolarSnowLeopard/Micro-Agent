"""Production adapter for the historical GNN risk model demo."""

from __future__ import annotations

import os


def main_process(verify_checkpoint: bool = True) -> dict[str, str]:
    """Load the historical GNN checkpoint and report inference readiness.

    Args:
        verify_checkpoint: Require the bundled checkpoint to load successfully.

    Returns:
        Model readiness, device, and checkpoint metadata.
    """
    os.environ["DGLBACKEND"] = "pytorch"
    os.environ["MPLCONFIGDIR"] = "/tmp/matplotlib"
    from inference import InferenceModel

    inference_model = InferenceModel(
        model_path="checkpoint/model.pt",
        device="cpu",
    )
    if verify_checkpoint:
        inference_model.load_model(
            in_feats=211,
            h_feats=211,
            out_feats=3,
        )
    return {
        "status": "ready" if inference_model.model is not None else "not_loaded",
        "device": str(inference_model.device),
        "model_path": inference_model.model_path,
    }
