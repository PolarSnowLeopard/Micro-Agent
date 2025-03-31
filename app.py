import asyncio
import json
import threading
import os

from flask import Flask, Response, stream_with_context, send_from_directory
from flask_restx import Api, Resource, fields 
from flask_cors import CORS 
 
app = Flask(__name__) 
CORS(app) 
api = Api(app, version='1.0', 
          title='代码解析-微服务封装-远程部署 Agent调用', 
          description='run ioeb agent',
          doc='/') 
 
ns = api.namespace('agent', description='ioeb agent service') 
 
agent_model = api.model('AgentModel', {}) 
 
# 添加流式传输命名空间
stream_ns = api.namespace('stream', description='流式Agent执行')

# 添加静态文件路由
@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

# 添加演示页面路由
@app.route('/stream_demo')
def stream_demo():
    return send_from_directory('static', 'stream_demo.html')

@ns.route('/code_analysis') 
class CodeAnalysis(Resource): 
    @ns.doc(description='code analysis') 
    def get(self): 
        from main import run_agent
        from app.prompt.task import CODE_ANALYSIS_PROMPT
        task_name = "code_analysis"
        prompt = CODE_ANALYSIS_PROMPT
        
        # 创建异步任务队列
        result_ready = threading.Event()
        error_message = [None]
        
        # 在单独线程中运行异步代码
        def run_async_task():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(run_agent(task_name, prompt))
                loop.close()
            except Exception as e:
                error_message[0] = str(e)
            finally:
                result_ready.set()
            
        thread = threading.Thread(target=run_async_task)
        thread.start()
        
        # 最多等待180秒
        if not result_ready.wait(timeout=180):
            return {'error': '任务执行超时'}, 500
            
        if error_message[0]:
            return {'error': error_message[0]}, 500
        
        # 读取结果
        try:
            with open('visualization/function.json', 'r', encoding='utf-8') as f:
                function = f.read()
            with open(f'visualization/{task_name}_record.json', 'r', encoding='utf-8') as f:
                record = f.read()
            return {'result': {'function': function, 'record': record}}
        except Exception as e:
            return {'error': f'读取结果文件失败: {str(e)}'}, 500
    
@ns.route('/service_packaging') 
class ServicePackaging(Resource): 
    @ns.doc(description='service packaging') 
    def get(self): 
        from main import run_agent
        from app.prompt.task import SERVICE_PACKAGING_PROMPT
        task_name = "service_packaging"
        prompt = SERVICE_PACKAGING_PROMPT

        # 创建异步任务队列
        result_ready = threading.Event()
        error_message = [None]
        
        # 在单独线程中运行异步代码
        def run_async_task():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(run_agent(task_name, prompt))
                loop.close()
            except Exception as e:
                error_message[0] = str(e)
            finally:
                result_ready.set()
            
        thread = threading.Thread(target=run_async_task)
        thread.start()
        
        # 最多等待180秒
        if not result_ready.wait(timeout=180):
            return {'error': '任务执行超时'}, 500
            
        if error_message[0]:
            return {'error': error_message[0]}, 500
        
        # 读取结果
        try:
            with open(f'visualization/{task_name}_record.json', 'r', encoding='utf-8') as f:
                record = f.read()
            try:
                with open('visualization/function.json', 'r', encoding='utf-8') as f:
                    function = f.read()
                return {'result': {'function': function, 'record': record}}
            except FileNotFoundError:
                # function.json 可能不存在，只返回 record
                return {'result': {'record': record}}
        except Exception as e:
            return {'error': f'读取结果文件失败: {str(e)}'}, 500

