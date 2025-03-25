# app/tool/remote_docker_manager.py
from app.tool.base import BaseTool, ToolResult
import json

class RemoteDockerManager(BaseTool):
    name: str = "remote_docker_manager"
    description: str = "管理远程服务器上的Docker容器，包括端口检查、查看容器信息、构建和运行容器"
    parameters: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "要执行的Docker操作: check_ports, list_containers, build, run, stop, remove, docker_compose_logs",
                "enum": ["check_ports", "list_containers", "build", "run", "stop", "remove", "docker_compose_logs"]
            },
            "port_range": {
                "type": "string",
                "description": "检查端口范围，格式如: 8000-9000",
            },
            "project_path": {
                "type": "string",
                "description": "Docker项目所在路径",
            },
            "compose_file": {
                "type": "string",
                "description": "docker-compose.yml文件的路径",
            },
            "container_name": {
                "type": "string",
                "description": "容器名称",
            }
        },
        "required": ["action"],
    }
    
    async def execute(self, action: str, **kwargs) -> ToolResult:
        # 使用远程SSH工具执行命令
        from app.tool.remote_ssh import RemoteSSH
        
        ssh_tool = RemoteSSH()
        
        if action == "list_containers":
            # 使用提供的脚本获取容器及其端口映射信息
            script = """#!/bin/bash
# 获取所有正在运行的 Docker 容器的 ID
container_ids=$(sudo docker ps -q)

# 遍历每个容器
for container_id in $container_ids; do
    # 获取容器的名称
    container_name=$(sudo docker inspect --format '{{.Name}}' $container_id | sed 's/^\///')

    # 获取容器的端口映射信息
    port_mappings=$(sudo docker inspect --format '{{range $p, $conf := .NetworkSettings.Ports}} {{$p}} -> {{(index $conf 0).HostPort}} {{end}}' $container_id)

    # 输出容器名称和端口映射信息
    echo "容器名称: $container_name"
    echo "端口映射: $port_mappings"
    echo "----------------------------------------"
done
"""
            # 创建临时脚本文件，执行，然后删除
            cmd = f"cat > /tmp/list_containers.sh << 'EOF'\n{script}\nEOF\n"
            cmd += "chmod +x /tmp/list_containers.sh && /tmp/list_containers.sh && rm -f /tmp/list_containers.sh"
            
            result = await ssh_tool.execute(command=cmd)
            return ToolResult(output=result.output)
            
        elif action == "check_ports":
            # 检查远程服务器上的可用端口
            port_range = kwargs.get("port_range", "8000-9000")
            
            # 解析并获取所有已使用的端口
            cmd = "sudo docker ps -a --format '{{.Ports}}' | grep -Eo '[0-9]+:[0-9]+' | cut -d: -f2 | grep -v '^0$' | sort -n | uniq"
            result = await ssh_tool.execute(command=cmd)
            
            if result.error:
                return ToolResult(error=result.error)
                
            # 解析已使用的端口
            used_ports = set()
            if result.output and result.output.strip():
                lines = result.output.strip().split('\n')
                for line in lines:
                    if line and line.strip().isdigit():
                        used_ports.add(int(line.strip()))
            
            # 找出可用端口
            try:
                if '-' in port_range:
                    start_port, end_port = map(int, port_range.split('-'))
                    available_ports = [p for p in range(start_port, end_port+1) if p not in used_ports]
                else:
                    return ToolResult(error=f"端口范围格式错误: {port_range}，应为如'8000-9000'的格式")
            except ValueError:
                return ToolResult(error=f"端口范围格式错误: {port_range}，应为如'8000-9000'的格式")
            
            return ToolResult(output=json.dumps({
                "used_ports": list(used_ports),
                "available_ports": available_ports  # 返回所有可用端口
            }))
            
        elif action == "build":
            project_path = kwargs.get("project_path")
            if not project_path:
                return ToolResult(error="缺少project_path参数")
                
            cmd = f"cd {project_path} && sudo docker-compose build"
            result = await ssh_tool.execute(command=cmd)
            return ToolResult(output=result.output)
            
        elif action == "run":
            project_path = kwargs.get("project_path")
            if not project_path:
                return ToolResult(error="缺少project_path参数")
                
            cmd = f"cd {project_path} && sudo docker-compose up -d"
            result = await ssh_tool.execute(command=cmd)
            return ToolResult(output=result.output)
            
        elif action == "stop":
            container_name = kwargs.get("container_name")
            if not container_name:
                return ToolResult(error="缺少container_name参数")
                
            cmd = f"sudo docker stop {container_name}"
            result = await ssh_tool.execute(command=cmd)
            return ToolResult(output=result.output)
            
        elif action == "remove":
            container_name = kwargs.get("container_name")
            if not container_name:
                return ToolResult(error="缺少container_name参数")
                
            cmd = f"sudo docker rm {container_name}"
            result = await ssh_tool.execute(command=cmd)
            return ToolResult(output=result.output)

        elif action == "docker_compose_logs":
            project_path = kwargs.get("project_path")
            if not project_path:
                return ToolResult(error="缺少project_path参数")
                
            cmd = f"cd {project_path} && sudo docker-compose logs"
            result = await ssh_tool.execute(command=cmd)
            return ToolResult(output=result.output)
        
        return ToolResult(error=f"未知操作: {action}")