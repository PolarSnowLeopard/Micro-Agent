# Micro-Agent 智能体框架

本项目是一个灵活、可扩展的智能体框架，旨在为上层应用提供智能代理能力。Micro-Agent 中的"Micro"意为"微"，与微服务中的"微"同义，代表其轻量级、模块化的特性。该框架可用于构建各种智能应用，目前已实现的示例包括代码分析、微服务封装及远程部署等功能。

## 框架特性

Micro-Agent 框架的核心特性包括：

1. **灵活的智能体架构**：

   - 支持自定义智能体
   - 提供继承体系便于扩展
   - 灵活的工具调用机制

2. **强大的工具系统**：

   - Python 执行工具
   - 命令行工具
   - 文件操作工具
   - 远程连接工具
   - 支持自定义工具开发

3. **基于大语言模型的推理能力**：

   - 支持 OpenAI 等 LLM 接口
   - 可扩展的 LLM 适配器

4. **完整的开发文档**：
   - 详细的架构说明
   - 工具开发指南
   - 示例代码

## 已实现的示例应用

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

## 系统架构

Micro-Agent 框架的主要组件包括：

- 智能体系统（BaseAgent, ReActAgent, ToolCallAgent 等）
- 工具系统（BaseTool 及各种具体工具实现）
- 提示系统
- 语言模型接口
- 配置系统

## 开发与使用

### 安全建议

由于 Micro-Agent 框架可能会使用 bash/命令行等工具对系统进行操作，为了安全起见，**强烈建议**在容器环境中运行本项目，以避免可能的不可控操作对宿主系统造成影响。

### 通过 Docker 运行（推荐）

1. 克隆仓库：

   ```bash
   git clone https://github.com/PolarSnowLeopard/Micro-Agent
   cd Micro-Agent
   ```

2. 创建并配置环境变量文件：

   ```bash
   cp .env.example .env
   cp config/config.example.toml config/config.toml
   # 编辑.env和config/config.toml文件，设置所需的API密钥和配置参数
   ```

3. 使用 docker-compose 启动服务：

   ```bash
   docker-compose up -d  # 后台运行
   docker-compose exec micro_agent bash  # 进入容器
   ```

4. 在容器内运行您的 Agent：
   ```bash
   python main.py
   ```

### 直接在本地环境运行（不推荐）

如果您确实需要在本地环境运行，请谨慎操作并了解可能的风险：

### 开发环境设置

- 建议 python 版本为 3.12.

1. 克隆仓库：

   ```bash
   git clone https://github.com/PolarSnowLeopard/Micro-Agent
   cd Micro-Agent
   ```

2. 使用 conda 创建 python 环境（可选）：

   ```bash
   conda create -n micro-agent python=3.12
   conda activate micro-agent
   ```

3. 创建并配置环境变量文件：

   ```bash
   cp .env.example .env
   cp config/config.example.toml config/config.toml
   # 编辑.env和config/config.toml文件，设置所需的API密钥和配置参数
   ```

4. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

### 文档中心

项目提供了完整的文档中心，可通过以下方式访问(或访问线上文档[fdueblab.cn/docs](https://fdueblab.cn/docs))：

1. 启动文档服务器：

   ```bash
   cd docs
   python -m http.server 8000
   ```

2. 浏览器访问：`http://localhost:8000`

文档中心包含详细的系统架构说明、API 文档、智能体开发指南以及工具开发指南。有关自定义开发、创建自定义智能体和工具的具体方法，请参考文档中心的相关章节。

## 配置选项

Micro-Agent 框架使用两个主要的配置文件：

1. **`.env`文件**：用于存储环境变量和敏感信息

   - 从`.env.example`复制得到：`cp .env.example .env`
   - 包含的主要配置：
     - `REMOTE_SSH_SERVER`: 远程服务器地址
     - `REMOTE_SSH_USERNAME`: 远程服务器用户名
     - `REMOTE_SSH_PASSWORD`: 远程服务器密码

2. **`config/config.toml`文件**：用于系统主要配置
   - 从`config/config.example.toml`复制得到：`cp config/config.example.toml config/config.toml`
   - 包含的主要配置：
     - LLM 模型配置（API 密钥、模型名称、参数等）
     - 视觉模型配置
     - 代理配置（可选）

在使用系统前，请确保这两个配置文件都已正确设置。特别是需要添加您的 LLM API 密钥以及其他必要的连接信息。

## 日志和调试

系统日志存储在`logs/`目录下，可用于故障排查和系统监控。

## Agent流式执行服务

本项目现在包含一个基于FastAPI的智能体流式执行服务，它提供了实时监控和查看智能体执行过程的能力。

### 主要特性

- **实时流式输出**：通过SSE (Server-Sent Events) 实时展示智能体执行过程
- **直观的Web界面**：提供友好的Web界面展示执行步骤和结果
- **任务最终结果展示**：支持展示任务特定的最终输出文件
- **错误和警告处理**：当文件不存在或读取失败时提供适当的错误和警告信息

### 启动服务

```bash
# 启动FastAPI服务
python app.py
```

服务默认在5000端口启动，可通过以下方式访问：

- API文档：http://localhost:5000/
- 演示页面：http://localhost:5000/stream_demo
- 文件上传演示：http://localhost:5000/upload_demo

### 示例任务

演示页面提供了以下几个示例任务：

1. **代码分析**：分析代码结构和功能，生成function.json
2. **服务封装**：将代码封装为微服务
3. **远程部署**：将服务部署到远程服务器
4. **系统信息**：获取系统基本信息
5. **列出工具**：显示Agent可以使用的所有工具

### API端点

服务提供以下API端点：

1. **GET /stream/run/{task_name}**: 流式执行指定任务
   - 支持的任务: code_analysis, service_packaging, remote_deploy, system_info, list_tools
   - 返回SSE格式的流式数据

2. **POST /api/agent/code_analysis**: 上传ZIP文件并执行代码分析
   - 接收一个ZIP格式的文件作为输入
   - 将文件解压到临时目录
   - 执行代码分析任务
   - 返回与GET端点相同格式的流式数据

3. **GET /upload_demo**: 文件上传演示页面
   - 提供文件上传和代码分析的Web界面

### 注意事项

- 本服务只提供FastAPI实现，不再支持Flask实现
- 流式输出使用SSE技术，确保浏览器支持EventSource API
- 任务执行结果会保存在visualization目录下

### 自定义任务

如需添加新的任务类型，请在`app.py`文件中修改`task_configs`字典，添加新的任务配置：

```python
task_configs = {
    "your_task_name": {
        "prompt": YOUR_TASK_PROMPT,
        "outputs": [
            {"name": "output_name", "file": "path/to/output/file.json"}
        ]
    },
    # ... 其他任务
}
```

## 贡献指南

欢迎为Micro-Agent项目做出贡献！贡献方式包括但不限于：

- 提交Bug报告
- 提供新功能建议
- 改进文档
- 提交代码修复或新功能实现

请通过GitHub Issues或Pull Requests参与项目贡献。

## 许可证

[MIT许可证](LICENSE)
