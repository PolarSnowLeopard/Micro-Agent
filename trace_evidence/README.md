# trace_evidence — legacy diagnostic tool

更新：2026-06-21。

`trace_evidence/` 是旧的 trace 后处理诊断工具包，不是当前“元应用想定式仿真构建”主链路。

当前主链路以 BuildBundle 为准：

```text
workspace/data/simulation_builds/{buildId}/
  manifest.json
  trace.json
  service_selection.json
  accepted_trajectory.json
  artifact.json
  frontend_state.json
  experiment/
```

本工具仍可用于离线检查旧 trace，但必须注意：

- 不参与 `MetaAppArtifact` 编译。
- 不参与 `real_mcp_reuse` baseline runner。
- 不读取或迁移旧 `workspace/data/traces` 作为新主线数据。
- 不应把 evidence card、checker report、config attachment 当成最终元应用产物。
- 运行输出属于本地诊断中间数据，不入库、不进 artifact、不提交 git。

## 可用入口

```bash
python trace_evidence/run_pipeline.py path/to/trace.json -o trace_evidence/output_local
```

输出通常包括：

- `evidence_card.json` / `evidence_card.md`
- `checker_report.json` / `checker_report.md`
- `config_attachment_draft.json`
- `bundle.json`

这些文件只用于人工诊断旧 trace 质量。

## 当前保留原因

- 旧实验/审计 trace 仍可能需要离线解释。
- 其中的结构化检查、脱敏、schema validation 代码可作为后续 BuildBundle 诊断工具的参考。

## 不再维护的内容

历史 handoff、progress、infrastructure report、`current/` baseline 输出已经从仓库移除。若后续需要新的诊断报告，应写入被 `.gitignore` 忽略的本地输出目录，而不是提交到仓库。
