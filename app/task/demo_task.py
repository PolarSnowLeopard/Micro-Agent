demo_task_configs = {
    "system_info": {
        "prompt": "我想知道当前机器的一些信息，比如cpu、内存、磁盘、网络等",
        "outputs": [
            # 当前没有特定的最终输出文件，如果将来有可以在这里添加
        ],
        "server_config": {
            "connection_type": "stdio",  # stdio连接类型使用默认的内置MCP服务器
            "server_url": None,
            "command": None,  # 默认使用sys.executable
            "args": None,     # 默认使用["-m", "app.mcp.server"]
            "server_id": None # 自动生成ID
        }
    },
    "list_tools": {
        "prompt": "列出你可以使用的工具，然后直接结束",
        "outputs": [
            # 当前没有特定的最终输出文件，如果将来有可以在这里添加
        ],
        "server_config": {
            "connection_type": "sse",  # 使用内置MCP服务器，之前是SSE但可能导致错误
            "server_url": "http://fdueblab.cn:25013/sse",          # SSE连接需要有效的URL，如果没有则使用stdio
            "command": None,  # 默认使用sys.executable
            "args": None,     # 默认使用["-m", "app.mcp.server"]
            "server_id": None # 自动生成ID
        }
    }
}
