# Tests（CI 门禁）

由 `.github/workflows/master.yml` 在 merge 前执行，image build 依赖其通过：

```bash
pytest tests/            # 稳定单元 / 仿真构建契约测试
pytest tests/functional  # 黑盒功能测试（按 ID 取 Artifact 的运行主链等）
```

- `tests/`：稳定模块单测 + 仿真构建契约（compiler/bundle、sandbox 路由、verifier 解析、scenario intake）。
- `tests/functional/`：经真实路由的黑盒功能测试。
- `tests/fixtures/golden_meta_app_artifact.json`：**跨端契约锚点**。MA 为真源，ioeb_backend / ioeb 各持相同副本；任一端改动构建产物契约都会让三端 id/hash 断言失败。勿手改，见 `tests/fixtures/golden.py`。
- 真实 MCP 全链路验收脚本见 `dev/simulation/headless_build.py`，**不**计入 CI。
