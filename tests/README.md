# Unit tests（CI 门禁）

`tests/` 仅放**稳定模块**的单元 / 组件测试，由 `.github/workflows/master.yml` 在 merge 前执行：

```bash
pytest tests/
```

仿真构建等仍在开发中的 spike 验收见 `dev/simulation/`，**不**在此目录。
