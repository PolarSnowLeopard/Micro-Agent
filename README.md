# Micro-Agent 智能体框架

<div align="center">

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.12-blue)

</div>

Micro-Agent 是一个灵活、可扩展的智能体框架，专为构建各种基于大语言模型的智能应用而设计。"Micro"意为"微"，与微服务中的"微"同义，代表其轻量级、模块化的特性。

## 📌 特性概览

- **现代智能体架构**：基于大语言模型构建的灵活智能代理系统
- **模型上下文协议 (MCP) 支持**：集成MCP协议，提供标准化工具调用机制
- **多服务器连接**：支持同时连接多个MCP服务器，灵活扩展工具生态
- **强大的内置工具集**：包含文件操作、命令执行、远程部署等常用工具
- **流式处理**：支持实时流式输出，提升交互体验
- **可视化界面**：提供基于Web的执行过程可视化

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
python app.py  # 访问 http://localhost:5000
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
python app.py  # 访问 http://localhost:5000
```

> ⚠️ **安全提示**：由于本项目可能执行系统命令，强烈建议在容器环境中运行，以避免对宿主系统的潜在影响。

## 🧠 系统架构

Micro-Agent 框架由以下主要组件构成：

### 智能体系统

- **BaseAgent**：所有智能体的基础类
- **ToolCallAgent**：支持工具调用的智能体
- **ReActAgent**：基于ReAct策略的智能体
- **MCPAgent**：支持MCP协议的智能体，可连接多个服务器

### 工具系统

- **BaseTool**：工具基类，定义标准接口
- **ToolCollection**：工具集合，支持工具分组管理
- **MCPClientTool**：MCP客户端工具，支持远程工具调用
- **内置工具**：文件操作、命令执行、远程部署等

### MCP系统

- **MCPServer**：集成旧工具的MCP服务器实现
- **MCPClients**：管理多个MCP服务器连接
- **多服务器支持**：同时连接多个服务器，汇集不同来源的工具

### 支持系统

- **提示系统**：智能体的系统提示和任务提示
- **语言模型接口**：封装LLM API调用
- **配置系统**：统一管理系统配置
- **日志系统**：单例模式设计的多实例日志系统

## 📊 示例应用

目前，框架已实现以下示例应用：

1. **代码分析 Agent**：
   - 自动分析代码结构和依赖关系
   - 生成代码依赖可视化图表
   - 输出详细的代码分析报告

2. **服务封装 Agent**：
   - 将现有代码自动封装为微服务
   - 生成 Docker 配置文件
   - 创建微服务部署所需的全部资源

3. **远程部署 Agent**：
   - 将封装好的微服务自动部署到远程服务器
   - 提供部署状态监控
   - 支持部署过程的日志记录和查看

4. **系统信息 Agent**：
   - 获取系统硬件和软件信息
   - 分析系统资源使用情况

## 💻 Web界面与API

Micro-Agent 提供了基于FastAPI的流式执行服务，可通过Web界面直观地监控智能体执行过程。

### 主要功能

- **实时流式输出**：通过SSE实时展示智能体执行过程
- **Web界面**：直观展示执行步骤和结果
- **文件上传**：支持上传ZIP文件进行代码分析
- **结果可视化**：可视化展示执行结果

### 访问方式

服务默认在5000端口启动，可通过以下方式访问：

- **API文档**：http://localhost:5000/
- **演示界面**：http://localhost:5000/stream_demo
- **文件上传**：http://localhost:5000/upload_demo

### API端点

- **GET /stream/run/{task_name}**：流式执行指定任务
- **POST /api/agent/code_analysis**：上传ZIP文件并执行代码分析

## 🔧 配置选项

Micro-Agent 使用两个主要配置文件：

1. **`.env`文件**：环境变量和敏感信息
   - 远程服务器连接信息
   - API密钥
   - 其他环境变量

2. **`config/config.toml`文件**：系统配置
   - LLM模型配置（模型名称、参数等）
   - 视觉模型配置
   - 应用程序设置

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

## 🌟 MCP功能详解

Micro-Agent 集成了模型上下文协议(Model Context Protocol, MCP)，提供标准化的工具调用机制。

### 主要特性

- **多服务器连接**：支持同时连接多个MCP服务器
- **工具转发**：将任何MCP服务器工具转发给智能体使用
- **内置MCP服务器**：默认提供内置MCP服务器
- **灵活配置**：支持stdio和SSE连接方式

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

欢迎为Micro-Agent项目做出贡献！贡献方式包括：

- 提交Bug报告
- 提供新功能建议
- 改进文档
- 提交代码修复或新功能实现

请通过GitHub Issues或Pull Requests参与项目贡献。

## 📄 许可证

[MIT 许可证](LICENSE)

---

<div align="center">
  <small>© 2023-2024 Micro-Agent Project Team. All rights reserved.</small>
</div>
