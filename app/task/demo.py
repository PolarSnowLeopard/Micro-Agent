demo_task_configs = {
    "system_info": {
        "prompt": "我想知道当前机器的一些信息，比如cpu、内存、磁盘、网络等",
        "outputs": [
            # 当前没有特定的最终输出文件，如果将来有可以在这里添加
        ],
        "server_config": [
            # 可以添加多个服务器配置
            {
                "connection_type": "stdio",
                "server_url": None,
                "command": None, 
                "args": None,    
                "server_id": None
            }
        ]
    },
    "list_tools": {
        "prompt": "列出你可以使用的工具，然后直接结束",
        "outputs": [
            # 当前没有特定的最终输出文件，如果将来有可以在这里添加
        ],
        "server_config": [
            {
                "connection_type": "sse",
                "server_url": "http://fdueblab.cn:25013/sse", 
                "command": None,
                "args": None,
                "server_id": None
            },
            {
                "connection_type": "stdio",
                "server_url": None, 
                "command": "python",
                "args": ["-m", "app.mcp.time_server"],
                "server_id": None
            }
        ]
    }
}
