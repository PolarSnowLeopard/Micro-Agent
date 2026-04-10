"""内置任务注册。import 此模块即可将所有预定义任务注册到全局注册表。"""

from micro_agent.task.base import TaskConfig, register_task

AGENT_SYSTEM_PROMPT = "你是一个专业的软件工程 Agent，能够分析代码、封装服务、评测系统。请使用可用的工具完成用户任务，完成后使用 terminate 工具返回最终结果。"

register_task(TaskConfig(
    name="code_analysis",
    description="分析代码结构，解析功能函数信息和依赖关系",
    prompt_template="code_analysis.md.j2",
    system_prompt=AGENT_SYSTEM_PROMPT,
    max_steps=30,
))

register_task(TaskConfig(
    name="service_packaging",
    description="将 Python 代码封装为 MCP 服务并容器化",
    prompt_template="service_packaging.md.j2",
    system_prompt=AGENT_SYSTEM_PROMPT,
    max_steps=40,
))

register_task(TaskConfig(
    name="mcp_test",
    description="测试指定 MCP Server 的功能",
    prompt_template="mcp_test.md.j2",
    system_prompt=AGENT_SYSTEM_PROMPT,
    max_steps=30,
))

register_task(TaskConfig(
    name="service_evaluation",
    description="对远程微服务进行技术指标评测",
    prompt_template="service_evaluation.md.j2",
    system_prompt=AGENT_SYSTEM_PROMPT,
    max_steps=30,
))

register_task(TaskConfig(
    name="mcp_service_recommendation",
    description="根据用户需求推荐合适的 MCP 服务",
    prompt_template="mcp_service_recommendation.md.j2",
    system_prompt=AGENT_SYSTEM_PROMPT,
    max_steps=30,
))

register_task(TaskConfig(
    name="meta_app_validation",
    description="对元应用进行数据验证评测",
    prompt_template="meta_app_validation.md.j2",
    system_prompt=AGENT_SYSTEM_PROMPT,
    max_steps=30,
))

register_task(TaskConfig(
    name="aml_report",
    description="对输入数据集进行 AML 风险预测并生成报告",
    prompt_template="aml_report.md.j2",
    system_prompt=AGENT_SYSTEM_PROMPT,
    max_steps=30,
))

register_task(TaskConfig(
    name="aml_model_evaluation",
    description="对 AML 模型进行多维度技术评测",
    prompt_template="aml_model_evaluation.md.j2",
    system_prompt=AGENT_SYSTEM_PROMPT,
    max_steps=30,
))
