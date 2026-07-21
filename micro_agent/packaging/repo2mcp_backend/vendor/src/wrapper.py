"""
MCPWrapper - 薄编排器

将 MCP 封装任务分为多个阶段，每个阶段内部使用 ReAct Agent：
  Stage 0: 准备（确定性：git clone + AST 分析）
  Stage 1: 代码理解（Agent：分析代码 → 输出工具设计方案）
  Stage 2: 代码生成（Agent：生成 server.py / Dockerfile / requirements.txt）
  Stage 3: 构建与测试（确定性构建 + Agent 修复循环）
  Stage 4: 收集输出
"""
import json
import os
import re
import shutil
import subprocess
import sys
import ast
from pathlib import Path

from config import AgentConfig, default_config
from src.llm.client import LLMClient
from src.sandbox.local import LocalSandbox
from src.tools.base import ToolRegistry
from src.tools.bash import BashTool
from src.tools.code_explorer import CodeExplorerTool
from src.tools.web_search import WebSearchTool, WebFetchTool
from src.agent.mcp_agent import MCPAgent
from src.prompts import (
    ANALYSIS_SYSTEM_PROMPT,
    ANALYSIS_JSON_FALLBACK_PROMPT,
    GENERATION_SYSTEM_PROMPT,
    GENERATION_SINGLE_CALL_PROMPT,
    FIX_SYSTEM_PROMPT,
)
from src.logger import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