@ns.route('/remote_deploy') 
class RemoteDeploy(Resource): 
    @ns.doc(description='remote deploy') 
    def get(self): 
        from main import run_agent
        from app.prompt.task import REMOTE_DEPLOY_PROMPT
        task_name = "remote_deploy"
        prompt = REMOTE_DEPLOY_PROMPT
        
        # 创建异步任务队列
        result_ready = threading.Event()
        error_message = [None]
        
        # 在单独线程中运行异步代码
        def run_async_task():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(run_agent(task_name, prompt))
                loop.close()
            except Exception as e:
                error_message[0] = str(e)
            finally:
                result_ready.set()
            
        thread = threading.Thread(target=run_async_task)
        thread.start()
        
        # 最多等待180秒
        if not result_ready.wait(timeout=180):
            return {'error': '任务执行超时'}, 500
            
        if error_message[0]:
            return {'error': error_message[0]}, 500
        
        # 读取结果
        try:
            with open(f'visualization/{task_name}_record.json', 'r', encoding='utf-8') as f:
                record = f.read()
            try:
                with open('visualization/function.json', 'r', encoding='utf-8') as f:
                    function = f.read()
                return {'result': {'function': function, 'record': record}}
            except FileNotFoundError:
                # function.json 可能不存在，只返回 record
                return {'result': {'record': record}}
        except Exception as e:
            return {'error': f'读取结果文件失败: {str(e)}'}, 500

@stream_ns.route('/run/<string:task_name>')
class StreamRun(Resource):
    @stream_ns.doc(description='以流式方式运行Agent', params={'task_name': '任务名称'})
    def get(self, task_name):
        from run_mcp import MCPRunner
        from app.prompt.task import CODE_ANALYSIS_PROMPT, SERVICE_PACKAGING_PROMPT, REMOTE_DEPLOY_PROMPT
        
        # 根据任务名称选择prompt
        prompts = {
            "code_analysis": CODE_ANALYSIS_PROMPT,
            "service_packaging": SERVICE_PACKAGING_PROMPT,
            "remote_deploy": REMOTE_DEPLOY_PROMPT,
            "system_info": "我想知道当前机器的一些信息，比如cpu、内存、磁盘、网络等"
        }
        
        if task_name not in prompts:
            return {'error': f'未知的任务名称: {task_name}'}, 400
            
        prompt = prompts[task_name]
        agent_name = f'{task_name.replace("_", " ").capitalize()} Agent'
        
        # 创建异步生成器转换为同步生成器的包装函数
        def generate():
            # 这个队列用于在异步和同步代码之间传递数据
            queue = asyncio.Queue()
            stop_event = threading.Event()
            
            # 异步运行Agent并将结果放入队列
            async def run_async_stream():
                try:
                    runner = MCPRunner(agent_name)
                    await runner.initialize("stdio", None)
                    
                    async for step_result in runner.run_stream(prompt):
                        # 将步骤结果转换为JSON字符串
                        json_result = json.dumps(step_result)
                        await queue.put(f"data: {json_result}\n\n")
                        
                        # 如果是最后一个结果，保存完整记录
                        if step_result.get("is_last", False):
                            full_result = await runner.agent.run(None)  # 获取完整结果但不重新执行
                            full_json = json.dumps(full_result, ensure_ascii=False)
                            # 保存完整记录到文件
                            from app.utils.visualize_record import save_record_to_json, generate_visualization_html
                            save_record_to_json(task_name, full_json)
                            generate_visualization_html(task_name)
                    
                    # 完成时通知队列结束
                    await queue.put(None)
                    
                except Exception as e:
                    error_msg = f"执行出错: {str(e)}"
                    await queue.put(f"data: {json.dumps({'error': error_msg, 'is_last': True})}\n\n")
                    await queue.put(None)
                finally:
                    if runner:
                        await runner.cleanup()
            
            # 在新线程中运行事件循环
            def run_event_loop():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(run_async_stream())
                loop.close()
                stop_event.set()
            
            # 启动异步处理线程
            thread = threading.Thread(target=run_event_loop)
            thread.start()
            
            # 从队列获取结果并生成
            while not stop_event.is_set() or not queue.empty():
                try:
                    # 使用timeout避免阻塞
                    future = asyncio.run_coroutine_threadsafe(queue.get(), asyncio.get_event_loop())
                    result = future.result(timeout=0.1)
                    if result is None:  # 结束信号
                        break
                    yield result
                except asyncio.TimeoutError:
                    # 超时时继续等待
                    pass
                except Exception as e:
                    yield f"data: {json.dumps({'error': f'生成器出错: {str(e)}', 'is_last': True})}\n\n"
                    break
        
        # 返回SSE流响应
        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no'  # 禁用Nginx缓冲
            }
        )

if __name__ == '__main__': 
    app.run(host='0.0.0.0', port=8003) 
