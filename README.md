<div align="center">

# Micro-Agent 智能体框架

![GitHub Stars](https://img.shields.io/github/stars/PolarSnowLeopard/Micro-Agent?style=flat&logo=github)
![GitHub Forks](https://img.shields.io/github/forks/PolarSnowLeopard/Micro-Agent?style=flat&logo=github)
![Version](https://img.shields.io/badge/version-1.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.12-blue)

</div>

Micro-Agent 是一个灵活、可扩展的智能体框架，专为构建各种基于大语言模型的智能应用而设计。"Micro"意为"微"，与微服务中的"微"同义，代表其轻量级、模块化的特性。

## 📌 特性概览

- **自主智能体架构 (Autonomous Agent Architecture)**：采用 ReAct 框架构建自主决策的智能体系统，有别于传统的工作流 (Workflow) 模式，智能体能够自主思考、规划和执行任务
- **MCP 协议支持**：使用模型上下文协议 (MCP) 为智能体集成工具，支持连接多个 MCP 服务器，灵活扩展工具生态系统
- **内置 Shell 交互环境**：为智能体提供完整的 Shell 交互环境，使其能够感知和操作系统环境，执行文件操作、命令调用等任务
- **标准化智能体调用接口**：Micro-Agent 专注于构建面向任务的智能体供上层应用调用，而非对话式聊天助手，提供封装完善的流式处理接口和标准化调用方式
- **执行过程可视化**：提供基于 Web 的智能体执行过程可视化界面，实时展示思考过程、行动步骤和执行结果

## 🔮 路线图

- **多智能体强化学习框架**：正在开发支持多个智能体协作和强化学习的框架，提升智能体的学习和适应能力
- **A2A 标准化协议集成**：计划集成 Agent-to-Agent (A2A) 等标准化智能体交互协议，实现智能体间的高效通信和协作
- **增强型可视化界面**：开发更加易用和功能丰富的可视化界面，提供智能体管理、任务配置和结果分析等功能

## 🚀 快速开始

### 通过 Docker 运行（推荐）

```bash
# 克隆项目
git clone https://github.com/PolarSnowLeopard/Micro-Agent
cd Micro-Agent

# 配置环境
cp .env.example .env
cp config/config.example.toml config/config.toml
# 编辑配置文件，设置API密钥等

# 启动容器
docker-compose up -d
docker-compose exec micro_agent bash

# 在容器内运行
python main.py
# 或启动Web界面
python app.py  # 访问 http://localhost:8010
```

### 本地环境运行

```bash
# 克隆项目
git clone https://github.com/PolarSnowLeopard/Micro-Agent
cd Micro-Agent

# 创建Python环境（推荐3.12）
conda create -n micro-agent python=3.12
conda activate micro-agent

# 配置环境
cp .env.example .env
cp config/config.example.toml config/config.toml
# 编辑配置文件，设置API密钥等

# 安装依赖
pip install -r requirements.txt

# 运行项目
python main.py
# 或启动Web界面
python app.py  # 访问 http://localhost:8010
```

> ⚠️ **安全提示**：由于本项目可能执行系统命令，强烈建议在容器环境中运行，以避免对宿主系统的潜在影响。

### 使用 main.py 构建智能体

`main.py` 是框架的核心入口，可以快速利用已有的 MCP 服务器构建智能体。

#### 1. 配置 MCP 服务器

在 `config.json` 文件中配置要连接的 MCP 服务器：

```json
{
  "servers": [
    {
      "connection_type": "sse",
      "server_url": "http://your-server.com/sse",
      "command": null,
      "args": null,
      "server_id": "remote_sse_server"
    },
    {
      "connection_type": "stdio",
      "server_url": null,
      "command": "python",
      "args": ["-m", "your_module.mcp_server"],
      "server_id": "local_stdio_server"
    }
  ]
}
```

**MCP 服务器配置说明：**

- `connection_type`: 连接类型，支持 `"sse"` 和 `"stdio"`
- `server_url`: SSE 服务器 URL（仅用于 SSE 类型）
- `command`: 启动命令（仅用于 stdio 类型）
- `args`: 命令参数（仅用于 stdio 类型）
- `server_id`: 服务器标识符（可选，自动生成）

**连接类型详解：**

1. **SSE 连接**：适用于远程 MCP 服务器
   - 通过 HTTP/SSE 协议连接
   - 需要提供 `server_url`
   - 适合连接部署在其他服务器上的 MCP 服务

2. **Stdio 连接**：适用于本地 MCP 服务器
   - 通过子进程启动 MCP 服务器
   - 需要提供 `command` 和 `args`
   - 适合运行本地 Python 模块作为 MCP 服务

#### 2. 编写提示词和配置智能体

在 `main.py` 的 `if __name__ == "__main__":` 部分直接修改智能体配置：

```python
if __name__ == "__main__":
    # 直接配置智能体名称（可以是任何你想要的名称）
    agent_name = "Code Analysis Agent"  # 或 "我的智能助手" 等
    
    # 定义任务提示词
    prompt = """
你是一个专业的代码分析师。
请分析给定的代码库结构，识别主要功能模块，并生成分析报告。

请按以下步骤执行：
1. 检查项目文件结构
2. 分析主要代码文件
3. 识别依赖关系
4. 生成分析报告
"""
    
    # 运行智能体（task_name用于生成保存的文件名）
    asyncio.run(run_agent("code_analysis", prompt))
```

**配置说明：**
- `agent_name`: 智能体显示名称，可以任意自定义
- `prompt`: 任务提示词，定义智能体的角色和任务
- `task_name`（第一个参数）: 用于生成保存文件的名称

#### 3. 智能体执行流程

智能体会自动：
1. 连接配置的所有 MCP 服务器
2. 获取可用工具列表
3. 根据提示词执行任务
4. 保存执行记录到 JSON 文件
5. 生成可视化 HTML 报告

#### 4. 查看可视化结果

执行结果文件会保存在 `visualization/` 目录下：
- `{task_name}_record.json`: 执行记录（JSON格式）
- `{task_name}.html`: 可视化报告（HTML格式）

**查看可视化报告：**

```bash
# 切换到可视化目录
cd visualization

# 启动本地HTTP服务器
python -m http.server 8000

# 在浏览器中访问
# http://localhost:8000/{task_name}.html
```

## 📚 文档

项目提供完整的文档中心，可通过以下方式访问：

1. **在线文档**：访问 [fdueblab.cn/docs](https://fdueblab.cn/docs)

2. **本地文档**：
   ```bash
   cd docs
   python -m http.server 8000
   # 访问 http://localhost:8000
   ```

## 👥 贡献指南

欢迎为 Micro-Agent 项目做出贡献！贡献方式包括：

- 提交 Bug 报告
- 提供新功能建议
- 改进文档
- 提交代码修复或新功能实现

请通过 GitHub Issues 或 Pull Requests 参与项目贡献。

## 📄 许可证

[MIT 许可证](LICENSE)

---

<div align="center">
  <small>© 2023-2024 Micro-Agent Project Team. All rights reserved.</small>
</div>
