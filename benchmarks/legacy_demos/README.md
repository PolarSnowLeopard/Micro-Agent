# Original IoEB Demo Regression Track

This track migrates the three historical prompt-GT demonstrations to the same
production v1 input contract used by every other algorithm. The production
packager contains no filename, repository, or sample-specific branches.

The source archives remain on the IoEB COS bucket. `prepare.py` downloads the
exact checksum-pinned inputs, safely extracts them, applies the reviewed
`main_process` adapters, and emits frontend-uploadable ZIP packages.

```bash
python benchmarks/legacy_demos/prepare.py \
  --output /tmp/ioeb-legacy-demos
```

The prepared packages must pass strict validation, static artifact verification,
and disposable Docker MCP verification before a demo download is replaced.

The latest recorded run is in [`results/vpn-2026-07-15.json`](results/vpn-2026-07-15.json).
It is a compatibility regression for these three reviewed demos, not evidence of
generalization to arbitrary algorithm repositories. In particular, the original
GNN archive has no fixture dataset, so its oracle verifies that the bundled
checkpoint loads and is exposed through MCP; dataset-level graph inference needs
a separate fixture before it can be claimed as covered.
