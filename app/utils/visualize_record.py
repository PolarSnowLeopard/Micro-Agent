import os
from app.logger import logger

from app.config import TEMPLATE_ROOT, VISUALIZATION_ROOT

def save_record_to_json(task_name: str, record: str) -> None:
    """
    将记录保存到JSON文件中

    参数:
        task_name: 任务名称，用于生成JSON文件名
        record: 记录内容，JSON字符串
    """
    # 确保visualization目录存在
    os.makedirs(VISUALIZATION_ROOT, exist_ok=True)

    json_path = VISUALIZATION_ROOT / f'{task_name}_record.json'
    with open(json_path, 'w', encoding='utf-8') as f:
        f.write(record)

def generate_visualization_html(task_name: str) -> None:
    """
    根据任务名生成可视化HTML文件，将record.html作为模板
    
    参数:
        task_name: 任务名称，用于生成HTML文件名和JSON文件名
    """
    # 确保visualization目录存在
    os.makedirs(VISUALIZATION_ROOT, exist_ok=True)
    
    # 定义文件路径
    template_path = TEMPLATE_ROOT / 'record.html'
    json_path = VISUALIZATION_ROOT / f'{task_name}_record.json'
    output_html_path = VISUALIZATION_ROOT / f'{task_name}.html'
    
    # 检查模板文件是否存在
    if not os.path.exists(template_path):
        logger.error(f"模板文件 {template_path} 不存在")
        return
    
    # 读取模板文件内容
    with open(template_path, 'r', encoding='utf-8') as f:
        template_content = f.read()
    
    # 修改模板中的JSON文件路径
    modified_content = template_content.replace(
        "const response = await fetch('record.json');",
        f"const response = await fetch('{task_name}_record.json');"
    )
    
    # 更新页面标题
    modified_content = modified_content.replace(
        "<title>Agent执行过程可视化</title>",
        f"<title>{task_name.replace('_', ' ').capitalize()} Agent执行过程可视化</title>"
    )
    
    modified_content = modified_content.replace(
        "<h1 class=\"display-4\">Agent执行过程可视化</h1>",
        f"<h1 class=\"display-4\">{task_name.replace('_', ' ').capitalize()} Agent执行过程可视化</h1>"
    )
    
    # 写入新的HTML文件
    with open(output_html_path, 'w', encoding='utf-8') as f:
        f.write(modified_content)
    
    logger.info(f"成功生成可视化HTML文件: {output_html_path}")