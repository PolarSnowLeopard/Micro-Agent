import os
import sys
import asyncio
from unittest import mock
import tempfile

import pytest
import pytest_asyncio

from app.tool.bash import Bash, _BashSession, CLIResult
from app.exceptions import ToolError


@pytest_asyncio.fixture
async def bash_tool():
    """创建一个Bash工具实例"""
    return Bash()


@pytest.mark.asyncio
async def test_session_timeout_auto_restart():
    """测试bash会话超时后自动重启功能"""
    # 创建一个bash工具实例
    bash = Bash()
    
    # 模拟一个已超时的会话
    mock_session = mock.MagicMock()
    mock_session.timed_out = True
    bash._session = mock_session
    
    # 执行命令，此时应该自动重启会话
    result = await bash.execute(command="echo 'test'")
    
    # 验证旧会话被停止
    mock_session.stop.assert_called_once()
    
    # 验证结果中包含超时重启的提示
    assert isinstance(result, CLIResult)
    assert "检测到上一次会话超时" in result.system
    assert "Previous session timed out" in result.error


@pytest.mark.asyncio
async def test_sentinel_detection_with_long_output():
    """测试长输出时终止标记的正确识别"""
    # 创建一个模拟的bash会话
    session = _BashSession()
    
    # 模拟subprocess的行为
    mock_process = mock.AsyncMock()
    mock_process.returncode = None
    
    # 创建模拟的stdin, stdout和stderr
    mock_stdin = mock.AsyncMock()
    mock_process.stdin = mock_stdin
    
    # 创建一个模拟的stdout buffer，用于存储一个长字符串
    # 该字符串包含终止标符但在不同位置
    class MockBuffer:
        def __init__(self, content):
            self.content = content
            
        def decode(self):
            return self.content
            
        def clear(self):
            self.content = ""
    
    # 创建一个很长的输出字符串，并在末尾包含终止标记
    long_output = "x" * 10000 + "\n" + session._sentinel + "\n"
    mock_process.stdout = mock.MagicMock()
    mock_process.stdout._buffer = MockBuffer(long_output)
    
    # 创建一个空的stderr
    mock_process.stderr = mock.MagicMock()
    mock_process.stderr._buffer = MockBuffer("")
    
    # 替换session的_process属性
    session._process = mock_process
    session._started = True
    
    # 使用patch替换asyncio.timeout，避免实际等待
    with mock.patch('asyncio.timeout'):
        with mock.patch('asyncio.sleep'):
            # 执行命令
            result = await session.run("echo 'test'")
            
            # 验证结果
            assert isinstance(result, CLIResult)
            assert result.output == "x" * 10000
            assert not result.error
            
            # 验证stdin被正确使用
            mock_stdin.write.assert_called_once()
            mock_stdin.drain.assert_called_once()


@pytest.mark.asyncio
async def test_timeout_handling():
    """测试超时处理"""
    # 创建一个bash会话
    session = _BashSession()
    session._started = True
    
    # 模拟subprocess的行为
    mock_process = mock.AsyncMock()
    mock_process.returncode = None
    mock_process.stdin = mock.AsyncMock()
    mock_process.stdout = mock.MagicMock()
    mock_process.stderr = mock.MagicMock()
    
    # 设置一个不包含终止标记的输出，这将导致超时
    class MockBuffer:
        def __init__(self, content):
            self.content = content
            
        def decode(self):
            return self.content
            
        def clear(self):
            self.content = ""
    
    # 创建一个不包含终止标记的输出
    mock_process.stdout._buffer = MockBuffer("无终止标记的输出")
    mock_process.stderr._buffer = MockBuffer("")
    
    # 替换session的_process属性
    session._process = mock_process
    
    # 模拟asyncio.timeout抛出TimeoutError
    with mock.patch('asyncio.timeout') as mock_timeout:
        mock_timeout.side_effect = asyncio.TimeoutError()
        
        # 执行命令，应该抛出ToolError
        with pytest.raises(ToolError) as excinfo:
            await session.run("echo 'test'")
        
        # 验证错误消息
        assert "timed out" in str(excinfo.value)
        
        # 验证timed_out标志被设置
        assert session.timed_out is True