class MCPWrapper:
    """MCP 封装编排器"""

    def __init__(
        self,
        llm_config,
        output_dir: str,
        workspace_base: str | None = None,
        agent_config: AgentConfig | None = None,
    ):
        self.llm_config = llm_config
        self.output_dir = output_dir
        self.workspace_base = (
            workspace_base
            if workspace_base is not None
            else default_config.sandbox.workspace_base
        )
        ac = agent_config or AgentConfig()
        self.max_fix_retries = ac.max_fix_retries
        self.analysis_steps = ac.analysis_steps
        self.generation_steps = ac.generation_steps
        self.fix_steps = ac.fix_steps
        self._agent_verbose = ac.verbose

        self.ast_analyzer_path = PROJECT_ROOT / "tools" / "ast_analyzer.py"
        self.sub_cli_path = PROJECT_ROOT / "sub_cli.py"

    def run(
        self,
        repo_url: str,
        wrap_intent: str,
        sample_id: str,
        commit_sha: str = None,
        *,
        stop_after_analysis: bool = False,
        tool_design_override: dict | None = None,
    ) -> dict:
        workspace = os.path.join(self.workspace_base, sample_id)
        source_dir = os.path.join(workspace, "source")
        output_dir = os.path.join(workspace, "output")
        ast_summary_path = os.path.join(workspace, "ast_summary.json")
        tool_design_path = os.path.join(workspace, "tool_design.json")

        os.makedirs(output_dir, exist_ok=True)

        sandbox = LocalSandbox(workdir=workspace, timeout=1200)
        sandbox.start_session()

        try:
            print(f"\n{'='*60}")
            print(f"Stage 0: 准备 - {sample_id}")
            print(f"{'='*60}")

            if not os.path.exists(os.path.join(source_dir, ".git")):
                print(f"  Cloning {repo_url}...")
                if commit_sha:
                    clone_cmd = (
                        f"git init {source_dir} && "
                        f"cd {source_dir} && "
                        f"git remote add origin {repo_url} && "
                        f"git fetch --quiet --depth 1 origin {commit_sha} && "
                        f"git checkout FETCH_HEAD"
                    )
                else:
                    clone_cmd = f"git clone --quiet --depth 1 {repo_url} {source_dir}"
                result = sandbox.exec(clone_cmd, timeout=300)
                if not result.success:
                    print("  浅克隆失败，尝试完整克隆...")
                    if os.path.exists(source_dir):
                        shutil.rmtree(source_dir, ignore_errors=True)
                    result = sandbox.exec(
                        f"git clone --quiet {repo_url} {source_dir}", timeout=600
                    )
                    if not result.success:
                        return self._fail("clone", f"Git clone 失败: {result.stderr}")
                    if commit_sha:
                        sandbox.exec(f"cd {source_dir} && git checkout --quiet {commit_sha}")
            else:
                print("  仓库已存在，跳过 clone")

            cache_file = os.path.join(workspace, "ast_cache.json")
            intent_file = os.path.join(workspace, "wrap_intent.txt")
            seeds_file = os.path.join(workspace, "seeds.txt")

            with open(intent_file, "w", encoding="utf-8") as f:
                f.write(wrap_intent)

            list_cmd = [
                sys.executable,
                str(self.ast_analyzer_path),
                source_dir,
                "--mode", "listing",
                "--cache-file", cache_file,
            ]
            lr = subprocess.run(list_cmd, capture_output=True, text=True, timeout=900)
            if lr.returncode != 0:
                return self._fail("ast_listing", f"AST listing 失败: {lr.stderr or lr.stdout}")
            listing_text = lr.stdout.strip()

            selected = self._llm_select_files(wrap_intent, listing_text) or []
            tier_cmd = [
                sys.executable,
                str(self.ast_analyzer_path),
                source_dir,
                "--mode", "tiered",
                "--cache-file", cache_file,
                "--max-tokens", "40000",
            ]
            if selected:
                with open(seeds_file, "w", encoding="utf-8") as sf:
                    sf.write("\n".join(selected) + "\n")
                tier_cmd.extend(["--seeds-file", seeds_file])
            else:
                tier_cmd.extend(["--intent-file", intent_file])

            tr = subprocess.run(tier_cmd, capture_output=True, text=True, timeout=900)
            if tr.returncode != 0:
                return self._fail("ast_tiered", f"AST tiered 失败: {tr.stderr or tr.stdout}")
            ast_summary_text = tr.stdout.strip()
            with open(ast_summary_path, "w", encoding="utf-8") as f:
                f.write(ast_summary_text)

            print(f"\n{'='*60}")
            print(f"Stage 1: 代码理解 - {sample_id}")
            print(f"{'='*60}")

            if tool_design_override is not None:
                Path(tool_design_path).write_text(
                    json.dumps(tool_design_override, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print("  Reusing validated tool_design.json from analysis phase")

            llm = LLMClient(self.llm_config)
            tools = ToolRegistry()
            tools.register(BashTool(sandbox))
            tools.register(WebSearchTool())
            tools.register(WebFetchTool())

            try:
                with open(cache_file, "r", encoding="utf-8") as cf:
                    ast_cache = json.load(cf)
                tools.register(CodeExplorerTool(ast_cache, source_dir))
            except Exception:
                pass

            analysis_system = (
                ANALYSIS_SYSTEM_PROMPT.format(sub_cli_path=str(self.sub_cli_path))
                .replace("SOURCE_DIR", source_dir)
                .replace("TOOL_DESIGN_PATH", tool_design_path)
            )
            analysis_agent = MCPAgent(
                llm=llm,
                tools=tools,
                system_prompt=analysis_system,
                max_steps=self.analysis_steps,
                verbose=self._agent_verbose,
                completion_check=lambda: os.path.isfile(tool_design_path),
                completion_nudge=(
                    f"tool_design.json 尚未写入磁盘！你必须调用 bash 工具执行以下命令：\n"
                    f"cat > {tool_design_path} << 'HEREDOC_EOF'\n"
                    f"{{... 你的 JSON 内容 ...}}\n"
                    f"HEREDOC_EOF\n"
                    f"请立即执行，不要再用文字描述。"
                ),
                force_completion_after=6,
                compact_initial_task_after=1,
            )
            analysis_task = (
                f"封装意图:\n{wrap_intent}\n\n"
                f"AST 结构摘要:\n```json\n{ast_summary_text}\n```\n\n"
                f"请完成分析，并将工具设计方案写入 {tool_design_path}。\n"
            )
            analysis_result = (
                ""
                if tool_design_override is not None
                else analysis_agent.run(analysis_task)
            )

            if not os.path.isfile(tool_design_path):
                self._try_extract_json_from_response(analysis_result, tool_design_path)

            if not os.path.isfile(tool_design_path):
                print("  ⚠️ tool_design.json 未生成，使用探索证据进行结构化 JSON 编译...")
                compact_ast = self._compact_analysis_evidence(ast_summary_text)
                explored_evidence = analysis_agent.evidence_digest()
                fallback_task = (
                    f"Packaging intent:\n{wrap_intent}\n\n"
                    f"Relevant DARP AST evidence:\n{compact_ast}\n\n"
                    f"Evidence collected by the analysis agent:\n{explored_evidence}\n\n"
                    "Compile the final tool_design.json object now."
                )
                try:
                    fallback_response = llm.simple_chat(
                        fallback_task,
                        system=ANALYSIS_JSON_FALLBACK_PROMPT,
                        max_tokens=8192,
                        response_format={"type": "json_object"},
                    )
                except Exception as structured_error:
                    logger.warning(
                        "Structured tool-design response unsupported; retrying as plain JSON: %s",
                        structured_error,
                    )
                    fallback_response = llm.simple_chat(
                        fallback_task,
                        system=ANALYSIS_JSON_FALLBACK_PROMPT,
                        max_tokens=8192,
                    )
                self._try_extract_json_from_response(
                    fallback_response,
                    tool_design_path,
                )

            if not os.path.isfile(tool_design_path):
                print("  ⚠️ tool_design.json 未生成，启动重试 Agent...")
                retry_task = (
                    f"上一次你在文本中描述了工具设计方案，但没有将其写入文件。\n"
                    f"你**必须**调用 bash 工具，用 cat heredoc 将 tool_design.json 写入磁盘。\n\n"
                    f"封装意图:\n{wrap_intent}\n\n"
                    f"AST 结构摘要:\n```json\n{ast_summary_text}\n```\n\n"
                    f"请分析并生成 tool_design.json（必须包含至少一个工具，"
                    f"且每个工具的 import_path 必须是仓库中实际存在的路径）。\n"
                    f"写入路径: {tool_design_path}\n"
                    f"**重要：必须用 bash 写入文件，不要只在回复中描述！**\n"
                )
                retry_tools = ToolRegistry()
                retry_tools.register(BashTool(sandbox))
                retry_agent = MCPAgent(
                    llm=llm, tools=retry_tools,
                    system_prompt=analysis_system,
                    max_steps=self.analysis_steps,
                    verbose=self._agent_verbose,
                    completion_check=lambda: os.path.isfile(tool_design_path),
                    completion_nudge=(
                        f"不要继续解释；立即用 bash heredoc 将最终 JSON 写入 "
                        f"{tool_design_path}。"
                    ),
                    force_completion_after=4,
                    compact_initial_task_after=1,
                )
                retry_result = retry_agent.run(retry_task)
                if not os.path.isfile(tool_design_path):
                    self._try_extract_json_from_response(retry_result, tool_design_path)
                if not os.path.isfile(tool_design_path):
                    return self._fail("analysis", "tool_design.json 未生成（含重试）")

            td_validation = self._validate_tool_design(tool_design_path)
            if not td_validation["valid"]:
                logger.warning(f"tool_design.json 校验失败: {td_validation['reason']}，尝试重新分析")
                retry_task = (
                    f"上一次生成的 tool_design.json 存在问题: {td_validation['reason']}\n\n"
                    f"封装意图:\n{wrap_intent}\n\n"
                    f"AST 结构摘要:\n```json\n{ast_summary_text}\n```\n\n"
                    f"请重新分析并生成正确的 tool_design.json（必须包含至少一个工具，"
                    f"且每个工具的 import_path 必须是仓库中实际存在的路径）。\n"
                    f"写入路径: {tool_design_path}\n"
                    f"**重要：必须用 bash 写入文件，不要只在回复中描述！**\n"
                )
                retry_agent = MCPAgent(
                    llm=llm, tools=tools,
                    system_prompt=analysis_system,
                    max_steps=self.analysis_steps,
                    verbose=self._agent_verbose,
                    completion_check=lambda: os.path.isfile(tool_design_path),
                    completion_nudge=(
                        f"立即修正并用 bash heredoc 覆盖 {tool_design_path}。"
                    ),
                    force_completion_after=6,
                    compact_initial_task_after=1,
                )
                retry_result = retry_agent.run(retry_task)
                if not os.path.isfile(tool_design_path):
                    self._try_extract_json_from_response(retry_result, tool_design_path)
                td_validation = self._validate_tool_design(tool_design_path)
                if not td_validation["valid"]:
                    return self._fail("analysis", f"tool_design.json 二次校验仍失败: {td_validation['reason']}")

            if stop_after_analysis:
                usage = llm.get_usage()
                return {
                    "success": True,
                    "stage": "analysis",
                    "message": "analysis_complete",
                    "tool_design_path": tool_design_path,
                    "tool_design": json.loads(
                        Path(tool_design_path).read_text(encoding="utf-8")
                    ),
                    "usage": usage,
                }

            print(f"\n{'='*60}")
            print(f"Stage 2: 代码生成 (单次调用) - {sample_id}")
            print(f"{'='*60}")

            try:
                tool_design_content = Path(tool_design_path).read_text(encoding="utf-8")
            except Exception:
                tool_design_content = "(tool_design.json 读取失败)"

            gen_prompt = (
                f"工具设计方案:\n```json\n{tool_design_content}\n```\n\n"
                f"请根据上述设计方案生成三个文件。"
            )
            try:
                gen_response = llm.simple_chat(
                    gen_prompt,
                    system=GENERATION_SINGLE_CALL_PROMPT,
                    max_tokens=16384,
                    response_format={"type": "json_object"},
                )
            except Exception as structured_error:
                logger.warning(
                    "Structured file generation unsupported; retrying as plain JSON: %s",
                    structured_error,
                )
                gen_response = llm.simple_chat(
                    gen_prompt,
                    system=GENERATION_SINGLE_CALL_PROMPT,
                    max_tokens=16384,
                )
            self._parse_and_write_generated_files(gen_response, output_dir)

            required_files = ["server.py", "Dockerfile"]
            missing = [f for f in required_files if not os.path.exists(os.path.join(output_dir, f))]
            if missing:
                print(f"  ⚠️ 单次生成缺少文件 {missing}，回退到 Agent 模式")
                gen_system = (
                    GENERATION_SYSTEM_PROMPT.replace("SOURCE_DIR", source_dir)
                    .replace("OUTPUT_DIR", output_dir)
                )
                gen_task = (
                    f"源代码目录: {source_dir}/\n"
                    f"输出目录: {output_dir}/\n\n"
                    f"工具设计方案:\n```json\n{tool_design_content}\n```\n\n"
                    f"请生成以下文件到输出目录:\n"
                    f"1. {output_dir}/server.py\n"
                    f"2. {output_dir}/Dockerfile\n"
                    f"3. {output_dir}/requirements.txt"
                )
                _server_py = os.path.join(output_dir, "server.py")
                _dockerfile = os.path.join(output_dir, "Dockerfile")
                gen_agent = MCPAgent(
                    llm=llm,
                    tools=tools,
                    system_prompt=gen_system,
                    max_steps=self.generation_steps,
                    verbose=self._agent_verbose,
                    completion_check=lambda: os.path.isfile(_server_py) and os.path.isfile(_dockerfile),
                    completion_nudge=(
                        f"server.py 或 Dockerfile 尚未写入磁盘！请立即调用 bash 工具，"
                        f"用 cat heredoc 将文件写入 {output_dir}/。不要只在文字中描述代码。"
                    ),
                )
                gen_agent.run(gen_task)
                missing = [f for f in required_files if not os.path.exists(os.path.join(output_dir, f))]
                if missing:
                    return self._fail("generation", f"缺少必要文件: {missing}")

            dependency_fixes = self._merge_declared_runtime_dependencies(
                server_py_path=os.path.join(output_dir, "server.py"),
                source_dir=source_dir,
                output_dir=output_dir,
            )
            dependency_fixes.extend(self._normalize_generated_requirements(output_dir))
            for fix_msg in dependency_fixes:
                print(f"  🔧 {fix_msg}")

            server_py_path = os.path.join(output_dir, "server.py")
            tool_count = self._count_mcp_tools(server_py_path)
            if tool_count == 0:
                print("  ⚠️ server.py 中无 @mcp.tool() 注册，用 Agent 重新生成")
                gen_system = (
                    GENERATION_SYSTEM_PROMPT.replace("SOURCE_DIR", source_dir)
                    .replace("OUTPUT_DIR", output_dir)
                )
                regen_task = (
                    f"源代码目录: {source_dir}/\n"
                    f"输出目录: {output_dir}/\n\n"
                    f"工具设计方案:\n```json\n{tool_design_content}\n```\n\n"
                    f"之前生成的 server.py 中没有任何 @mcp.tool() 注册的工具函数，"
                    f"这是不可接受的。请根据工具设计方案重新生成，确保每个工具都有 "
                    f"@mcp.tool() 装饰器，且 import 路径正确（先用 bash 验证）。\n"
                    f"请重新生成以下文件到输出目录:\n"
                    f"1. {output_dir}/server.py\n"
                    f"2. {output_dir}/Dockerfile\n"
                    f"3. {output_dir}/requirements.txt"
                )
                regen_agent = MCPAgent(
                    llm=llm, tools=tools, system_prompt=gen_system,
                    max_steps=self.generation_steps, verbose=self._agent_verbose,
                )
                regen_agent.run(regen_task)
                tool_count = self._count_mcp_tools(server_py_path)
            print(f"  server.py 工具数: {tool_count}")

            # --- Stage 2.5: Import 预验证 ---
            import_issues = self._verify_imports(server_py_path, source_dir, sandbox)
            if import_issues:
                print(f"  ⚠️ Import 预验证发现问题: {import_issues}")
                fix_import_task = (
                    f"server.py 中以下 import 在仓库源码中验证失败:\n"
                    + "\n".join(f"  - {issue}" for issue in import_issues) + "\n\n"
                    f"请修复 {server_py_path} 中的 import 路径。\n"
                    f"原始仓库代码在: {source_dir}/\n"
                    f"用 bash 搜索正确的模块路径: rg -rn 'def function_name' {source_dir}/ --type py\n"
                    f"修复后确保 import 路径在仓库中确实存在。"
                )
                fix_import_system = (
                    FIX_SYSTEM_PROMPT.replace("SOURCE_DIR", source_dir)
                    .replace("OUTPUT_DIR", output_dir)
                )
                fix_import_agent = MCPAgent(
                    llm=llm, tools=tools, system_prompt=fix_import_system,
                    max_steps=self.fix_steps, verbose=self._agent_verbose,
                )
                fix_import_agent.run(fix_import_task)

            # --- Stage 2.7: server.py 质量校验与自动修复 ---
            quality_fixes = self._check_and_fix_server_quality(server_py_path)
            if quality_fixes:
                for fix_msg in quality_fixes:
                    print(f"  🔧 {fix_msg}")

            print(f"\n{'='*60}")
            print(f"Stage 3: 构建与测试 (max_retries={self.max_fix_retries})")
            print(f"{'='*60}")

            # --- Stage 2.9: Dockerfile 静态校验 + 自动修复 ---
            dockerfile_path = os.path.join(output_dir, "Dockerfile")
            self._validate_and_fix_dockerfile(dockerfile_path, output_dir)

            build_success = False
            image_tag = f"repo2mcp-test-{sample_id}".lower()

            for attempt in range(self.max_fix_retries + 1):
                print(f"\n  构建尝试 {attempt + 1}/{self.max_fix_retries + 1}...")

                repo_in_build = os.path.join(output_dir, "repo")
                if os.path.exists(repo_in_build):
                    shutil.rmtree(repo_in_build, ignore_errors=True)
                shutil.copytree(
                    source_dir, repo_in_build,
                    dirs_exist_ok=True,
                    ignore_dangling_symlinks=True,
                )

                build_result = sandbox.exec(
                    f"cd {output_dir} && docker build -t {image_tag} .",
                    timeout=1200,
                )

                if not build_result.success:
                    error_log = build_result.stderr or build_result.stdout
                    print("  ❌ 构建失败")
                    self._cleanup_docker(image_tag)
                    if attempt < self.max_fix_retries:
                        print("  启动构建修复 Agent...")
                        if len(error_log) > 3000:
                            error_log = error_log[-3000:]
                        fix_task = (
                            f"Docker 构建失败（第 {attempt + 1} 次尝试）\n\n"
                            f"错误日志:\n```\n{error_log}\n```\n\n"
                            f"需要修复的文件在: {output_dir}/\n"
                            f"原始仓库代码在: {source_dir}/\n"
                            f"请分析错误并修复相关文件。"
                        )
                    else:
                        continue
                else:
                    print("  ✅ Docker 构建成功")

                    # --- Stage 3.5: 健康检查 ---
                    health_result = sandbox.exec(
                        f"docker run --rm {image_tag} python -c "
                        f"\"import sys; sys.path.insert(0,'/app'); "
                        f"import ast; ast.parse(open('/app/server.py').read()); "
                        f"exec(open('/app/server.py').read().split('if __name__')[0])\" 2>&1",
                        timeout=60,
                    )
                    if health_result.success:
                        print("  ✅ 健康检查通过")
                        build_success = True
                        self._cleanup_docker(image_tag)
                        break

                    health_error = (health_result.stderr or health_result.stdout or "")
                    print(f"  ⚠️ 健康检查失败: {health_error[:200]}")
                    self._cleanup_docker(image_tag)
                    if attempt < self.max_fix_retries:
                        print("  启动健康检查修复 Agent...")
                        if len(health_error) > 3000:
                            health_error = health_error[-3000:]
                        fix_task = (
                            f"Docker 构建成功但健康检查失败（第 {attempt + 1} 次尝试）\n"
                            f"健康检查命令: python -c \"import server\" (在容器内执行)\n\n"
                            f"错误日志:\n```\n{health_error}\n```\n\n"
                            f"这通常是 server.py 中的 import 路径错误或依赖缺失。\n"
                            f"需要修复的文件在: {output_dir}/\n"
                            f"原始仓库代码在: {source_dir}/\n"
                            f"请分析错误并修复相关文件。"
                        )
                    else:
                        continue

                # 共用修复逻辑：构建失败或健康检查失败时执行
                fix_system = (
                    FIX_SYSTEM_PROMPT.replace("SOURCE_DIR", source_dir)
                    .replace("OUTPUT_DIR", output_dir)
                )
                fix_agent = MCPAgent(
                    llm=llm, tools=tools,
                    system_prompt=fix_system,
                    max_steps=self.fix_steps,
                    verbose=self._agent_verbose,
                )
                fix_agent.run(fix_task)
                fix_messages = self._merge_declared_runtime_dependencies(
                    server_py_path=server_py_path,
                    source_dir=source_dir,
                    output_dir=output_dir,
                )
                fix_messages.extend(self._normalize_generated_requirements(output_dir))
                for fix_msg in fix_messages:
                    print(f"  🔧 {fix_msg}")
                self._validate_and_fix_dockerfile(dockerfile_path, output_dir)

            repo_in_build = os.path.join(output_dir, "repo")
            if os.path.exists(repo_in_build):
                shutil.rmtree(repo_in_build)

            print(f"\n{'='*60}")
            print("Stage 4: 收集输出")
            print(f"{'='*60}")

            final_output = os.path.join(self.output_dir, sample_id)
            os.makedirs(final_output, exist_ok=True)

            collected = []
            for fname in [
                "server.py",
                "Dockerfile",
                "requirements.txt",
                "requirements-cpu.txt",
            ]:
                src = os.path.join(output_dir, fname)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(final_output, fname))
                    collected.append(fname)
                    print(f"  ✅ {fname}")
                else:
                    print(f"  ⚠️ {fname} 不存在")

            status = "success" if build_success else "build_failed"
            usage = llm.get_usage()
            print(f"\n{'='*60}")
            print(f"完成: {status} | 文件: {collected}")
            print(f"用量: {usage['calls']} 次调用, {usage['total_tokens']:,} tokens, ${usage['cost']:.4f}")
            print(f"输出目录: {final_output}")
            print(f"{'='*60}")

            return {
                "success": build_success,
                "stage": "complete",
                "message": status,
                "files": collected,
                "output_dir": final_output,
                "usage": usage,
            }

        except Exception as e:
            logger.error(f"Wrapper execution failed: {e}")
            return self._fail("exception", str(e))

        finally:
            sandbox.stop_session()

    def _llm_select_files(self, intent: str, listing: str) -> list[str] | None:
        try:
            llm = LLMClient(self.llm_config)
            prompt = (
                "你是一个代码仓库分析专家。以下是一个 Python 仓库的文件清单（包含文件路径和公开函数/类名）：\n\n"
                f"```\n{listing}\n```\n\n"
                f"用户想要将这个仓库封装为 MCP 服务，封装意图如下：{intent}\n\n"
                "请选出与该封装意图**直接相关**的文件（实现核心功能的模块、必要的数据结构定义、"
                "关键的工具函数等）。不要选测试文件、文档、示例。\n\n"
                "只输出一个 JSON 数组，包含选中的文件路径，不要输出其他内容。例如：\n"
                '["src/core.py", "src/models/base.py"]'
            )
            response = llm.simple_chat(prompt)
            match = re.search(r"\[.*?\]", response, re.DOTALL)
            if match:
                paths = json.loads(match.group())
                if isinstance(paths, list) and all(isinstance(p, str) for p in paths):
                    return paths
        except Exception as e:
            logger.error(f"LLM file selection failed: {e}")
        return None

    @staticmethod
    def _validate_tool_design(path: str) -> dict:
        """校验 tool_design.json 质量"""
        try:
            td = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError) as e:
            return {"valid": False, "reason": f"JSON 解析失败: {e}"}

        tools_list = td.get("tools", [])
        if not tools_list:
            return {"valid": False, "reason": "tools 数组为空，必须至少定义 1 个工具"}

        warnings = []
        for t in tools_list:
            name = t.get("name", "")
            if not name or name in ("example_tool", "tool_name", "tool_name_in_english"):
                return {"valid": False, "reason": f"工具名 '{name}' 是占位符，请使用有意义的名称"}
            impl = t.get("implementation", {})
            if not impl.get("verified_import") and not impl.get("import_path") and not impl.get("source_file"):
                return {"valid": False, "reason": f"工具 '{name}' 缺少 verified_import / import_path / source_file"}
            if not impl.get("verified_import"):
                warnings.append(f"工具 '{name}' 缺少 verified_import，生成阶段可能需要额外验证")
            if not impl.get("function_signature"):
                warnings.append(f"工具 '{name}' 缺少 function_signature")

        result = {"valid": True, "reason": "", "tool_count": len(tools_list)}
        if warnings:
            result["warnings"] = warnings
        return result

    @staticmethod
    def _count_mcp_tools(server_py_path: str) -> int:
        """统计 server.py 中非注释的 @mcp.tool() 注册数"""
        try:
            import ast as _ast
            tree = _ast.parse(Path(server_py_path).read_text(encoding="utf-8"))
            count = 0
            for node in _ast.walk(tree):
                if isinstance(node, _ast.FunctionDef):
                    for dec in node.decorator_list:
                        dec_name = ""
                        if isinstance(dec, _ast.Call) and hasattr(dec.func, "attr"):
                            dec_name = dec.func.attr
                        elif isinstance(dec, _ast.Attribute):
                            dec_name = dec.attr
                        if dec_name == "tool":
                            count += 1
            return count
        except Exception:
            return 0

    @staticmethod
    def _normalize_generated_requirements(output_dir: str) -> list[str]:
        """Normalize common import names before paying for a failed Docker build."""
        path = Path(output_dir) / "requirements.txt"
        if not path.is_file():
            return []
        aliases = {
            "pil": "Pillow",
            "cv2": "opencv-python-headless",
            "sklearn": "scikit-learn",
            "yaml": "PyYAML",
            "skimage": "scikit-image",
            "bio": "biopython",
            "fitz": "PyMuPDF",
            "openslide": "openslide-python",
            "composer": "mosaicml",
            "streaming": "mosaicml-streaming",
        }
        fixes: list[str] = []
        normalized: list[str] = []
        cpu_requirements: list[str] = []
        seen: set[str] = set()
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^([A-Za-z0-9_.-]+)(.*)$", line)
            if not match:
                continue
            name, suffix = match.groups()
            replacement = aliases.get(name.lower())
            if replacement:
                fixed = replacement + suffix
                fixes.append(f"requirements: {line} → {fixed}")
                line = fixed
                name = replacement
            key = re.sub(r"[-_.]+", "-", name).lower()
            if key in seen:
                continue
            seen.add(key)
            if key in {"torch", "torchvision", "torchaudio"}:
                cpu_requirements.append(line)
                fixes.append(f"requirements: {line} → requirements-cpu.txt")
            else:
                normalized.append(line)
        path.write_text(
            "\n".join(normalized) + ("\n" if normalized else ""),
            encoding="utf-8",
        )
        (Path(output_dir) / "requirements-cpu.txt").write_text(
            "\n".join(cpu_requirements) + ("\n" if cpu_requirements else ""),
            encoding="utf-8",
        )
        return fixes

    @staticmethod
    def _merge_declared_runtime_dependencies(
        server_py_path: str,
        source_dir: str,
        output_dir: str,
        *,
        max_local_modules: int = 64,
    ) -> list[str]:
        """Complete generated requirements from the imported local module closure.

        The repository remains the authority for package declarations.  We only
        add a declared distribution when its import is reachable from server.py,
        avoiding the unrelated dev/build dependencies found in many repositories.
        """
        source_root = Path(source_dir)
        declared_path = source_root / "requirements.txt"
        generated_path = Path(output_dir) / "requirements.txt"
        if not declared_path.is_file() or not generated_path.is_file():
            return []

        stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
        external_imports: set[str] = set()
        optional_imports: set[str] = set()
        visited: set[Path] = set()
        queue: list[Path] = [Path(server_py_path)]
        source_roots = [source_root]
        src_layout_root = source_root / "src"
        if src_layout_root.is_dir():
            source_roots.append(src_layout_root)
        shallow_local_modules: dict[str, list[Path]] = {}
        for root in source_roots:
            for candidate in root.glob("*/*.py"):
                shallow_local_modules.setdefault(candidate.stem, []).append(candidate)

        def local_module_file(module: str) -> Path | None:
            if not module:
                return None
            for root in source_roots:
                base = root.joinpath(*module.split("."))
                py_file = base.with_suffix(".py")
                if py_file.is_file():
                    return py_file
                init_file = base / "__init__.py"
                if init_file.is_file():
                    return init_file
            if "." not in module:
                matches = shallow_local_modules.get(module, [])
                if len(matches) == 1:
                    return matches[0]
            return None

        def imported_names(body: list[ast.stmt]) -> set[str]:
            names: set[str] = set()
            for statement in body:
                if isinstance(statement, ast.Import):
                    names.update(
                        alias.asname or alias.name.split(".", 1)[0]
                        for alias in statement.names
                    )
                elif isinstance(statement, ast.ImportFrom):
                    names.update(alias.asname or alias.name for alias in statement.names)
            return names

        def defined_names(body: list[ast.stmt]) -> set[str]:
            names: set[str] = set()
            for statement in body:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(statement.name)
                elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        statement.targets
                        if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    for target in targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
            return names

        def catches_import_error(handler: ast.ExceptHandler) -> bool:
            if isinstance(handler.type, ast.Name):
                return handler.type.id == "ImportError"
            if isinstance(handler.type, ast.Tuple):
                return any(
                    isinstance(item, ast.Name) and item.id == "ImportError"
                    for item in handler.type.elts
                )
            return False

        def has_import_fallback(statement: ast.Try) -> bool:
            imported = imported_names(statement.body)
            if not imported:
                return False
            for handler in statement.handlers:
                if not catches_import_error(handler):
                    continue
                if handler.body and all(isinstance(item, ast.Pass) for item in handler.body):
                    return True
                if imported <= defined_names(handler.body):
                    return True
            return False

        def import_time_statements(body: list[ast.stmt]):
            """Yield statements executed while a module is imported.

            Imports guarded by module-level ``try``/``if`` blocks still run at
            startup and can be required to define the module's public API.  Do
            not descend into function or class bodies because those imports are
            deferred until the selected capability is invoked.
            """
            for statement in body:
                yield statement
                nested_bodies: list[list[ast.stmt]] = []
                if isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While)):
                    nested_bodies.extend((statement.body, statement.orelse))
                elif isinstance(statement, (ast.With, ast.AsyncWith)):
                    nested_bodies.append(statement.body)
                elif isinstance(statement, ast.Try):
                    if has_import_fallback(statement):
                        for optional in statement.body:
                            if isinstance(optional, ast.Import):
                                optional_imports.update(
                                    alias.name.split(".", 1)[0].lower()
                                    for alias in optional.names
                                )
                            elif isinstance(optional, ast.ImportFrom) and optional.module:
                                optional_imports.add(
                                    optional.module.split(".", 1)[0].lower()
                                )
                    else:
                        nested_bodies.append(statement.body)
                    nested_bodies.extend([statement.orelse, statement.finalbody])
                    nested_bodies.extend(handler.body for handler in statement.handlers)
                elif isinstance(statement, ast.Match):
                    nested_bodies.extend(case.body for case in statement.cases)
                for nested in nested_bodies:
                    yield from import_time_statements(nested)

        while queue and len(visited) < max_local_modules:
            current = queue.pop(0)
            resolved = current.resolve()
            if resolved in visited or not current.is_file():
                continue
            visited.add(resolved)
            try:
                tree = ast.parse(current.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue

            try:
                relative = current.resolve().relative_to(source_root.resolve())
                module_parts = list(relative.with_suffix("").parts)
                package_parts = (
                    module_parts[:-1]
                    if module_parts[-1] != "__init__"
                    else module_parts[:-1]
                )
            except ValueError:
                package_parts = []

            # Import-time health depends on direct module imports and imports in
            # module-level control flow. Walking function/class bodies would pull
            # in deferred optional solver/visualization extras unnecessarily.
            for node in import_time_statements(tree.body):
                candidates: list[str] = []
                if isinstance(node, ast.Import):
                    candidates.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        ascend = max(node.level - 1, 0)
                        base = package_parts[: max(len(package_parts) - ascend, 0)]
                        if node.module:
                            candidates.append(".".join(base + node.module.split(".")))
                        else:
                            candidates.extend(
                                ".".join(base + [alias.name]) for alias in node.names
                            )
                    elif node.module:
                        candidates.append(node.module)

                for module in candidates:
                    local = local_module_file(module)
                    if local is not None:
                        if local.resolve() not in visited:
                            queue.append(local)
                        continue
                    top_level = module.split(".", 1)[0]
                    if top_level and top_level not in stdlib and top_level != "mcp":
                        external_imports.add(top_level.lower())

        import_aliases = {
            "pillow": "pil",
            "opencv-python": "cv2",
            "opencv-python-headless": "cv2",
            "scikit-learn": "sklearn",
            "pyyaml": "yaml",
            "scikit-image": "skimage",
            "biopython": "bio",
            "pymupdf": "fitz",
            "openslide-python": "openslide",
            "python-dateutil": "dateutil",
            "python-dotenv": "dotenv",
            "beautifulsoup4": "bs4",
            "setuptools": "pkg_resources",
            "mosaicml": "composer",
            "mosaicml-streaming": "streaming",
        }
        distribution_for_import = {
            import_name: distribution
            for distribution, import_name in import_aliases.items()
        }

        def distribution_name(line: str) -> str | None:
            match = re.match(r"^([A-Za-z0-9_.-]+)", line)
            if not match:
                return None
            return re.sub(r"[-_.]+", "-", match.group(1)).lower()

        generated_lines = generated_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        local_top_levels = {
            child.stem if child.is_file() else child.name
            for root in source_roots
            for child in root.iterdir()
            if (child.is_file() and child.suffix == ".py") or child.is_dir()
        }
        filtered_lines: list[str] = []
        additions: list[str] = []
        for line in generated_lines:
            name = distribution_name(line.strip())
            import_name = (
                import_aliases.get(name, name).replace("-", "_") if name else None
            )
            is_repository_local = bool(
                import_name
                and (
                    import_name in local_top_levels
                    or local_module_file(import_name) is not None
                )
            )
            is_optional_fallback = bool(
                import_name
                and import_name in optional_imports
                and import_name not in external_imports
            )
            if is_repository_local or is_optional_fallback:
                reason = (
                    "repository-local module"
                    if is_repository_local
                    else "optional fallback import"
                )
                additions.append(
                    f"requirements: remove {reason} dependency {line.strip()}"
                )
                continue
            filtered_lines.append(line)
        generated_lines = filtered_lines
        present = {
            name for line in generated_lines if (name := distribution_name(line.strip()))
        }
        for raw_line in declared_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "-r", "--", "-e")):
                continue
            name = distribution_name(line)
            if not name or name in present:
                continue
            import_name = import_aliases.get(name, name).replace("-", "_")
            if local_module_file(import_name) is not None:
                continue
            if import_name.lower() not in external_imports:
                continue
            generated_lines.append(line)
            present.add(name)
            additions.append(f"requirements: add reachable repository dependency {line}")

        satisfied_imports = {
            import_aliases.get(name, name).replace("-", "_").lower()
            for name in present
        }
        for import_name in sorted(external_imports - satisfied_imports):
            distribution = distribution_for_import.get(import_name, import_name)
            canonical = re.sub(r"[-_.]+", "-", distribution).lower()
            if canonical in present:
                continue
            generated_lines.append(distribution)
            present.add(canonical)
            additions.append(
                f"requirements: infer reachable import {import_name} → {distribution}"
            )

        if additions:
            generated_path.write_text(
                "\n".join(generated_lines) + "\n",
                encoding="utf-8",
            )
        return additions

    @staticmethod
    def _get_tool_functions(server_py_path: str) -> list:
        """提取 server.py 中所有 @mcp.tool() 装饰的函数 AST 节点"""
        import ast as _ast
        tree = _ast.parse(Path(server_py_path).read_text(encoding="utf-8"))
        tool_funcs = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                for dec in node.decorator_list:
                    dec_name = ""
                    if isinstance(dec, _ast.Call) and hasattr(dec.func, "attr"):
                        dec_name = dec.func.attr
                    elif isinstance(dec, _ast.Attribute):
                        dec_name = dec.attr
                    if dec_name == "tool":
                        tool_funcs.append(node)
                        break
        return tool_funcs

    @staticmethod
    def _check_and_fix_server_quality(server_py_path: str) -> list:
        """确定性校验 server.py 的 D2 质量并自动修复可修复的问题。

        检查项（零 LLM 开销）：
        1. 每个 @mcp.tool() 函数是否有 docstring
        2. 是否有 try/except 错误处理
        3. 是否使用 json.dumps 返回结构化结果
        不满足时尝试自动包裹。
        """
        import ast as _ast
        fixes = []

        try:
            content = Path(server_py_path).read_text(encoding="utf-8")
            tree = _ast.parse(content)
        except Exception:
            return fixes

        tool_funcs = MCPWrapper._get_tool_functions(server_py_path)
        if not tool_funcs:
            return fixes

        lines = content.splitlines()
        modified = False

        for func in tool_funcs:
            fname = func.name

            # 检查 1: docstring
            docstring = _ast.get_docstring(func)
            if not docstring:
                fixes.append(f"{fname}: 缺少 docstring（需要 LLM 补充，此处跳过）")

            # 检查 2: try/except
            has_try = any(isinstance(stmt, _ast.Try) for stmt in _ast.walk(func))
            if not has_try:
                func_start = func.body[0].lineno - 1
                func_end = func.end_lineno
                indent = ""
                for ch in lines[func_start]:
                    if ch in (' ', '\t'):
                        indent += ch
                    else:
                        break

                body_lines = lines[func_start:func_end]
                new_body = [f"{indent}try:"]
                for bl in body_lines:
                    new_body.append(f"    {bl}" if bl.strip() else bl)
                params = [arg.arg for arg in func.args.args if arg.arg != "self"]
                err_parts = ", ".join(f"{p}={{{p}!r}}" for p in params[:3])
                err_fmt = f'f"{err_parts}: {{e}}"' if params else 'str(e)'
                new_body.append(f'{indent}except Exception as e:')
                new_body.append(f'{indent}    return json.dumps({{"result": None, "status": "error", "error": {err_fmt}}})')

                lines[func_start:func_end] = new_body
                modified = True
                fixes.append(f"{fname}: 自动添加 try/except 错误处理")

        # 检查 3: 确保 import json 存在
        has_json_import = any(
            "import json" in line and not line.strip().startswith("#")
            for line in lines
        )
        if not has_json_import and modified:
            for i, line in enumerate(lines):
                if line.startswith("import ") or line.startswith("from "):
                    lines.insert(i, "import json")
                    fixes.append("自动添加 import json")
                    break

        if modified:
            Path(server_py_path).write_text("\n".join(lines), encoding="utf-8")

        return fixes

    @staticmethod
    def _verify_imports(server_py_path: str, source_dir: str, sandbox) -> list:
        """Statically validate imports that resolve to files in the repository.

        Executing imports on the worker before generated dependencies are
        installed creates false failures and expensive Agent loops. External
        packages are therefore deferred to the authoritative Docker build.
        """
        issues = []
        try:
            content = Path(server_py_path).read_text(encoding="utf-8")
        except Exception:
            return issues

        skip_prefixes = ("mcp", "sys", "os", "json", "typing", "pathlib", "re", "io",
                         "abc", "functools", "collections", "dataclasses", "enum",
                         "datetime", "math", "itertools", "copy", "base64", "hashlib")
        import_lines = re.findall(r'^from\s+([\w.]+)\s+import\s+(.+)', content, re.MULTILINE)
        source_root = Path(source_dir)

        for module, names in import_lines[:8]:
            if any(module.startswith(p) for p in skip_prefixes):
                continue
            parts = module.split(".")
            top_level = source_root / parts[0]
            if not (top_level.is_dir() or top_level.with_suffix(".py").is_file()):
                continue
            module_path = source_root.joinpath(*parts)
            if not (
                module_path.with_suffix(".py").is_file()
                or (module_path / "__init__.py").is_file()
            ):
                issues.append(f"from {module} import {names.strip()} → FAIL")
        return issues

    @staticmethod
    def _validate_and_fix_dockerfile(dockerfile_path: str, output_dir: str):
        """静态校验 Dockerfile 中的 COPY 指令，自动修复常见路径错误"""
        try:
            content = Path(dockerfile_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return

        lines = content.splitlines()
        fixed_lines = []
        fixed = False

        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith("COPY") or stripped.upper().startswith("ADD"):
                parts = stripped.split()
                if len(parts) >= 3:
                    src = parts[1]
                    for bad_prefix in ("./output/", "output/", "./source/", "source/"):
                        if src.startswith(bad_prefix):
                            if "source" in bad_prefix:
                                new_src = "repo/" + src[len(bad_prefix):]
                            else:
                                new_src = src[len(bad_prefix):]
                            new_line = line.replace(src, new_src, 1)
                            print(f"  🔧 Dockerfile 自动修复: {stripped} → {new_line.strip()}")
                            line = new_line
                            fixed = True
                            break
            fixed_lines.append(line)

        pythonpath_line = "ENV PYTHONPATH=/app/repo:/app/repo/src"
        if pythonpath_line not in fixed_lines:
            insert_at = next(
                (
                    index + 1
                    for index, docker_line in enumerate(fixed_lines)
                    if docker_line.strip().upper().startswith("WORKDIR ")
                ),
                1,
            )
            fixed_lines.insert(insert_at, pythonpath_line)
            fixed = True

        if fixed:
            Path(dockerfile_path).write_text("\n".join(fixed_lines), encoding="utf-8")

        content = Path(dockerfile_path).read_text(encoding="utf-8")
        cpu_copy = "COPY requirements.txt requirements-cpu.txt /app/"
        content = content.replace(
            "COPY requirements.txt /app/requirements.txt",
            cpu_copy,
        )
        if "PYTORCH_CPU_INDEX_URL" not in content:
            content = content.replace(
                cpu_copy,
                cpu_copy
                + "\nARG PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu"
                + "\nRUN if [ -s /app/requirements-cpu.txt ]; then "
                + "pip install --no-cache-dir --index-url "
                + "\"${PYTORCH_CPU_INDEX_URL}\" --timeout 120 --retries 5 "
                + "-r /app/requirements-cpu.txt; fi",
            )
        plain_pip_install = "RUN pip install --no-cache-dir -r /app/requirements.txt"
        if plain_pip_install in content:
            content = content.replace(
                plain_pip_install,
                "ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple\n"
                "RUN pip install --no-cache-dir --index-url \"${PIP_INDEX_URL}\" "
                "--timeout 120 --retries 5 -r /app/requirements.txt",
            )
            Path(dockerfile_path).write_text(content, encoding="utf-8")
            print("  🔧 Dockerfile 已在构建前启用可靠 PyPI 镜像、超时与重试")
        else:
            Path(dockerfile_path).write_text(content, encoding="utf-8")

        copy_pattern = re.findall(r'(?:COPY|ADD)\s+(\S+)', content, re.IGNORECASE)
        known_context = {".", "requirements.txt", "requirements-cpu.txt", "repo/", "repo", "server.py",
                         "/app/repo/", "/app/requirements.txt", "/app/server.py",
                         "--from=", "--chown="}
        bad_copies = []
        for src in copy_pattern:
            if src.startswith("-"):
                continue
            if src.startswith("/app"):
                continue
            if src in known_context:
                continue
            build_path = os.path.join(output_dir, src.rstrip("/"))
            if src == "repo/" or src == "repo":
                continue
            if not os.path.exists(build_path) and src not in ("requirements.txt", "server.py"):
                bad_copies.append(src)

        if bad_copies:
            print(f"  ⚠️ Dockerfile 中 COPY 源路径可能不存在于构建上下文: {bad_copies}")
            print("  🔧 使用标准 Dockerfile 模板覆盖")
            base_image = "python:3.11-slim"
            for line in content.splitlines():
                if line.strip().upper().startswith("FROM"):
                    base_image = line.strip().split(None, 1)[1] if len(line.strip().split()) > 1 else base_image
                    break
            has_repo_reqs = os.path.exists(os.path.join(output_dir, "repo", "requirements.txt"))
            repo_reqs_line = (
                "RUN pip install --no-cache-dir -r /app/repo/requirements.txt\n"
                if has_repo_reqs else ""
            )
            standard = (
                f"FROM {base_image}\n"
                f"WORKDIR /app\n"
                f"ENV PYTHONPATH=/app/repo:/app/repo/src\n"
                f"COPY requirements.txt requirements-cpu.txt /app/\n"
                f"ARG PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu\n"
                f"RUN if [ -s /app/requirements-cpu.txt ]; then "
                f"pip install --no-cache-dir --index-url \"${{PYTORCH_CPU_INDEX_URL}}\" "
                f"--timeout 120 --retries 5 -r /app/requirements-cpu.txt; fi\n"
                f"ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple\n"
                f"RUN pip install --no-cache-dir --index-url \"${{PIP_INDEX_URL}}\" "
                f"--timeout 120 --retries 5 -r /app/requirements.txt\n"
                f"{repo_reqs_line}"
                f"COPY repo/ /app/repo/\n"
                f"COPY server.py /app/server.py\n"
                f"EXPOSE 8000\n"
                f'CMD ["python", "server.py"]\n'
            )
            Path(dockerfile_path).write_text(standard, encoding="utf-8")

    @staticmethod
    def _parse_and_write_generated_files(response: str, output_dir: str) -> list:
        """从单次生成的 LLM 响应中解析并写入 server.py / Dockerfile / requirements.txt"""
        written = []
        if not response:
            return written

        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", response):
            try:
                payload, _ = decoder.raw_decode(response[match.start():])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            aliases = {
                "server.py": ("server.py", "server_py"),
                "Dockerfile": ("Dockerfile", "dockerfile"),
                "requirements.txt": ("requirements.txt", "requirements_txt"),
            }
            for actual_name, keys in aliases.items():
                content = next(
                    (
                        payload[key]
                        for key in keys
                        if isinstance(payload.get(key), str) and payload[key].strip()
                    ),
                    None,
                )
                if content is None:
                    continue
                normalized = content.rstrip() + "\n"
                target = os.path.join(output_dir, actual_name)
                Path(target).write_text(normalized, encoding="utf-8")
                written.append(actual_name)
                print(f"  ✅ {actual_name} ({len(normalized)} chars, structured JSON)")
            if written:
                return written

        file_map = {
            "server.py": "server.py",
            "Dockerfile": "Dockerfile",
            "dockerfile": "Dockerfile",
            "requirements.txt": "requirements.txt",
        }

        for pattern_name, actual_name in file_map.items():
            pattern = re.compile(
                r'```' + re.escape(pattern_name) + r'\s*\n(.*?)```',
                re.DOTALL,
            )
            match = pattern.search(response)
            if match:
                content = match.group(1).strip() + "\n"
                target = os.path.join(output_dir, actual_name)
                Path(target).write_text(content, encoding="utf-8")
                written.append(actual_name)
                print(f"  ✅ {actual_name} ({len(content)} chars)")

        if "server.py" not in written:
            code_blocks = re.findall(r'```(?:python)?\s*\n(.*?)```', response, re.DOTALL)
            for block in code_blocks:
                if "FastMCP" in block and "@mcp.tool()" in block:
                    target = os.path.join(output_dir, "server.py")
                    Path(target).write_text(block.strip() + "\n", encoding="utf-8")
                    written.append("server.py")
                    print(f"  ✅ server.py (从 python 代码块提取, {len(block)} chars)")
                    break

        return written

    @staticmethod
    def _try_extract_json_from_response(response: str, target_path: str) -> bool:
        """尝试从 Agent 文本响应中提取 tool_design JSON 并写入文件"""
        if not response:
            return False
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", response):
            try:
                parsed, _ = decoder.raw_decode(response[match.start():])
            except (json.JSONDecodeError, TypeError):
                continue
            if (
                isinstance(parsed, dict)
                and isinstance(parsed.get("tools"), list)
                and parsed["tools"]
            ):
                Path(target_path).write_text(
                    json.dumps(parsed, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                print(f"  📝 从 Agent 响应中提取 tool_design.json（{len(parsed['tools'])} 个工具）")
                return True
        return False

    @staticmethod
    def _compact_analysis_evidence(text: str, max_chars: int = 16_000) -> str:
        """Keep the highest-ranked DARP evidence within a bounded compiler prompt."""
        if len(text) <= max_chars:
            return text
        head = max_chars * 3 // 4
        tail = max_chars - head
        omitted = len(text) - max_chars
        return (
            text[:head]
            + f"\n\n[DARP evidence compacted: {omitted} characters omitted]\n\n"
            + text[-tail:]
        )

    @staticmethod
    def _cleanup_docker(image_tag: str):
        # Only remove the image produced by this run.  Global image/builder
        # pruning discards reusable dependency layers, slows every subsequent
        # benchmark sample, and can interfere with unrelated Docker workloads
        # on a shared host.
        subprocess.run(
            f"docker rmi {image_tag} 2>/dev/null || true",
            shell=True,
            capture_output=True,
            timeout=60,
        )
        print("  🧹 Docker 清理完成")

    @staticmethod
    def _fail(stage: str, message: str) -> dict:
        print(f"\n❌ 失败 at {stage}: {message}")
        return {
            "success": False,
            "stage": stage,
            "message": message,
            "files": [],
        }
