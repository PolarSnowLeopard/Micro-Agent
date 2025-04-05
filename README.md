# Micro-Agent 智能体框架

<div align="center">

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.12-blue)

</div>

Micro-Agent 是一个灵活、可扩展的智能体框架，专为构建各种基于大语言模型的智能应用而设计。"Micro"意为"微"，与微服务中的"微"同义，代表其轻量级、模块化的特性。

## 📌 特性概览

- **现代智能体架构**：基于大语言模型构建的灵活智能代理系统
- **模型上下文协议 (MCP) 支持**：集成 MCP 协议，提供标准化工具调用机制
- **多服务器连接**：支持同时连接多个 MCP 服务器，灵活扩展工具生态
- **强大的内置工具集**：包含文件操作、命令执行、远程部署等常用工具
- **流式处理**：支持实时流式输出，提升交互体验
- **可视化界面**：提供基于 Web 的执行过程可视化
- **任务模板系统**：支持预定义任务模板，快速创建特定功能的智能体

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

## 🧠 系统架构

Micro-Agent 框架由以下主要组件构成：

### 智能体系统

- **BaseAgent**：所有智能体的基础类
- **ToolCallAgent**：支持工具调用的智能体
- **ReActAgent**：基于 ReAct 策略的智能体
- **MCPAgent**：支持 MCP 协议的智能体，可连接多个服务器

### 工具系统

- **BaseTool**：工具基类，定义标准接口
- **ToolCollection**：工具集合，支持工具分组管理
- **MCPClientTool**：MCP 客户端工具，支持远程工具调用
- **内置工具**：文件操作、命令执行、远程部署等

### MCP 系统

- **MCPServer**：集成旧工具的 MCP 服务器实现
- **MCPClients**：管理多个 MCP 服务器连接
- **多服务器支持**：同时连接多个服务器，汇集不同来源的工具

### 支持系统

- **提示系统**：智能体的系统提示和任务提示
- **语言模型接口**：封装 LLM API 调用
- **配置系统**：统一管理系统配置
- **日志系统**：单例模式设计的多实例日志系统

## 📊 示例应用

目前，框架已实现以下示例应用：

1. **代码分析 Agent**：

   - 自动分析代码结构和依赖关系
   - 生成代码依赖可视化图表
   - 输出详细的代码分析报告

2. **服务评估 Agent**：

   - 对微服务进行性能和质量评估
   - 生成服务评估报告
   - 提供性能优化建议

3. **元应用验证 Agent**：

   - 验证元应用 API 的数据质量和一致性
   - 生成数据验证报告
   - 识别潜在的数据问题

4. **AML 模型评估 Agent**：

   - 评估反洗钱(AML)模型的性能和准确性
   - 生成模型评估报告
   - 提供模型优化建议

5. **系统信息 Agent**：
   - 获取系统硬件和软件信息
   - 分析系统资源使用情况

## 💻 Web 界面与 API

Micro-Agent 提供了基于 FastAPI 的流式执行服务，可通过 Web 界面直观地监控智能体执行过程。

### 主要功能

- **实时流式输出**：通过 SSE 实时展示智能体执行过程
- **Web 界面**：直观展示执行步骤和结果
- **文件上传**：支持上传 ZIP 文件进行代码分析
- **结果可视化**：可视化展示执行结果
- **多任务支持**：提供多种预定义任务模板

### 访问方式

服务默认在 8010 端口启动，可通过以下方式访问：

- **API 文档**：http://localhost:8010/
- **演示界面**：http://localhost:8010/stream_demo
- **文件上传**：http://localhost:8010/upload_demo

### API 端点

- **GET /stream/run/{task_name}**：流式执行指定任务
- **POST /api/agent/code_analysis**：上传 ZIP 文件并执行代码分析
- **POST /api/agent/service_evaluation**：评估微服务性能和质量
- **POST /api/agent/meta_app_validation**：验证元应用 API 的数据
- **POST /api/agent/aml_model_evaluation**：评估 AML 模型性能

## 🔧 配置选项

Micro-Agent 使用两个主要配置文件：

1. **`.env`文件**：环境变量和敏感信息

   - 远程服务器连接信息
   - API 密钥
   - 其他环境变量

2. **`config/config.toml`文件**：系统配置

   - LLM 模型配置（模型名称、参数等）
   - 任务相关配置
   - 应用程序设置

3. **`config.json`文件**：MCP 服务器配置
   - 服务器连接配置
   - 连接类型和参数

## 📝 日志和调试

系统日志存储在`logs/`目录下，提供多种使用方式：

```python
# 使用默认日志实例
from app.logger import logger
logger.info("信息日志")

# 创建命名日志实例
from app.logger import Logger
custom_logger = Logger("my_module")
custom_logger.info("自定义日志")

# 配置日志级别
logger.define_log_level(print_level="DEBUG", logfile_level="INFO")
```

## 🌟 MCP 功能详解

Micro-Agent 集成了模型上下文协议(Model Context Protocol, MCP)，提供标准化的工具调用机制。

### 主要特性

- **多服务器连接**：支持同时连接多个 MCP 服务器
- **工具转发**：将任何 MCP 服务器工具转发给智能体使用
- **内置 MCP 服务器**：默认提供内置 MCP 服务器
- **灵活配置**：支持 stdio 和 SSE 连接方式

### 使用方法

```python
# 在代码中使用MCPRunner
from run_mcp import MCPRunner

# 创建runner实例
runner = MCPRunner("My Agent")

# 添加内置服务器（默认自动添加）
server_id = await runner.add_server(
    connection_type="stdio",
    server_id="stdio_built_in"
)

# 添加自定义服务器
custom_server = await runner.add_server(
    connection_type="sse",
    server_url="http://localhost:8000/sse"
)

# 运行智能体
result = await runner.agent.run("执行任务")

# 使用流式输出运行智能体
async for step_result in runner.run_stream("执行任务"):
    print(step_result)
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
