import os
from app.config import WORKSPACE_ROOT

WORKSPACE = os.path.join(os.getcwd(),WORKSPACE_ROOT)
INPUT_DIR = "app-demo-input"
MAIN_CODE = "main.py"
OUTPUT_DIR = "app-demo-output"
TEMP_DIR = "visualization"
FUNCTION_INFO_PATH = "function.json"
SELECTED_FUNCTIONS = ['func2', 'func5']

def get_code_analysis_prompt(workspace: str = WORKSPACE,
                             input_dir: str = "app-demo-input", 
                             main_code: str = "main.py", 
                             temp_dir: str = 'temp', 
                             function_info_path: str = 'function.json'):
    return f"""`{workspace}`路径下有一个`{input_dir}`文件夹，内有`{main_code}`和`requirements.txt`两个文件(如果requirements.txt不存在，则忽略)。`{main_code}`当中包含待封装为REST API的功能函数，`requirements.txt`则是对应的依赖环境。请你分析`{main_code}`当中的代码逻辑，解析其中功能函数的相关信息和依赖关系，并在`{temp_dir}`文件夹下创建一个`{function_info_path}`文件。该文件的格式类似以下示例：

```json
{{
    nodes: [
        {{ id: '9001', x: 0, y: 150, label: 'datasets', size: 50, input: 'rawData', output: 'processedData', environment: '', process: '', apiType: 0, methodType: 0, inputType: 2, outputType: 2 }},
        {{ id: '9002', x: 150, y: 150, label: 'preprocess', size: 50, input: 'processedData', output: 'cleanedData', environment: '', process: '', apiType: 0, methodType: 1, inputType: 2, outputType: 2 }},
        {{ id: '9003', x: 300, y: 150, label: 'train', size: 50, input: 'cleanedData', output: 'trainedModel', environment: '', process: '', apiType: 2, methodType: 1, inputType: 2, outputType: 2 }},
        {{ id: '9004', x: 450, y: 50, label: 'predict', size: 50, input: 'trainedModel', output: 'predictionResult', environment: '', process: '', apiType: 0, methodType: 0, inputType: 2, outputType: 1 }},
        {{ id: '9005', x: 450, y: 150, label: 'evaluate', size: 50, input: 'trainedModel', output: 'evaluationMetrics', environment: '', process: '', apiType: 0, methodType: 1, inputType: 2, outputType: 1 }},
        {{ id: '9006', x: 450, y: 250, label: 'visualize', size: 50, input: 'trainedModel', output: 'visualization', environment: '', process: '', apiType: 0, methodType: 1, inputType: 2, outputType: 3 }},
        {{ id: '9007', x: 300, y: 250, label: 'models', size: 50, input: 'trainedModel', output: 'modelMetadata', environment: '', process: '', apiType: 0, methodType: 1, inputType: 2, outputType: 1 }}
    ],
    edges: [
        {{ sourceID: '9001', targetID: '9002' }},
        {{ sourceID: '9002', targetID: '9003' }},
        {{ sourceID: '9003', targetID: '9004' }},
        {{ sourceID: '9003', targetID: '9005' }},
        {{ sourceID: '9003', targetID: '9006' }},
        {{ sourceID: '9003', targetID: '9007' }},
        {{ sourceID: '9007', targetID: '9004' }},
        {{ sourceID: '9007', targetID: '9005' }},
        {{ sourceID: '9007', targetID: '9006' }}
    ]
}}
```"""

