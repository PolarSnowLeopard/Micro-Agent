import asyncio
import json
import threading

from flask import Flask 
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
 
if __name__ == '__main__': 
    app.run(host='0.0.0.0', port=8003) 
