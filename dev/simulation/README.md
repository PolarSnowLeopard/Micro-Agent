# 仿真构建 — 人工真实 MCP 验收

本目录只保留人工连接真实 MCP 的验收入口。稳定契约测试位于 `tests/`，并由 CI 执行。

| 文件 | 说明 |
|------|------|
| `headless_build.py` | 读 `external-mcp/service_catalog.json`，对真实 MCP 跑 Orchestrator 全链路 |

```bash
python dev/simulation/headless_build.py
python dev/simulation/headless_build.py sepsis_bedside pe_risk
```
