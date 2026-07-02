# 开发期脚本与验收

本目录存放**功能开发中**的手动验收脚本与 spike 测试，**不在 CI 门禁内**。

稳定后的对外契约测试应写在 `tests/`（与 ioeb_backend 的 unit/integration 同级）。

## 子目录

| 目录 | 用途 |
|------|------|
| `simulation/` | 元应用仿真构建链路：伪造 trace 验收、Verifier 解析、想定追问 mock、headless 全链路 |

本地运行示例：

```bash
pip install -e ".[dev]"
pytest dev/simulation/ -q
python dev/simulation/headless_build.py
```
