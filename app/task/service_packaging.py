import os
from app.config import WORKSPACE_ROOT

WORKSPACE = os.path.join(os.getcwd(),WORKSPACE_ROOT)

def get_service_packaging_prompt(workspace: str = WORKSPACE,
                               input_dir: str = "app-demo-input",
                               main_code: str = "main.py",
                               temp_dir: str = 'temp'):
    return f"""
    任务：将Python代码封装为RESTful API服务并完成容器化部署准备

    项目结构：
    - 工作目录: {workspace}
    - 输入目录: {input_dir}/
        ├── {main_code} (主程序文件)
        └── requirements.txt (依赖文件，可选)

    具体要求：
    1. API封装要求：
       - 分析{main_code}中的功能函数，识别适合作为API端点的函数
       - 使用FastAPI创建app.py文件，要求：
         * 为每个功能函数创建合理的API端点
         * 添加适当的请求/响应模型
         * 配置CORS跨域支持
         * 启用/docs的Swagger UI文档
         * 包含健康检查端点(/health)
         * 添加合理的错误处理

    2. 容器化准备：
       - 创建生产级Dockerfile，要求：
         * 使用合适的基础镜像(如python:3.9-slim)
         * 正确处理依赖安装
         * 暴露正确端口(默认8000)

       - 创建docker-compose.yml，要求：
         * 配置服务名称和端口映射
         * 支持环境变量配置

    3. 文档要求：
       - 生成README.md，包含：
         * 服务功能描述
         * API文档链接
         * 本地运行指南
         * 容器化部署指南
         * 配置项说明

    输出：
    请在{input_dir}目录下生成以下文件：
    - app.py (FastAPI应用)
    - Dockerfile
    - docker-compose.yml
    - README.md

    注意：
    - 保持代码风格一致(PEP 8)
    - 确保所有生成文件符合生产环境最佳实践
    - 如果requirements.txt不存在，请基于{main_code}的导入语句推断依赖
    """
