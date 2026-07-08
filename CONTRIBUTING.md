# 贡献指南

感谢你对 Micro-Agent 的关注！为了让协作更高效，请在贡献前阅读以下指南。

## 贡献流程

1. **先开 Issue 讨论** — 无论是 Bug 修复、新功能还是文档改进，请先开 Issue 描述你的想法，与维护者达成共识后再提交 PR。
2. **Fork & 创建分支** — 从 `master` 创建特性分支，分支命名建议：`feat/xxx`、`fix/xxx`、`docs/xxx`。
3. **提交 PR** — PR 描述中请关联对应 Issue（如 `Closes #123`），并说明改动内容和测试方式。

## PR 要求

- 每个 PR 应聚焦于**单一目的**（一个 Bug 修复或一个功能）
- 代码改动需附带相应的测试
- 文档改动应提供**新增价值**，而非重组已有内容
- 确保 `pytest`（`tests/` 目录）测试通过
- 仿真构建等开发中功能的手动验收见 `dev/simulation/`，不纳入 CI

## 开发环境

```bash
git clone https://github.com/fdueblab/Micro-Agent.git
cd Micro-Agent
pip install -e ".[dev]"
pytest                    # CI 单元测试（tests/）
pytest dev/simulation/    # 仿真构建开发期验收（手动，非 CI）
```

## 行为准则

请保持友善和尊重，专注于技术讨论。