CODE_ANALYSIS_PROMPT = f"""`{WORKSPACE}`路径下有一个`{INPUT_DIR}`文件夹，内有`{MAIN_CODE}`和`requirements.txt`两个文件。`{MAIN_CODE}`当中包含待封装为REST API的功能函数，`requirements.txt`则是对应的依赖环境。请你分析`{MAIN_CODE}`当中的代码逻辑，解析其中功能函数的相关信息和依赖关系，并在`{TEMP_DIR}`文件夹下创建一个`{FUNCTION_INFO_PATH}`文件。该文件的格式类似以下示例：

```json
{{
    nodes: [
        {{ id: '9001', x: 0, y: 150, label: 'datasets', size: 50, input: 'rawData', output: 'processedData', environment: '', process: '', apiType: 0, methodType: 0, inputType: 2, outputType: 2 }},
        {{ id: '9002', x: 150, y: 150, label: 'preprocess', size: 50, input: 'processedData', output: 'cleanedData', environment: '', process: '', apiType: 0, methodType: 1, inputType: 2, outputType: 2 }},
        {{ id: '9003', x: 300, y: 150, label: 'train', size: 50, input: 'cleanedData', output: 'trainedModel', environment: '', process: '', apiType: 2, methodType: 1, inputType: 2, outputType: 2 }},
        {{ id: '9004', x: 450, y: 50, label: 'predict', size: 50, input: 'trainedModel', output: 'predictionResult', environment: '', process: '', apiType: 0, methodType: 0, inputType: 2, outputType: 1 }},
        {{ id: '9005', x: 450, y: 150, label: 'evaluate', size: 50, input: 'trainedModel', output: 'evaluationMetrics', environment: '', process: '', apiType: 0, methodType: 1, inputType: 2, outputType: 1 }},
        {{ id: '9006', x: 450, y: 250, label: 'visualize', size: 50, input: 'trainedModel', output: 'visualization', environment: '', process: '', apiType: 0, methodType: 1, inputType: 2, outputType: 3 }},
        {{ id: '9007', x: 300, y: 250, label: 'models', size: 50, input: 'trainedModel', output: 'modelMetadata', environment: '', process: '', apiType: 0, methodType: 1, inputType: 2, outputType: 1 }}
    ],
    edges: [
        {{ sourceID: '9001', targetID: '9002' }},
        {{ sourceID: '9002', targetID: '9003' }},
        {{ sourceID: '9003', targetID: '9004' }},
        {{ sourceID: '9003', targetID: '9005' }},
        {{ sourceID: '9003', targetID: '9006' }},
        {{ sourceID: '9003', targetID: '9007' }},
        {{ sourceID: '9007', targetID: '9004' }},
        {{ sourceID: '9007', targetID: '9005' }},
        {{ sourceID: '9007', targetID: '9006' }}
    ]
}}
```"""

SERVICE_PACKAGING_PROMPT = f"""`{WORKSPACE}`路径下有一个`{INPUT_DIR}`文件夹，内有`{MAIN_CODE}`和`requirements.txt`两个文件。`{MAIN_CODE}`当中包含待封装为REST API的功能函数，`requirements.txt`则是对应的依赖环境。

请你在`{WORKSPACE}`下创建一个名为`{OUTPUT_DIR}`的文件夹，将两个原始输入文件复制到该文件夹下，并在其中创建`app.py`用Flask框架将`{MAIN_CODE}`中的`{SELECTED_FUNCTIONS}`封装为api，且使用flask-restx自动生成swagger文档。不要忘记配置跨域访问。在完成创建app.py之后，在`{OUTPUT_DIR}`下的`requirements.txt`中添加flask相关的依赖（如flask==3.1.0，flask-restx==1.3.0，flask_cors），然后再在`{OUTPUT_DIR}`内创建`Dockerfile`和`docker-compose.yml`两个文件，以方便我将封装好的服务基于docker进行部署。

你刚才已经分析过`{MAIN_CODE}`当中的代码逻辑并解析了其中功能函数的相关信息和依赖关系。如果你忘记了，可以阅读`{TEMP_DIR}`下的`{FUNCTION_INFO_PATH}`文件。

在生成Dockerfile时，需要对其中的python依赖安装进行换源。你可以使用清华源即`https://pypi.tuna.tsinghua.edu.cn/simple`。此外，我希望python的版本是3.10。"""

REMOTE_DEPLOY_PROMPT = f"""将`{OUTPUT_DIR}`文件夹传输至远程服务器./路径下（保持同名），并使用Docker-compose进行部署。在传输之前，你需要修改`{OUTPUT_DIR}`文件夹下的`docker-compose.yml`文件中映射的宿主机端口。该端口应该在25100-25200之间，你需要先查看远程服务器已有的容器占用了哪些端口，然后选择一个范围内的空闲端口使用。"""

if __name__ == "__main__":
    print(get_code_analysis_prompt())
