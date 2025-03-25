#!/usr/bin/env python
import ast
import os
import sys
import re
import importlib.util
from pathlib import Path
from collections import defaultdict, deque
import shutil

class ImportVisitor(ast.NodeVisitor):
    """分析Python文件中的导入语句"""
    
    def __init__(self):
        self.imports = []  # 存储所有导入
        self.from_imports = []  # 存储所有from导入
        self.string_refs = []  # 存储字符串中可能的模块引用
    
    def visit_Import(self, node):
        for name in node.names:
            self.imports.append(name.name)
        self.generic_visit(node)
    
    def visit_ImportFrom(self, node):
        if node.module:  # 排除相对导入情况下module为None的情况
            module_name = node.module
            for name in node.names:
                # 记录完整的导入路径
                self.from_imports.append(f"{module_name}.{name.name}")
        self.generic_visit(node)
    
    def visit_Str(self, node):
        # 检查字符串中是否包含看起来像模块引用的部分
        if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)+$', node.s):
            self.string_refs.append(node.s)
        self.generic_visit(node)
    
    def visit_Constant(self, node):
        # Python 3.8+ 使用 Constant 而不是 Str
        if isinstance(node.value, str):
            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)+$', node.value):
                self.string_refs.append(node.value)
        self.generic_visit(node)

