"""文件处理服务：上传保存、ZIP 解压、URL 下载、临时文件清理。"""

from __future__ import annotations

import base64
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import HTTPException, UploadFile
from loguru import logger


async def save_upload(upload: UploadFile, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / f"{ts}_{upload.filename or 'upload'}"
    content = await upload.read()
    dest.write_bytes(content)
    logger.info(f"文件已保存: {dest} ({len(content)} bytes)")
    return dest


def extract_zip(zip_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)
    logger.info(f"ZIP 已解压: {zip_path} -> {dest_dir}")
    return dest_dir


async def download_from_url(url: str, dest_dir: Path) -> Path:
    """从 URL 下载文件到 dest_dir，返回保存路径。"""
    from urllib.parse import urlparse

    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path) or f"download_{ts}.zip"
    dest = dest_dir / f"{ts}_{filename}"

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise HTTPException(400, f"无法从 URL 下载文件，状态码: {resp.status_code}")
        dest.write_bytes(resp.content)

    logger.info(f"文件已下载: {url} -> {dest}")
    return dest


def resolve_project_dir(saved: Path, workspace: Path) -> str:
    """处理上传的 ZIP/PY 文件，返回项目目录路径。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = saved.suffix.lower()

    if ext == ".zip":
        extract_dir = workspace / f"{ts}_extracted"
        extract_zip(saved, extract_dir)
        items = [
            i for i in os.listdir(extract_dir)
            if not i.startswith(".") and i not in {"__MACOSX", "Thumbs.db", "desktop.ini"}
        ]
        if len(items) == 1 and (extract_dir / items[0]).is_dir():
            return str(extract_dir / items[0])
        return str(extract_dir)

    if ext == ".py":
        project_dir = workspace / f"{ts}_extracted"
        project_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(saved), str(project_dir / saved.name))
        return str(project_dir)

    raise HTTPException(400, f"不支持的文件类型: {ext}，仅支持 .zip 和 .py")


def find_main_file(directory: str) -> str:
    """在目录中查找主 Python 文件。"""
    priority = ("main.py", "app.py", "server.py", "run.py", "start.py", "__main__.py")
    for name in priority:
        if os.path.isfile(os.path.join(directory, name)):
            return name
    for f in os.listdir(directory):
        if f.endswith(".py") and os.path.isfile(os.path.join(directory, f)):
            return f
    return ""


async def resolve_file_or_url(
    file: Optional[UploadFile],
    file_url: Optional[str],
    dest_dir: Path,
) -> Path:
    """统一处理 '文件上传 OR URL 下载' 二选一逻辑，返回本地文件路径。"""
    has_file = file is not None and getattr(file, "filename", None)
    has_url = file_url is not None and file_url.strip() != ""

    if not has_file and not has_url:
        raise HTTPException(400, "必须提供文件上传或文件 URL")

    if has_file:
        return await save_upload(file, dest_dir)
    return await download_from_url(file_url.strip(), dest_dir)


def pack_directory_as_zip_base64(directory: str) -> dict:
    """将目录打包为 ZIP 并返回 base64 编码结果。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = Path(directory).parent / f"{ts}_service_package.zip"
    folder_name = os.path.basename(directory)

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(directory):
                for f in files:
                    fp = os.path.join(root, f)
                    arcname = os.path.relpath(fp, os.path.dirname(directory))
                    zf.write(fp, arcname)

        with open(zip_path, "rb") as f:
            content = base64.b64encode(f.read()).decode("utf-8")

        return {
            "filename": zip_path.name,
            "content": content,
            "type": "zip",
        }
    finally:
        if zip_path.exists():
            zip_path.unlink()


def read_paper_content(file_path: str) -> str:
    """从 PDF / DOC / DOCX 文件中提取文本内容。"""
    _, ext = os.path.splitext(file_path.lower())
    content = ""
    try:
        if ext == ".pdf":
            from PyPDF2 import PdfReader
            reader = PdfReader(file_path)
            for page in reader.pages:
                content += (page.extract_text() or "")
        elif ext in (".doc", ".docx"):
            from docx import Document
            doc = Document(file_path)
            for para in doc.paragraphs:
                content += para.text + "\n"
    except Exception as e:
        logger.warning(f"提取文件内容失败 ({file_path}): {e}")
    return content


def cleanup_paths(*paths: str | Path) -> None:
    """安全清理临时文件和目录。"""
    for p in paths:
        try:
            p = Path(p)
            if not p.exists():
                continue
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            logger.debug(f"已清理: {p}")
        except Exception as e:
            logger.warning(f"清理 {p} 失败: {e}")
