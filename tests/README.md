# Tests（CI 门禁）

由 `.github/workflows/master.yml` 在 merge 前执行，image build 依赖其通过：

```bash
pytest tests/            # 稳定单元 / 仿真构建契约测试
```

- `tests/`：稳定模块单测 + 仿真构建契约（compiler/bundle、sandbox、verifier、scenario intake）。
- `tests/fixtures/golden_meta_app_artifact.json`：MA 构建产物的稳定契约样例，见 `tests/fixtures/golden.py`。
- 真实 MCP 全链路验收脚本见 `dev/simulation/headless_build.py`，**不**计入 CI。