def analyze_file_imports(file_path):
    """分析单个文件的导入"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 解析AST
        tree = ast.parse(content)
        
        # 分析导入
        visitor = ImportVisitor()
        visitor.visit(tree)
        
        # 额外分析：直接在文本中查找可能是动态导入的模式
        # 寻找常见的动态导入模式, 如 __import__('module'), importlib.import_module('module')等
        dynamic_imports = []
        
        # 匹配 __import__('...') 或 importlib.import_module('...')
        import_matches = re.findall(r"(?:__import__|importlib\.import_module)\(['\"]([^'\"]+)['\"]\)", content)
        dynamic_imports.extend(import_matches)
        
        # 匹配 -m module.name 模式
        m_flag_matches = re.findall(r"-m\s+([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)", content)
        dynamic_imports.extend(m_flag_matches)
        
        return visitor.imports, visitor.from_imports, visitor.string_refs, dynamic_imports
    except Exception as e:
        print(f"分析文件 {file_path} 时出错: {e}")
        return [], [], [], []

def get_python_files(directory):
    """获取目录下所有Python文件"""
    python_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                python_files.append(os.path.join(root, file))
    return python_files

def resolve_module_to_file(module_name, base_dir):
    """将模块名称解析为文件路径"""
    parts = module_name.split('.')
    
    # 尝试不同的路径组合
    potential_paths = []
    
    # 直接映射为路径
    file_path = os.path.join(base_dir, *parts) + '.py'
    potential_paths.append(file_path)
    
    # 处理包情况（__init__.py）
    init_path = os.path.join(base_dir, *parts, '__init__.py')
    potential_paths.append(init_path)
    
    # 返回第一个存在的路径
    for path in potential_paths:
        if os.path.exists(path):
            return path
    
    return None

def build_dependency_graph(entry_file, base_dir):
    """构建项目依赖图"""
    dependency_graph = defaultdict(set)  # 文件依赖关系图
    file_to_module = {}  # 文件路径到模块名的映射
    module_to_file = {}  # 模块名到文件路径的映射
    
    # 获取所有Python文件
    all_py_files = get_python_files(base_dir)
    
    # 建立模块名到文件路径的映射
    for file_path in all_py_files:
        rel_path = os.path.relpath(file_path, base_dir)
        
        # 将文件路径转换为模块路径
        module_path = rel_path.replace(os.sep, '.').rstrip('.py')
        if module_path.endswith('.__init__'):
            module_path = module_path[:-9]  # 移除.__init__
        
        file_to_module[file_path] = module_path
        module_to_file[module_path] = file_path
    
    # BFS遍历依赖关系
    queue = deque([entry_file])
    visited = set([entry_file])
    
    while queue:
        current_file = queue.popleft()
        imports, from_imports, string_refs, dynamic_imports = analyze_file_imports(current_file)
        
        # 处理常规导入
        for module_name in imports:
            file_path = resolve_module_to_file(module_name, base_dir)
            if file_path and file_path not in visited and os.path.exists(file_path):
                dependency_graph[current_file].add(file_path)
                queue.append(file_path)
                visited.add(file_path)
        
        # 处理from导入
        for full_import in from_imports:
            # 尝试解析父模块
            parts = full_import.split('.')
            for i in range(len(parts), 0, -1):
                parent_module = '.'.join(parts[:i])
                file_path = resolve_module_to_file(parent_module, base_dir)
                
                if file_path and file_path not in visited and os.path.exists(file_path):
                    dependency_graph[current_file].add(file_path)
                    queue.append(file_path)
                    visited.add(file_path)
                    break
        
        # 处理字符串引用和动态导入
        all_potential_modules = string_refs + dynamic_imports
        
        for module_name in all_potential_modules:
            # 特别处理app.mcp.server这个特殊情况
            if module_name == "app.mcp.server" or module_name.startswith("app.mcp.server."):
                server_path = os.path.join(base_dir, "app", "mcp", "server.py")
                if os.path.exists(server_path) and server_path not in visited:
                    dependency_graph[current_file].add(server_path)
                    queue.append(server_path)
                    visited.add(server_path)
                    continue
            
            file_path = resolve_module_to_file(module_name, base_dir)
            if file_path and file_path not in visited and os.path.exists(file_path):
                dependency_graph[current_file].add(file_path)
                queue.append(file_path)
                visited.add(file_path)
    
    return dependency_graph, visited

def find_unused_files(dependency_graph, visited_files, base_dir):
    """找出未使用的Python文件"""
    all_py_files = set(get_python_files(base_dir))
    unused_files = all_py_files - visited_files
    return unused_files

def delete_unused_files(unused_files, dry_run=True):
    """删除未使用的文件"""
    for file_path in unused_files:
        if dry_run:
            print(f"将删除未使用的文件: {file_path}")
        else:
            try:
                os.remove(file_path)
                print(f"已删除: {file_path}")
                
                # 检查是否需要删除空目录
                dir_path = os.path.dirname(file_path)
                if os.path.exists(dir_path) and not os.listdir(dir_path):
                    os.rmdir(dir_path)
                    print(f"删除空目录: {dir_path}")
            except Exception as e:
                print(f"删除文件 {file_path} 时出错: {e}")

def main(entry_file='run_mcp.py', base_dir='.', scan_dir='app', delete=False):
    print(f"分析从 {entry_file} 开始的依赖关系...")
    
    # 标准化路径
    entry_file = os.path.abspath(entry_file)
    base_dir = os.path.abspath(base_dir)
    scan_dir = os.path.abspath(scan_dir)
    # 构建依赖图
    dependency_graph, visited_files = build_dependency_graph(entry_file, base_dir)
    
    # 打印依赖关系
    print("\n依赖关系图:")
    for file, deps in dependency_graph.items():
        print(f"{os.path.relpath(file, base_dir)}:")
        for dep in deps:
            print(f"  - {os.path.relpath(dep, base_dir)}")
    
    # 找出未使用的文件
    unused_files = find_unused_files(dependency_graph, visited_files, base_dir)
    
    # 排除当前脚本
    script_path = os.path.abspath(__file__)
    if script_path in unused_files:
        unused_files.remove(script_path)

    # 只保留scan_dir目录下的文件
    # scan_dir是个子目录，全字匹配
    scan_dir_files = []
    for file in unused_files:
        # 如果scan_dir是rel_path的子目录，则保留
        if file.startswith(scan_dir + os.sep):
            scan_dir_files.append(file)
        
    unused_files = scan_dir_files
    
    # 打印结果
    print(f"\n访问过的文件数量: {len(visited_files)}")
    print(f"未使用的文件数量: {len(unused_files)}")
    
    if unused_files:
        print("\n未使用的文件:")
        for file in sorted(unused_files):
            rel_path = os.path.relpath(file, base_dir)
            print(f"- {rel_path}")
        
        # 删除未使用的文件
        if delete:
            print("\n删除未使用的文件...")
            delete_unused_files(unused_files, dry_run=False)
        else:
            print("\n运行时添加 --delete 参数来删除以上文件")
    else:
        print("\n没有发现未使用的文件。")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="分析代码依赖并删除未使用的文件")
    parser.add_argument("--entry", default="run_mcp.py", help="入口文件路径")
    parser.add_argument("--dir", default=".", help="基础目录路径")
    parser.add_argument("--scan_dir", default="app", help="扫描目录")
    parser.add_argument("--delete", action="store_true", help="删除未使用的文件")
    
    args = parser.parse_args()
    
    main(entry_file=args.entry, base_dir=args.dir, scan_dir=args.scan_dir, delete=args.delete) 