@pytest.mark.asyncio
async def test_bash_restart():
    """测试显式重启功能"""
    # 创建一个bash工具实例
    bash = Bash()
    
    # 设置一个模拟的会话
    mock_session = mock.MagicMock()
    bash._session = mock_session
    
    # 执行restart命令
    result = await bash.execute(restart=True)
    
    # 验证旧会话被停止
    mock_session.stop.assert_called_once()
    
    # 验证结果
    assert isinstance(result, CLIResult)
    assert "tool has been restarted" in result.system


@pytest.mark.asyncio
async def test_no_command_provided():
    """测试未提供命令的情况"""
    # 创建一个bash工具实例
    bash = Bash()
    
    # 确保有一个会话
    if bash._session is None:
        bash._session = mock.MagicMock()
        bash._session.timed_out = False
    
    # 不提供命令，应该抛出ToolError
    with pytest.raises(ToolError) as excinfo:
        await bash.execute(command=None)
    
    # 验证错误消息
    assert "no command provided" in str(excinfo.value)


@pytest.mark.asyncio
async def test_session_not_started():
    """测试会话未启动的情况"""
    # 创建一个bash会话
    session = _BashSession()
    
    # 会话未启动就运行命令，应该抛出ToolError
    with pytest.raises(ToolError) as excinfo:
        await session.run("echo 'test'")
    
    # 验证错误消息
    assert "Session has not started" in str(excinfo.value)


@pytest.mark.asyncio
async def test_session_persistence_and_multiple_commands():
    """测试会话持久化和多次命令执行"""
    # 创建一个bash工具实例
    bash = Bash()
    
    # 模拟session.run方法，使其返回预定义的结果
    async def mock_run(command):
        if command == "cd /tmp":
            return CLIResult(output="", error="")
        elif command == "pwd":
            return CLIResult(output="/tmp", error="")
        elif command == "echo $PREVIOUS_COMMAND":
            return CLIResult(output="pwd", error="")
            
    # 创建模拟会话
    mock_session = mock.MagicMock()
    mock_session.timed_out = False
    mock_session.run = mock_run
    
    # 替换bash的_session
    bash._session = mock_session
    
    # 执行第一个命令：cd /tmp
    result1 = await bash.execute(command="cd /tmp")
    assert isinstance(result1, CLIResult)
    assert result1.output == ""
    
    # 执行第二个命令：pwd，应该保持在/tmp目录
    result2 = await bash.execute(command="pwd")
    assert isinstance(result2, CLIResult)
    assert result2.output == "/tmp"
    
    # 执行第三个命令，检查上一个命令是否被正确记忆
    result3 = await bash.execute(command="echo $PREVIOUS_COMMAND")
    assert isinstance(result3, CLIResult)
    assert result3.output == "pwd"


@pytest.mark.asyncio
async def test_sentinel_in_middle_of_output():
    """测试输出中间包含终止标记的情况"""
    # 创建一个模拟的bash会话
    session = _BashSession()
    
    # 定义一个自定义的终止标记，用于本测试
    test_sentinel = "<<test_exit>>"
    session._sentinel = test_sentinel
    
    # 模拟subprocess的行为
    mock_process = mock.AsyncMock()
    mock_process.returncode = None
    mock_process.stdin = mock.AsyncMock()
    
    # 创建一个模拟的stdout buffer类
    class MockBuffer:
        def __init__(self, content):
            self.content = content
            
        def decode(self):
            return self.content
            
        def clear(self):
            self.content = ""
    
    # 创建一个输出，其中终止标记出现在中间和结尾
    tricky_output = f"开始{test_sentinel}中间内容{test_sentinel}结束"
    mock_process.stdout = mock.MagicMock()
    mock_process.stdout._buffer = MockBuffer(tricky_output)
    
    # 创建一个空的stderr
    mock_process.stderr = mock.MagicMock()
    mock_process.stderr._buffer = MockBuffer("")
    
    # 替换session的_process属性
    session._process = mock_process
    session._started = True
    
    # 使用patch替换asyncio.timeout和sleep，避免实际等待
    with mock.patch('asyncio.timeout'), mock.patch('asyncio.sleep'):
        # 执行命令
        result = await session.run("echo 'test'")
        
        # 验证结果 - 应该只包含终止标记之前的内容
        assert isinstance(result, CLIResult)
        assert result.output == "开始"
        assert not result.error


# 在文件的最后，添加直接运行测试的代码
if __name__ == "__main__":
    pytest.main(["-v", __file__]) 