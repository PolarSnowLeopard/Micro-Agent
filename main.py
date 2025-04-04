from run_mcp import MCPRunner
import asyncio
import sys
import json
from app.logger import logger
from app.utils.visualize_record import save_record_to_json, generate_visualization_html

async def run_agent(task_name: str, prompt: str) -> None:
    """
    执行特定任务的智能体的主入口点。
    
    参数:
        task_name: 任务名称，用于生成JSON和HTML文件名
        prompt: 任务的prompt
    """
    agent_name = f'{task_name.replace("_", " ").capitalize()} Agent'
    runner = MCPRunner(agent_name)
    result = ""
    try:
        # 添加内置MCP服务器
        logger.info("添加默认内置MCP服务器")
        built_in_server_id = await runner.add_server(
            connection_type="stdio",
            server_url=None,
            command=None,  # 使用默认Python解释器
            args=None,     # 使用默认的app.mcp.server模块
            server_id="stdio_built_in"  # 指定一个固定ID以便识别
        )
        logger.info(f"使用内置MCP服务器: {built_in_server_id}")
        
        # 执行智能体任务
        result = await runner.agent.run(prompt)
        result = json.dumps(result, ensure_ascii=False, indent=4)
        
        # 保存JSON记录文件
        save_record_to_json(task_name, result)
            
        # 生成可视化HTML文件
        generate_visualization_html(task_name)
        
        logger.info(f"已完成 {task_name} 任务，结果已保存")

    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"运行MCPAgent时出错: {str(e)}", exc_info=True)
        sys.exit(1)
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    # from app.prompt.task import CODE_ANALYSIS_PROMPT, SERVICE_PACKAGING_PROMPT, REMOTE_DEPLOY_PROMPT

    # task_name = ["code_analysis", "service_packaging", "remote_deploy"]
    # prompt = [CODE_ANALYSIS_PROMPT, SERVICE_PACKAGING_PROMPT, REMOTE_DEPLOY_PROMPT]

    # for task, prompt in zip(task_name, prompt):
    #     asyncio.run(run_agent(task, prompt))

    prompt = "请列出你可以使用的mcp工具，然后直接结束"
    asyncio.run(run_agent("system_info", prompt))
