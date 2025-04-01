import os
import shutil
import tempfile
import uuid
import zipfile


def create_temp_dir():
    """创建临时目录"""
    temp_dir = os.path.join(tempfile.gettempdir(), str(uuid.uuid4()))
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def extract_zip(zip_file_path, extract_path):
    """解压ZIP文件到指定路径"""
    with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
        zip_ref.extractall(extract_path)
    return extract_path


def find_main_file(directory, keywords=None):
    """
    识别主文件
    策略：
    1. 查找名称包含main, app, index的文件
    2. 如果没有，查找最大的.py文件
    """
    if keywords is None:
        keywords = ["main", "safety_fingerprint_run"]

    py_files = []

    # 遍历目录查找所有.py文件
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".py"):
                full_path = os.path.join(root, file)
                py_files.append((full_path, os.path.getsize(full_path)))

    if not py_files:
        return None

    # 查找包含关键词的文件
    for keyword in keywords:
        for path, _ in py_files:
            if keyword in os.path.basename(path).lower():
                return path

    # 如果没有找到关键词文件，返回最大的文件
    py_files.sort(key=lambda x: x[1], reverse=True)
    return py_files[0][0]


def create_zip(source_dir, output_path=None):
    """将目录打包为ZIP文件"""
    if output_path is None:
        output_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.zip")

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(source_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, source_dir)
                zipf.write(file_path, arcname)

    return output_path


def cleanup(path):
    """
    清理临时文件或目录

    Args:
        path: 要清理的文件或目录路径
    """
    if not os.path.exists(path):
        return

    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except PermissionError:
        # Windows上的特殊处理，如果有权限错误，可能是文件被占用
        # 记录错误并继续，让系统稍后自动清理这些文件
        import logging

        logging.getLogger(__name__).warning(f"无法删除文件/目录'{path}'，可能正在被使用")
    except Exception as e:
        # 其他异常，记录但不抛出
        import logging

        logging.getLogger(__name__).error(f"清理'{path}'时发生错误: {str(e)}")
