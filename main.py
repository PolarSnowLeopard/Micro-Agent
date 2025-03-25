from run_mcp import MCPRunner
import asyncio
import sys
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
        await runner.initialize("stdio", None)
        result = await runner.run_single_prompt(prompt)
        
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
    from app.prompt.task import CODE_ANALYSIS_PROMPT, SERVICE_PACKAGING_PROMPT, REMOTE_DEPLOY_PROMPT

    # task_name = "code_analysis"
    # prompt = CODE_ANALYSIS_PROMPT

    # task_name = "service_packaging"
    # prompt = SERVICE_PACKAGING_PROMPT

    task_name = "remote_deploy"
    prompt = REMOTE_DEPLOY_PROMPT

    asyncio.run(run_agent(task_name, prompt))