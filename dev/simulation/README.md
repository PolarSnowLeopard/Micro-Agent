# 仿真构建 — 开发期验收

仿真构建仍在迭代中，artifact / intake 等 schema 未冻结。此处脚本用于**本地 spike 验收**，不是发布 unit test。

| 文件 | 说明 |
|------|------|
| `test_build_bundle.py` | 伪造 build trace → `compile_build` / `BuildBundleStore` 落盘与 artifact 边界 |
| `test_verification_parse.py` | Verifier `terminate.verdict` 结构化解析与文本回退 |
| `test_scenario_intake.py` | 想定追问 JSON 解析与 mock LLM 回合（question / ready） |
| `headless_build.py` | 读 `external-mcp/service_catalog.json`，对真实 MCP 跑 Orchestrator 全链路 |

```bash
pytest dev/simulation/ -q
python dev/simulation/headless_build.py
python dev/simulation/headless_build.py sepsis_bedside pe_risk
```

链路稳定后，应在 `tests/` 补充少量对外 API integration 测试，再纳入 CI。
