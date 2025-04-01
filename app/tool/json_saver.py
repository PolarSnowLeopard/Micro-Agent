import os
import json

import aiofiles

from app.config import PROJECT_ROOT
from app.tool.base import BaseTool, ToolResult


class JsonSaver(BaseTool):
    name: str = "json_saver"
    description: str = """将内容保存为JSON文件。
当你需要将文本、代码或生成的内容保存为JSON格式时使用此工具。
该工具接受内容（字符串或对象）和文件路径，并将内容保存到指定位置。
"""
    parameters: dict = {
        "type": "object",
        "properties": {
            "content": {
                "type": ["string", "object"],
                "description": "(必需) 要保存的内容，可以是JSON字符串或Python对象。",
            },
            "file_path": {
                "type": "string",
                "description": "(必需) 保存文件的路径，包括文件名和扩展名(.json)。",
            },
            "pretty": {
                "type": "boolean",
                "description": "(可选) 是否美化JSON输出。默认为True。",
                "default": True,
            },
        },
        "required": ["content", "file_path"],
    }

    async def execute(self, content: [str, dict], file_path: str, pretty: bool = True) -> str:
        """
        将内容保存为JSON文件。

        参数:
            content (str或dict): 要保存的内容，可以是JSON字符串或Python对象。
            file_path (str): 保存文件的路径。
            pretty (bool, 可选): 是否美化JSON输出。默认为True。

        返回:
            str: 表示操作结果的消息。
        """
        try:
            # 确保文件路径有.json扩展名
            if not file_path.lower().endswith('.json'):
                file_path = f"{file_path}.json"

            # 规范化路径，确保路径分隔符符合当前操作系统
            file_path = os.path.normpath(file_path)
            
            # 处理绝对路径和相对路径
            if os.path.isabs(file_path):
                # 如果是绝对路径，直接使用该路径
                full_path = file_path
            else:
                # 如果是相对路径，将其与PROJECT_ROOT组合
                path_components = [p for p in file_path.replace('\\', '/').split('/') if p]
                full_path = os.path.normpath(os.path.join(PROJECT_ROOT, *path_components))

            # 确保路径规范化
            full_path = os.path.normpath(full_path)

            # 确保目录存在
            directory = os.path.dirname(full_path)
            if directory and not os.path.exists(directory):
                os.makedirs(directory)

            # 处理内容 - 如果是字符串，尝试解析为JSON对象；如果已是对象，直接使用
            if isinstance(content, str):
                try:
                    json_obj = json.loads(content)
                except json.JSONDecodeError:
                    return ToolResult(error="提供的字符串不是有效的JSON格式")
            else:
                json_obj = content

            # 将对象转换为JSON字符串
            if pretty:
                json_str = json.dumps(json_obj, ensure_ascii=False, indent=2)
            else:
                json_str = json.dumps(json_obj, ensure_ascii=False)

            # 写入文件
            async with aiofiles.open(full_path, 'w', encoding="utf-8") as file:
                await file.write(json_str)

            return ToolResult(output=f"JSON内容已成功保存到 {full_path}")
        except Exception as e:
            return ToolResult(error=f"保存JSON文件时出错: {str(e)}")