# Wake #10 Handoff

## Commit
`e04a92d` — schema_version in config_draft + output cleanup rotation

## Delivered This Wake
1. **schema_version in ConfigAttachmentDraft** — Added `schema_version="1.0.0"` field, serialized as first key in `to_dict()` output. Matches evidence_card and checker_report convention.
2. **Output folder cleanup** — `_cleanup_output_dir()` removes stale `ev-*` / `sim-*` / `*_bundle.json` artifacts before each new pipeline run. Only canonical outputs survive.
3. **Trace rotation** — `_rotate_traces()` keeps only the 5 most recent trace files, preventing unbounded growth.
4. **2 new tests** — `test_schema_version_present` + `test_schema_version_first_key` in test_config_attachment.py.

## Verification
- 157 unit tests PASS
- Fresh headless trace: 20/20 checks PASS, cleanup removed 11 stale files, 1 old trace rotated

## Test Count Progression
Wake#5: 55 → Wake#6: 125 → Wake#7: 132 → Wake#8: 148 → Wake#9: 155 → Wake#10: 157

## Remaining Quality Angles (for future wakes)
- evidence_card.md readability polish (table widths, section folding)
- Broader e2e integration test (config_attachment round-trip through full pipeline)
- Performance profiling of headless_run (currently ~25s)
- Schema validation test for config_draft JSON against a formal JSON Schema
- CI integration (GitHub Actions workflow for pytest + headless)
