

## ❓ 常见问题

### 基础概念

**Q: Micro-Agent 是什么？**  
A: Micro-Agent 是一个轻量级的垂域 Agent 服务框架，专为快速构建和交付专业 AI Agent API 服务设计。核心代码少于 3000 行，支持 Skills 知识注入、RAG 检索增强、MCP 工具集成，适合垂直领域应用。

**Q: Micro-Agent 与 LangGraph 有什么区别？**  
A: LangGraph 面向复杂多步工作流编排，适合需要精细控制 Agent 执行流程的场景；Micro-Agent 面向垂域专业 Agent 快速交付，开箱即用 API 服务，内置 Skills/RAG/MCP，配置即可启动。

**Q: Micro-Agent 与 AutoGen 有什么区别？**  
A: AutoGen（原版）已进入维护模式，新项目建议使用 Microsoft Agent Framework；Micro-Agent 面向单 Agent 专业服务，AutoGen 面向多角色智能体协作。

**Q: Micro-Agent 与 OpenClaw 有什么区别？**  
A: OpenClaw 面向个人自主 AI 助手，强调个性化与自主执行；Micro-Agent 面向垂域专业 Agent 服务交付，强调 API 化与轻量化。

### 安装与配置

**Q: 如何安装 Micro-Agent？**  
A: 克隆仓库后安装依赖，配置 `.env` 文件填入 API Key：
```bash
git clone https://github.com/fdueblab/Micro-Agent.git
cd Micro-Agent && pip install -e ".[dev]"
cp .env.example .env
```

**Q: 支持哪些 LLM 提供商？**  
A: 通过 litellm 统一接口，支持 OpenAI、DeepSeek、Claude、Ollama、OpenRouter 等任意模型。配置格式为 `provider/model-name`。

**Q: 如何配置多 LLM Profile？**  
A: 在 `config/config.toml` 中定义多个 `[llm.profile_name]` 配置段，任务中通过 `llm_profile` 参数指定。

**Q: 如何使用本地模型？**  
A: 配置 Ollama 作为 provider（`LLM_MODEL=ollama/qwen2.5`），确保 Ollama 服务已启动。

### 功能使用

**Q: 如何注入垂域知识？**  
A: 在 `workspace/skills/` 目录下创建 `SKILL.md` 文件，Agent 启动时会自动发现并注入到 system prompt。

**Q: 如何使用 RAG 检索增强？**  
A: 将领域文档放入 `workspace/knowledge/` 目录，EmbeddingRetriever 会检索相关文档作为推理上下文。

**Q: 如何集成 MCP 工具？**  
A: Micro-Agent 内置 MCP 工具支持（stdio/SSE），通过配置连接 MCP 服务器即可。

**Q: 如何定义自定义任务？**  
A: 编写 Jinja2 模板（`task/templates/`），注册任务配置（`task/builtin.py`），通过 API 调用。

### 故障排查

**Q: Agent 启动失败，提示 API Key 无效？**  
A: 检查 `.env` 文件中的 `LLM_API_KEY` 配置，Ollama 本地模型可填任意值。

**Q: RAG 检索无结果？**  
A: 确保 `workspace/knowledge/` 目录下有文档文件，检查文档格式（支持 .txt/.md/.pdf）。

**Q: MCP 工具连接失败？**  
A: 检查 MCP 服务器是否启动，确认连接配置正确（stdio 或 SSE 模式）。

**Q: 如何调试 Agent 执行过程？**  
A: 设置环境变量 `DEBUG=true` 启用详细日志，SSE 流式输出可通过 `/api/tasks` 端点查看。

### 更多帮助

更多问题请查阅 [GitHub Issues](https://github.com/fdueblab/Micro-Agent/issues) 或提交新 Issue。

---
## 许可

[MIT](LICENSE)
