import os
import sys
import asyncio
import tempfile
import shutil
from pathlib import Path
from unittest import mock
import json

import pytest
import pytest_asyncio

from app.tool.file_saver import FileSaver, ToolResult
from app.config import PROJECT_ROOT


@pytest_asyncio.fixture
async def file_saver():
    """创建一个FileSaver工具实例"""
    return FileSaver()


@pytest.fixture
def temp_dir():
    """创建一个临时目录用于测试文件操作"""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    # 测试结束后清理
    shutil.rmtree(temp_dir)


@pytest.mark.asyncio
async def test_save_file_relative_path(file_saver, temp_dir):
    """测试使用相对路径保存文件"""
    # 设置一个临时的PROJECT_ROOT
    with mock.patch('app.tool.file_saver.PROJECT_ROOT', temp_dir):
        # 使用相对路径
        rel_path = "test_file.txt"
        content = "测试内容"
        
        result = await file_saver.execute(content=content, file_path=rel_path)
        
        # 验证结果
        assert isinstance(result, ToolResult)
        assert "successfully saved" in result.output
        
        # 验证文件是否被正确创建
        full_path = os.path.join(temp_dir, rel_path)
        assert os.path.exists(full_path)
        
        # 验证文件内容
        with open(full_path, 'r', encoding='utf-8') as f:
            saved_content = f.read()
            assert saved_content == content


@pytest.mark.asyncio
async def test_save_file_absolute_path(file_saver, temp_dir):
    """测试使用绝对路径保存文件"""
    # 使用绝对路径
    abs_path = os.path.join(temp_dir, "abs_test_file.txt")
    content = "绝对路径测试内容"
    
    result = await file_saver.execute(content=content, file_path=abs_path)
    
    # 验证结果
    assert isinstance(result, ToolResult)
    assert "successfully saved" in result.output
    
    # 验证文件是否被正确创建
    assert os.path.exists(abs_path)
    
    # 验证文件内容
    with open(abs_path, 'r', encoding='utf-8') as f:
        saved_content = f.read()
        assert saved_content == content


@pytest.mark.asyncio
async def test_append_mode(file_saver, temp_dir):
    """测试追加模式"""
    test_file = os.path.join(temp_dir, "append_test.txt")
    initial_content = "初始内容\n"
    append_content = "追加内容"
    
    # 先创建文件并写入初始内容
    result1 = await file_saver.execute(content=initial_content, file_path=test_file)
    assert "successfully saved" in result1.output
    
    # 追加内容
    result2 = await file_saver.execute(content=append_content, file_path=test_file, mode="a")
    assert "successfully saved" in result2.output
    
    # 验证文件内容
    with open(test_file, 'r', encoding='utf-8') as f:
        content = f.read()
        assert content == initial_content + append_content


@pytest.mark.asyncio
async def test_create_directory(file_saver, temp_dir):
    """测试自动创建目录"""
    nested_path = os.path.join(temp_dir, "nested", "dirs", "test_file.txt")
    content = "嵌套目录测试"
    
    result = await file_saver.execute(content=content, file_path=nested_path)
    
    # 验证结果
    assert "successfully saved" in result.output
    
    # 验证目录和文件是否被创建
    assert os.path.exists(nested_path)
    
    # 验证文件内容
    with open(nested_path, 'r', encoding='utf-8') as f:
        saved_content = f.read()
        assert saved_content == content


@pytest.mark.asyncio
async def test_windows_path_handling(file_saver, temp_dir):
    """测试Windows风格路径处理"""
    # 只在非Windows系统上模拟Windows路径
    if os.name != 'nt':
        with mock.patch('app.tool.file_saver.PROJECT_ROOT', temp_dir):
            # 使用Windows风格的路径分隔符
            win_path = "windows\\style\\path.txt"
            content = "Windows路径测试"
            
            result = await file_saver.execute(content=content, file_path=win_path)
            
            # 验证结果
            assert "successfully saved" in result.output
            
            # 检查文件是否在正确的位置创建（转换为系统适用的路径）
            expected_path = os.path.join(temp_dir, "windows", "style", "path.txt")
            assert os.path.exists(expected_path)
            
            # 验证文件内容
            with open(expected_path, 'r', encoding='utf-8') as f:
                saved_content = f.read()
                assert saved_content == content


@pytest.mark.asyncio
async def test_unix_path_handling(file_saver, temp_dir):
    """测试Unix风格路径处理"""
    # 只在Windows系统上模拟Unix路径
    if os.name == 'nt':
        with mock.patch('app.tool.file_saver.PROJECT_ROOT', temp_dir):
            # 使用Unix风格的路径分隔符
            unix_path = "unix/style/path.txt"
            content = "Unix路径测试"
            
            result = await file_saver.execute(content=content, file_path=unix_path)
            
            # 验证结果
            assert "successfully saved" in result.output
            
            # 检查文件是否在正确的位置创建（转换为系统适用的路径）
            expected_path = os.path.join(temp_dir, "unix", "style", "path.txt")
            assert os.path.exists(expected_path)
            
            # 验证文件内容
            with open(expected_path, 'r', encoding='utf-8') as f:
                saved_content = f.read()
                assert saved_content == content


@pytest.mark.asyncio
async def test_error_handling(file_saver):
    """测试错误处理"""
    # 尝试写入到无法访问的路径
    invalid_path = "/root/forbidden/file.txt" if os.name != 'nt' else "C:\\Windows\\System32\\config\\forbidden.txt"
    content = "错误测试"
    
    result = await file_saver.execute(content=content, file_path=invalid_path)
    
    # 验证错误处理
    assert isinstance(result, ToolResult)
    assert "Error saving file" in result.error


@pytest.mark.asyncio
async def test_invalid_mode(file_saver, temp_dir):
    """测试无效的模式参数"""
    test_file = os.path.join(temp_dir, "invalid_mode_test.txt")
    content = "模式测试"
    
    # 尝试使用无效的模式
    result = await file_saver.execute(content=content, file_path=test_file, mode="x")
    
    # 验证模式被纠正为默认的"w"
    assert "successfully saved" in result.output
    
    # 验证文件被创建
    assert os.path.exists(test_file)


@pytest.mark.asyncio
async def test_overwrite_existing_file(file_saver, temp_dir):
    """测试覆盖现有文件"""
    test_file = os.path.join(temp_dir, "overwrite_test.txt")
    initial_content = "初始内容"
    new_content = "新内容"
    
    # 先创建文件
    await file_saver.execute(content=initial_content, file_path=test_file)
    
    # 覆盖文件
    result = await file_saver.execute(content=new_content, file_path=test_file)
    assert "successfully saved" in result.output
    
    # 验证文件内容已更新
    with open(test_file, 'r', encoding='utf-8') as f:
        content = f.read()
        assert content == new_content

@pytest.mark.asyncio
async def test_file_saver_json(file_saver, temp_dir):
    """测试保存json文件"""
    test_file = os.path.join(temp_dir, "test_json.json")
    content_dict = {"key": "value"}
    content_str = json.dumps(content_dict, ensure_ascii=False, indent=2)
    
    result = await file_saver.execute(content=content_str, file_path=test_file)
    assert "successfully saved" in result.output
    
    # 验证JSON文件内容
    with open(test_file, 'r', encoding='utf-8') as f:
        loaded_content = json.load(f)
        assert loaded_content == content_dict



# 在文件的最后，添加直接运行测试的代码
if __name__ == "__main__":
    pytest.main(["-v", __file__])
