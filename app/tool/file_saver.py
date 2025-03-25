import os

import aiofiles

from app.config import PROJECT_ROOT
from app.tool.base import BaseTool, ToolResult


class FileSaver(BaseTool):
    name: str = "file_saver"
    description: str = """Save content to a local file at a specified path.
Use this tool when you need to save text, code, or generated content to a file on the local filesystem.
The tool accepts content and a file path, and saves the content to that location.
"""
    parameters: dict = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "(required) The content to save to the file.",
            },
            "file_path": {
                "type": "string",
                "description": "(required) The path where the file should be saved, including filename and extension.",
            },
            "mode": {
                "type": "string",
                "description": "(optional) The file opening mode. Default is 'w' for write. Use 'a' for append.",
                "enum": ["w", "a"],
                "default": "w",
            },
        },
        "required": ["content", "file_path"],
    }

    async def execute(self, content: str, file_path: str, mode: str = "w") -> str:
        """
        Save content to a file at the specified path.

        Args:
            content (str): The content to save to the file.
            file_path (str): The path where the file should be saved.
            mode (str, optional): The file opening mode. Default is 'w' for write. Use 'a' for append.

        Returns:
            str: A message indicating the result of the operation.
        """
        try:
            mode = mode if mode in ["w", "a"] else "w"

            # 先规范化路径，确保路径分隔符符合当前操作系统
            file_path = os.path.normpath(file_path)
            
            # 处理绝对路径和相对路径
            if os.path.isabs(file_path):
                # 如果是绝对路径，直接使用该路径
                full_path = file_path
            else:
                # 如果是相对路径，将其与WORKSPACE_ROOT组合
                # 确保file_path不包含系统无关的路径分隔符问题
                path_components = [p for p in file_path.replace('\\', '/').split('/') if p]
                full_path = os.path.normpath(os.path.join(PROJECT_ROOT, *path_components))

            # 确保路径规范化
            full_path = os.path.normpath(full_path)

            # Ensure the directory exists
            directory = os.path.dirname(full_path)
            if directory and not os.path.exists(directory):
                os.makedirs(directory)

            # Write directly to the file
            async with aiofiles.open(full_path, mode, encoding="utf-8") as file:
                await file.write(content)

            return ToolResult(output=f"Content successfully saved to {full_path}")
        except Exception as e:
            return ToolResult(error=f"Error saving file: {str(e)}")