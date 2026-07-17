"""Agent 路由：每个任务独立端点，处理各自的输入格式。

端点列表（与旧版一一对应）：
  POST /api/agent/code_analysis              文件上传 → 代码分析
  POST /api/agent/service_packaging          文件上传 → Agent 服务封装（含 ZIP 回传）
  POST /api/agent/mcp_test                   表单 → MCP 测试
  POST /api/agent/service_evaluation         表单+文件 → 服务评测
  POST /api/agent/service_upgrade_advice     表单 → 成果升级建议
  POST /api/agent/scenario_intake               表单 → 想定场景追问（grill-me）
  POST /api/agent/aml_scenario_intake           表单 → 算法想定对话填表
  POST /api/agent/mcp_service_recommendation 表单 → MCP 服务推荐
  POST /api/agent/meta_app_validation        表单+文件 → 元应用数据验证
  POST /api/agent/aml_report                 文件/URL → AML 报告生成
  POST /api/agent/aml_model_evaluation       表单+文件/URL → AML 模型评测（支持数据适配）
  POST /api/agent/aml_auto_generate            表单+文件 → 算法模型想定式开发
  POST /api/agent/meta_app/run               表单+数据文件 → 元应用执行
  POST /api/agent/capability_describe        表单 → 能力描述翻译（直接 LLM）
  POST /api/agent/capability_chat            表单 → 引导式问答（直接 LLM）
  POST /api/agent/custom                     JSON → 自定义 prompt 任务
  GET  /api/agent/tasks                      列出所有预定义任务
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from functools import partial
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from api.deps import build_agent, task_manager
from api.services.files import (
    cleanup_paths,
    fetch_url_text,
    find_main_file,
    parse_dataset_file,
    read_paper_content,
    read_reference_text,
    resolve_file_or_url,
    resolve_project_dir,
    save_upload,
)
from api.services.sse import sse_response, event_to_legacy, _sse_line
from micro_agent.core.config import config
from micro_agent.core.llm import LLM
from micro_agent.core.mcp_agent import MCPAgent
from micro_agent.core.schema import AgentEvent
from micro_agent.packaging.analyzer import RepositoryAnalyzer
from micro_agent.packaging.runtime_verifier import ContainerRuntimeVerifier
from micro_agent.packaging.workflow import (
    AgenticAnalysisWorkflow,
    AgenticPackagingWorkflow,
    analysis_cache,
)
from micro_agent.meta_app import PublishedMetaAppError, load_published_artifact
from micro_agent.simulation.artifact_runtime import run_artifact
from micro_agent.task.base import get_task, list_tasks, render_prompt
from micro_agent.data_file import DataFileError, FileRegistry
from micro_agent.tool.mcp.connection import ServerConfig

import tasks.builtin  # noqa: F401

router = APIRouter(prefix="/api/agent", tags=["agent"])

WORKSPACE = str(config.workspace)


# ============================================================
#  端点：代码分析
# ============================================================

@router.post("/code_analysis")
async def code_analysis(file: UploadFile = File(...)):
    job_root = Path(WORKSPACE) / "temp" / f"mcp-analysis-{uuid.uuid4().hex}"
    saved = await save_upload(file, job_root / "upload")
    project_dir = resolve_project_dir(saved, job_root / "input")
    ir = RepositoryAnalyzer().analyze(project_dir)
    graph_path = job_root / "function.json"
    workflow = AgenticAnalysisWorkflow(project_dir=project_dir, ir=ir, graph_path=graph_path)
    ctx = await task_manager.submit(workflow, file.filename or "uploaded repository")

    return await sse_response(
        ctx,
        output_files=[{"name": "function", "file": str(graph_path)}],
        cleanup=partial(cleanup_paths, job_root),
        components_meta={
            "engine": "agentic",
            "phase": "semantic_planning",
            "repository_fingerprint": ir.fingerprint,
            "files_scanned": len(ir.files),
            "symbols_scanned": len(ir.symbols),
        },
    )


# ============================================================
#  端点：Agent 语义规划、实现与验收
# ============================================================


@router.post("/service_packaging")
async def service_packaging(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(default=None),
):
    job_root = Path(WORKSPACE) / "temp" / f"mcp-package-{uuid.uuid4().hex}"
    saved = await save_upload(file, job_root / "upload")
    project_dir = resolve_project_dir(saved, job_root / "input")
    ir = RepositoryAnalyzer().analyze(project_dir)
    output_dir = job_root / "artifact"
    cached_plan = analysis_cache.get(ir.fingerprint)
    response_session_id = session_id or uuid.uuid4().hex
    runtime_mode = os.getenv("IOEB_PACKAGING_RUNTIME_VERIFY", "static").strip().lower()
    if runtime_mode not in {"static", "docker"}:
        raise HTTPException(
            status_code=500,
            detail="IOEB_PACKAGING_RUNTIME_VERIFY 必须是 static 或 docker",
        )
    require_functional = (
        os.getenv("IOEB_PACKAGING_REQUIRE_FUNCTIONAL_VERIFY", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    workflow = AgenticPackagingWorkflow(
        project_dir=project_dir,
        ir=ir,
        artifact_dir=output_dir,
        plan=cached_plan,
        runtime_verifier_factory=(
            partial(
                ContainerRuntimeVerifier,
                require_full_smoke_coverage=require_functional,
            )
            if runtime_mode == "docker"
            else None
        ),
    )
    ctx = await task_manager.submit(workflow, file.filename or "uploaded repository")

    return await sse_response(
        ctx,
        zip_dir=str(output_dir),
        ready_marker=str(output_dir / ".ioeb-ready"),
        cleanup=partial(cleanup_paths, job_root),
        session_id=response_session_id,
        components_meta={
            "engine": "agentic",
            "phase": "implementation_and_verification",
            "llm_profile": "reasoning",
            "llm_model": config.get_llm("reasoning").model,
            "repository_fingerprint": ir.fingerprint,
            "analysis_cache_hit": cached_plan is not None,
            "session_id": response_session_id,
            "max_repair_attempts": workflow.max_repairs,
            "host_bash_enabled": False,
            "runtime_verification": runtime_mode,
            "functional_verification_required": require_functional,
        },
    )


# ============================================================
#  端点：MCP 测试
# ============================================================

@router.post("/mcp_test")
async def mcp_test(message: str = Form(...), server_url: str = Form(...)):
    prompt = render_prompt("mcp_test.md.j2", message=message, workspace=WORKSPACE)
    agent, _ = await build_agent(name="mcp_test", system_prompt=get_task("mcp_test").system_prompt, use_mcp=True)
    assert isinstance(agent, MCPAgent)
    await agent.connect(ServerConfig(connection_type="sse", server_url=server_url))
    ctx = await task_manager.submit(agent, prompt)

    return await sse_response(
        ctx,
        output_files=[{"name": "mcp_server_list", "file": f"{WORKSPACE}/temp/mcp_server_list.md"}],
    )


# ============================================================
#  端点：服务评测
# ============================================================

@router.post("/service_evaluation")
async def service_evaluation(
    service_name: str = Form(...),
    metrics: str = Form(...),
    file_url: str = Form(default=None),
    data_file: UploadFile = File(None),
):
    metrics_list = json.loads(metrics) if metrics.startswith("[") else [m.strip() for m in metrics.split(",")]

    zip_filename = ""
    if (data_file and data_file.filename) or (file_url and file_url.strip()):
        saved = await resolve_file_or_url(data_file, file_url, Path(WORKSPACE) / "temp")
        zip_filename = str(saved)

    prompt = render_prompt(
        "service_evaluation.md.j2",
        service_name=service_name, metrics_list=metrics_list,
        zip_filename=zip_filename, base_url="https://fdueblab.cn",
        service_info="{}", workspace=WORKSPACE,
    )
    agent, _ = await build_agent(name="service_evaluation", system_prompt=get_task("service_evaluation").system_prompt)
    ctx = await task_manager.submit(agent, prompt)

    return await sse_response(
        ctx,
        output_files=[{"name": "evaluation_result", "file": f"{WORKSPACE}/temp/evaluation_result.json"}],
        cleanup=partial(cleanup_paths, zip_filename) if zip_filename else None,
    )


# ============================================================
#  端点：成果升级建议
# ============================================================

@router.post("/service_upgrade_advice")
async def service_upgrade_advice(
    service_name: str = Form(...),
    service_type: str = Form(default=""),
    domain: str = Form(default=""),
    industry: str = Form(default=""),
    scenario: str = Form(default=""),
    technology: str = Form(default=""),
    status: str = Form(default=""),
    number: str = Form(default="0"),
    norm_summary: str = Form(default=""),
    source_summary: str = Form(default=""),
    code_snippet: str = Form(default=""),
):
    prompt = render_prompt(
        "service_upgrade_advice.md.j2",
        service_name=service_name,
        service_type=service_type,
        domain=domain,
        industry=industry,
        scenario=scenario,
        technology=technology,
        status=status,
        number=number,
        norm_summary=norm_summary or "暂无评测数据",
        source_summary=source_summary or "暂无描述",
        code_snippet=code_snippet or "",
        workspace=WORKSPACE,
    )
    agent, _ = await build_agent(
        name="service_upgrade_advice",
        system_prompt=get_task("service_upgrade_advice").system_prompt,
    )
    ctx = await task_manager.submit(agent, prompt)

    return await sse_response(
        ctx,
        output_files=[{
            "name": "upgrade_advice_result",
            "file": f"{WORKSPACE}/temp/upgrade_advice_result.json",
        }],
    )


# ============================================================
#  端点：想定场景追问（grill-me，一次一问）
# ============================================================

@router.post("/scenario_intake")
async def scenario_intake(
    message: str = Form(...),
    domain: str = Form(default="generic"),
    session_id: Optional[str] = Form(default=None),
):
    from micro_agent.scenario import run_scenario_intake_turn

    try:
        result = await run_scenario_intake_turn(
            message=message,
            domain=domain,
            session_id=session_id or None,
        )
        return {"success": True, **result}
    except Exception as e:
        logger.error(f"scenario_intake 失败: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ============================================================
#  端点：算法想定对话填表（自然语言 → formDraft）
# ============================================================

@router.post("/aml_scenario_intake")
async def aml_scenario_intake(
    message: str = Form(...),
    domain: str = Form(default="generic"),
    session_id: Optional[str] = Form(default=None),
    partial_form: str = Form(default=""),
    dictionary_snapshot: str = Form(default=""),
    followup_count: str = Form(default="0"),
):
    from micro_agent.scenario import run_aml_scenario_intake_turn

    try:
        try:
            followup_n = int(followup_count or "0")
        except (TypeError, ValueError):
            followup_n = 0
        result = await run_aml_scenario_intake_turn(
            message=message,
            domain=domain,
            session_id=session_id or None,
            partial_form=partial_form or None,
            dictionary_snapshot=dictionary_snapshot or None,
            followup_count=followup_n,
        )
        return {"success": True, **result}
    except Exception as e:
        logger.error(f"aml_scenario_intake 失败: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ============================================================
#  端点：MCP 服务推荐
# ============================================================

@router.post("/mcp_service_recommendation")
async def mcp_service_recommendation(
    message: str = Form(...),
    service_type: str = Form(...),
    scenario_summary: str = Form(default=""),
    scenario_parsed: str = Form(default=""),
    user_remark: str = Form(default=""),
    session_id: Optional[str] = Form(default=None),
):
    prompt = render_prompt(
        "mcp_service_recommendation.md.j2",
        message=message,
        service_type=service_type,
        workspace=WORKSPACE,
        scenario_summary=scenario_summary,
        scenario_parsed=scenario_parsed,
        user_remark=user_remark,
    )
    agent, resolved_session = await build_agent(
        name="mcp_service_recommendation",
        system_prompt=get_task("mcp_service_recommendation").system_prompt,
        use_mcp=True,
        enable_session=bool(session_id),
        session_id=session_id or None,
    )
    assert isinstance(agent, MCPAgent)
    # MCP stdio 默认仅传一份安全子集环境，需显式把 DB_*/MYSQL_* 叠加上去，
    # 否则 mysql_server 子进程读不到 ioeb-dev 连接配置。
    from mcp.client.stdio import get_default_environment

    stdio_env = {
        **get_default_environment(),
        **{k: v for k, v in os.environ.items()
           if k.startswith(("DB_", "MYSQL_"))},
    }
    try:
        await asyncio.wait_for(
            agent.connect(ServerConfig(
                connection_type="stdio",
                command=sys.executable,
                args=["-m", "app.mcp.mysql_server.server"],
                env=stdio_env,
                server_id="mysql_server",
            )),
            timeout=25.0,
        )
    except asyncio.TimeoutError:
        logger.error("mcp_service_recommendation: MCP 连接超时")
        raise HTTPException(
            status_code=503,
            detail="MCP 服务连接超时（mysql_server），请检查运行环境或稍后重试",
        ) from None
    except Exception as e:
        logger.error(f"mcp_service_recommendation: MCP 连接失败: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"MCP 服务连接失败: {e}",
        ) from e
    ctx = await task_manager.submit(agent, prompt)

    output_file = f"{WORKSPACE}/temp/mcp_recommendation_result.json"
    return await sse_response(
        ctx,
        output_files=[{"name": "recommendation_result", "file": output_file}],
        cleanup=partial(cleanup_paths, output_file),
        session_id=resolved_session,
    )


# ============================================================
#  端点：元应用数据验证
# ============================================================

@router.post("/meta_app_validation")
async def meta_app_validation(
    meta_app_api: str = Form(...),
    metrics: str = Form(...),
    file_url: str = Form(default=None),
    data_file: UploadFile = File(None),
):
    metrics_list = json.loads(metrics) if metrics.startswith("[") else [m.strip() for m in metrics.split(",")]
    valid_metrics = {"查全率", "查准率", "计算效率"}
    for m in metrics_list:
        if m not in valid_metrics:
            raise HTTPException(400, f"无效的评测指标: {m}。有效指标为: {', '.join(valid_metrics)}")

    saved = await resolve_file_or_url(data_file, file_url, Path(WORKSPACE))
    zip_filename = str(saved)

    prompt = render_prompt(
        "meta_app_validation.md.j2",
        meta_app_api=meta_app_api, metrics_list=metrics_list,
        zip_filename=zip_filename, workspace=WORKSPACE,
    )
    agent, _ = await build_agent(name="meta_app_validation", system_prompt=get_task("meta_app_validation").system_prompt)
    ctx = await task_manager.submit(agent, prompt)

    output_file = f"{WORKSPACE}/temp/validation_result.json"
    return await sse_response(
        ctx,
        output_files=[{"name": "validation_result", "file": output_file}],
        cleanup=partial(cleanup_paths, zip_filename, output_file),
    )


# ============================================================
#  端点：AML 报告生成
# ============================================================

@router.post("/aml_report")
async def aml_report(
    file_url: str = Form(default=None),
    file: UploadFile = File(None),
):
    saved = await resolve_file_or_url(file, file_url, Path(WORKSPACE))

    prompt = render_prompt("aml_report.md.j2", workspace=WORKSPACE, input_dir=str(saved))
    agent, _ = await build_agent(
        name="aml_report",
        system_prompt=get_task("aml_report").system_prompt,
        use_mcp=True,
    )
    assert isinstance(agent, MCPAgent)
    await agent.connect(ServerConfig(
        connection_type="stdio", command="python",
        args=["-m", "app.mcp.aml_server.server"], server_id="aml_server",
    ))
    await agent.connect(ServerConfig(
        connection_type="stdio", command="python",
        args=["-m", "app.mcp.deepseek_server.server"], server_id="deepseek_server",
    ))
    ctx = await task_manager.submit(agent, prompt)

    output_file = f"{WORKSPACE}/temp/aml_report.md"
    return await sse_response(
        ctx,
        output_files=[{"name": "report", "file": output_file}],
        cleanup=partial(cleanup_paths, str(saved), output_file),
    )


# ============================================================
#  端点：AML 模型技术评测（支持数据适配）
# ============================================================

@router.post("/aml_model_evaluation")
async def aml_model_evaluation(
    model_name: str = Form(...),
    metrics: str = Form(...),
    file_url: str = Form(default=None),
    data_file: UploadFile = File(None),
    dataset_type: str = Form(default="1"),
    enable_adaptation: str = Form(default="true"),
):
    metrics_list = json.loads(metrics) if metrics.startswith("[") else [m.strip() for m in metrics.split(",")]
    valid_metrics = {"privacy", "safety-fingerprint", "safety-watermark", "fairness", "robustness", "explainability"}
    for m in metrics_list:
        if m not in valid_metrics:
            raise HTTPException(400, f"无效的评测指标: {m}。有效指标为: {', '.join(valid_metrics)}")

    saved = await resolve_file_or_url(data_file, file_url, Path(WORKSPACE))
    zip_filename = str(saved)

    use_adaptation = enable_adaptation.lower() == "true"

    if use_adaptation:
        from tasks.aml_model_evaluation import build_aml_evaluation_prompt_with_adaptation
        prompt = build_aml_evaluation_prompt_with_adaptation(
            model_name=model_name,
            data_info={
                "dataset_type": dataset_type,
                "data_path": zip_filename,
                "data_url": file_url or "",
            },
            metrics_list=metrics_list,
            workspace=WORKSPACE,
        )
        logger.info("使用数据适配模式的 AML 模型技术评测")
    else:
        prompt = render_prompt(
            "aml_model_evaluation.md.j2",
            model_name=model_name, zip_filename=zip_filename,
            metrics_str=", ".join(metrics_list), workspace=WORKSPACE,
        )
        logger.info("使用标准模式的 AML 模型技术评测")

    mcp_url = os.getenv("PROJECT_4_MCP", "")
    agent, _ = await build_agent(
        name="aml_model_evaluation",
        system_prompt=get_task("aml_model_evaluation").system_prompt,
        use_mcp=True,
    )
    assert isinstance(agent, MCPAgent)
    if mcp_url:
        await agent.connect(ServerConfig(
            connection_type="sse", server_url=mcp_url, server_id="project_4_mcp",
        ))
    if use_adaptation:
        import sys
        adaptation_server = Path(config.workspace) / "mcp_servers" / "data_adaptation_server" / "server.py"
        if adaptation_server.exists():
            await agent.connect(ServerConfig(
                connection_type="stdio",
                command=sys.executable,
                args=[str(adaptation_server)],
                server_id="data_adaptation_mcp",
            ))
            logger.info("已连接数据适配 MCP 服务器")
        else:
            logger.warning(f"数据适配 MCP 服务器不存在: {adaptation_server}，跳过（需部署后启用）")

    ctx = await task_manager.submit(agent, prompt)

    output_file = f"{WORKSPACE}/temp/model_evaluation_result.json"
    return await sse_response(
        ctx,
        output_files=[{"name": "evaluation_result", "file": output_file}],
        cleanup=partial(cleanup_paths, zip_filename, output_file),
    )


# ============================================================
#  端点：算法模型想定式开发（支持会话记忆 + Skills + RAG）
# ============================================================

_aml_retriever = None

AML_AUTO_GENERATE_SYSTEM_PROMPT = (
    "你是一个专业的AI算法工程师 Agent，能够根据用户需求自动生成高质量的算法模型代码。"
    "请使用可用的工具完成代码生成与质量分析任务，完成后使用 terminate 工具返回最终结果。"
    "当用户提供了「相关资料」（论文、专利、开源代码、网址等）时，你应当："
    "1) 参考这些资料中的思路与方法，但严禁逐行照搬其代码或受专利保护的具体实现；"
    "2) 主动进行差异化创新（如改进结构、替换关键步骤、优化策略、结合多种方法），"
    "确保产出与参考资料有实质性区别，规避知识产权与专利侵权争议；"
    "3) 在最终结果中清晰说明参考了什么、新增了什么、提升了什么，以及相比现有算法的特点与优势。"
)


async def _get_aml_retriever():
    """延迟初始化算法模型知识库检索器（模块级单例）。"""
    global _aml_retriever
    if _aml_retriever is not None:
        return _aml_retriever

    knowledge_dir = Path(config.workspace) / "knowledge" / "aml_auto_generate"
    if not knowledge_dir.exists():
        logger.warning(f"aml_auto_generate 知识库目录不存在: {knowledge_dir}")
        return None

    from micro_agent.core.rag.embedding import EmbeddingRetriever
    _aml_retriever = EmbeddingRetriever(
        model=config.rag.embedding_model,
        chunk_size=250,
        api_key=config.llm.api_key,
        base_url=config.llm.base_url,
    )
    await _aml_retriever.load_directory(knowledge_dir)
    return _aml_retriever


@router.post("/aml_auto_generate")
async def aml_auto_generate(
    model_name: str = Form(...),
    free_narrative: str = Form(...),
    industry: str = Form(""),
    scenario: str = Form(""),
    technology: str = Form(""),
    file: UploadFile = File(None),
    dataset_file: UploadFile = File(None),
    algorithm_category: str = Form(""),
    category_params: str = Form(""),
    reference_files: list[UploadFile] = File(default=[]),
    reference_urls: str = Form(""),
    reference_notes: str = Form(""),
    session_id: Optional[str] = Form(default=None),
):
    from tasks.aml_auto_generate import build_aml_auto_generate_prompt

    cleanup_files: list[str] = []
    paper_content = ""
    dataset_info: dict = {}
    reference_materials = ""

    try:
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in (".pdf", ".doc", ".docx"):
                raise HTTPException(400, f"不支持的文件类型: {ext}。仅支持 .pdf / .doc / .docx")
            saved = await save_upload(file, Path(WORKSPACE))
            cleanup_files.append(str(saved))
            paper_content = read_paper_content(str(saved))
            logger.info(f"描述文件已保存并提取文本 ({len(paper_content)} 字符)")

        if dataset_file and dataset_file.filename:
            ds_ext = os.path.splitext(dataset_file.filename)[1].lower()
            if ds_ext not in (".csv", ".xlsx", ".xls", ".json", ".txt", ".pdf"):
                raise HTTPException(400, f"不支持的数据集格式: {ds_ext}")
            ds_saved = await save_upload(dataset_file, Path(WORKSPACE) / "temp")
            cleanup_files.append(str(ds_saved))
            dataset_info = parse_dataset_file(str(ds_saved))
            logger.info(f"数据集文件已解析: {dataset_info.get('format', '?')}, "
                        f"rows={dataset_info.get('total_rows', '?')}")

        parsed_category_params = _parse_json_form(category_params) or {}

        # 处理用户提供的「相关资料」：文件 + URL + 备注，汇总为参考文本
        reference_sections: list[str] = []
        ref_keywords: list[str] = []
        if reference_files:
            for rf in reference_files:
                if not rf or not getattr(rf, "filename", None):
                    continue
                try:
                    ref_saved = await save_upload(rf, Path(WORKSPACE) / "temp" / "references")
                    cleanup_files.append(str(ref_saved))
                    ref_text = read_reference_text(str(ref_saved))
                    if ref_text.strip():
                        reference_sections.append(
                            f"【资料文件：{rf.filename}】\n{ref_text}"
                        )
                        ref_keywords.append(os.path.splitext(rf.filename)[0])
                    logger.info(f"参考资料文件已提取: {rf.filename} ({len(ref_text)} 字符)")
                except Exception as e:
                    logger.warning(f"处理参考资料文件失败 ({getattr(rf, 'filename', '?')}): {e}")

        url_list = _parse_json_form(reference_urls) or []
        if isinstance(url_list, str):
            url_list = [url_list]
        for url in url_list:
            if not url:
                continue
            url_text = await fetch_url_text(str(url))
            if url_text.strip():
                reference_sections.append(f"【参考网址：{url}】\n{url_text}")
            else:
                reference_sections.append(f"【参考网址：{url}】（未能抓取内容，请仅作链接参考）")
            ref_keywords.append(str(url))

        if reference_notes.strip():
            reference_sections.append(f"【用户参考说明】\n{reference_notes.strip()}")

        if reference_sections:
            reference_materials = "\n\n".join(reference_sections)
            logger.info(f"已汇总参考资料 {len(reference_sections)} 项，共 {len(reference_materials)} 字符")

        retriever = await _get_aml_retriever()

        # Skill 匹配：通过 SkillRegistry 元数据自动选择（含 always_for）
        skill_names = _resolve_skills_for_category(
            algorithm_category, parsed_category_params, free_narrative,
            model_name=model_name,
        )
        logger.info(f"自动匹配 Skill: {skill_names}")

        # RAG 预检索：用聚焦查询代替完整 prompt，提高检索精度
        rag_context = ""
        rag_docs = []
        if retriever:
            rag_parts = [model_name, algorithm_category, free_narrative[:200]]
            cat_labels = parsed_category_params.get("labels") or []
            if cat_labels:
                rag_parts.extend(str(l) for l in cat_labels)
            if ref_keywords:
                rag_parts.extend(ref_keywords[:5])
            rag_query = " ".join(p for p in rag_parts if p)
            rag_docs = await retriever.retrieve(rag_query, top_k=5)
            if rag_docs:
                rag_context = "\n---\n".join(
                    f"[{d.source}] {d.content}" for d in rag_docs
                )
                logger.info(
                    f"RAG 预检索命中 {len(rag_docs)} 篇文档: "
                    f"{[d.source for d in rag_docs]}"
                )

        llm_profile = "reasoning"

        agent, sid = await build_agent(
            name="aml_auto_generate",
            system_prompt=AML_AUTO_GENERATE_SYSTEM_PROMPT,
            max_steps=40,
            llm_profile=llm_profile,
            enable_session=True,
            session_id=session_id,
            skills=skill_names,
            retriever=retriever,
        )

        components_meta = {
            "skills": skill_names,
            "llm_profile": llm_profile,
            "llm_model": config.get_llm(llm_profile).model,
            "rag_ready": retriever is not None and len(retriever._docs) > 0,
            "rag_docs_count": len(retriever._docs) if retriever else 0,
            "rag_prequery_hits": len(rag_docs) if retriever and rag_context else 0,
            "memory_loaded": len(agent.memory) if sid and session_id else 0,
            "session_id": sid,
            "session_resumed": bool(session_id),
        }

        prompt = build_aml_auto_generate_prompt(
            model_name=model_name,
            free_narrative=free_narrative,
            workspace=WORKSPACE,
            industry=industry,
            scenario=scenario,
            technology=technology,
            paper_content=paper_content,
            dataset_info=dataset_info,
            algorithm_category=algorithm_category,
            category_params=parsed_category_params,
            rag_context=rag_context,
            reference_materials=reference_materials,
        )

        ctx = await task_manager.submit(agent, prompt)

        output_files = [
            {"name": "aml_generate_result", "file": f"{WORKSPACE}/temp/aml_generate_result.json"},
        ]

        return await sse_response(
            ctx,
            output_files=output_files,
            cleanup=partial(cleanup_paths, *cleanup_files) if cleanup_files else None,
            session_id=sid,
            components_meta=components_meta,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"算法模型自动生成出错: {e}", exc_info=True)
        for fp in cleanup_files:
            if os.path.exists(fp):
                os.remove(fp)
        raise HTTPException(500, f"处理请求时出错: {e}")


def _resolve_skills_for_category(
    category: str,
    params: dict,
    narrative: str,
    model_name: str = "",
) -> list[str]:
    """通过 SkillRegistry 元数据匹配，自动选择适用的 Skill。

    无需硬编码任何 Skill 名称 —— 新增 Skill 只需在其目录下放 skill.toml
    声明 match 条件即可自动被发现。
    """
    from micro_agent.core.skill import SkillRegistry

    input_types = params.get("inputTypes") or []
    labels = params.get("labels") or []
    text_ctx = " ".join(
        filter(None, [model_name, narrative, *(str(l) for l in labels)])
    )
    logger.info(
        f"[Skill匹配] category={category!r}, input_types={input_types}, "
        f"labels={labels}, text_ctx_preview={text_ctx[:150]!r}"
    )
    logger.info(f"[Skill匹配] 已注册 Skill 列表: {SkillRegistry.list_skills()}")
    result = SkillRegistry.find_matching(
        task_name="aml_auto_generate",
        category=category,
        input_types=input_types,
        labels=labels,
        text_context=text_ctx,
    )
    logger.info(f"[Skill匹配] 匹配结果: {result}")
    return result


def _parse_json_form(value: str) -> list | dict | None:
    """安全解析 Form 中的 JSON 字符串，失败时返回 None。"""
    if not value or not value.strip():
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


# ============================================================
#  端点：元应用执行
# ============================================================

@router.post("/meta_app/run")
async def meta_app_run(
    message: str = Form(default=""),
    meta_app_id: str = Form(...),
    input_files: list[UploadFile] = File(default=[]),
):
    try:
        artifact = await load_published_artifact(meta_app_id)
    except PublishedMetaAppError as exc:
        raise HTTPException(422, str(exc)) from exc

    registry = FileRegistry(Path(WORKSPACE) / "runtime_files", f"run_{uuid.uuid4().hex}")
    try:
        input_file_ids = [
            (await registry.register(upload)).file_id
            for upload in input_files if upload and upload.filename
        ]
    except DataFileError as exc:
        registry.cleanup()
        raise HTTPException(400, exc.payload()["error"]) from exc

    request = message.strip() or ("请读取所附数据文件并完成元应用任务。" if input_file_ids else "请根据任务契约完成元应用任务。")

    async def generate():
        yield _sse_line({"status": "start", "inputFileIds": input_file_ids})
        try:
            result = await run_artifact(
                artifact,
                request,
                prefer_golden_path=False,
                file_registry=registry if input_file_ids else None,
                input_file_ids=input_file_ids,
            )
        except Exception as exc:
            yield _sse_line({"error": str(exc)})
            return
        finally:
            registry.cleanup()

        for row in result.get("events") or []:
            if not isinstance(row, dict):
                continue
            event = AgentEvent(
                type=row.get("type") or "log",
                step=int(row.get("step") or 0),
                data=row.get("data") or {},
            )
            yield _sse_line(event_to_legacy(event))

        text_result = result.get("result")
        if text_result is not None and not isinstance(text_result, str):
            text_result = json.dumps(text_result, ensure_ascii=False)
        if not result.get("success"):
            yield _sse_line({"error": result.get("error") or "元应用执行失败"})

        yield _sse_line({
            "is_final_result": True,
            "final_results": {
                "text_result": text_result,
                "visualization_data": None,
                "file_result": None,
            },
        })

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ============================================================
#  端点：能力描述翻译（直接 LLM 调用）
# ============================================================

@router.post("/capability_describe")
async def capability_describe(
    capabilities: str = Form(...),
    context: str = Form(default=""),
):
    llm = LLM(config.llm)
    prompt = f"""你是一个技术到业务的翻译专家。以下是从代码中自动识别出的服务能力列表。
请将每个能力的技术描述转换为普通业务人员能够理解的中文描述。

服务信息：{context}

原始能力列表：
{capabilities}

请严格以JSON数组格式返回，每个元素包含：
- "name": 原始能力名称
- "friendlyName": 简洁的中文能力名称（2-6个字）
- "friendlyDesc": 一句话中文说明
- "friendlyInput": 通俗中文描述需要提供什么
- "friendlyOutput": 通俗中文描述会得到什么结果

只返回JSON数组，不要包含markdown代码块标记。"""

    try:
        resp = await llm.complete([{"role": "user", "content": prompt}], temperature=0.3)
        text = resp.content.strip()
        if text.startswith("```"):
            lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
            text = "\n".join(lines)
        return {"success": True, "data": json.loads(text)}
    except json.JSONDecodeError:
        return {"success": True, "data": resp.content}
    except Exception as e:
        raise HTTPException(500, str(e))


# ============================================================
#  端点：引导式问答（直接 LLM 调用）
# ============================================================

@router.post("/capability_chat")
async def capability_chat(
    capabilities: str = Form(...),
    history: str = Form(default="[]"),
    context: str = Form(default=""),
):
    llm = LLM(config.llm)
    try:
        chat_history = json.loads(history)
    except json.JSONDecodeError:
        chat_history = []

    user_msgs = [m for m in chat_history if m.get("role") == "user"]
    current_round = len(user_msgs) + 1
    system_prompt = f"""你是一个友好的服务配置助手。

当前服务信息：{context}
已识别的服务能力：{capabilities}

你只有3轮对话机会，请遵循：
1. 每次只问一个问题，优先选择题或是/否问题
2. 不使用技术术语
3. 第3轮直接给出优化建议总结

当前是第{current_round}轮。"""
    if current_round >= 3:
        system_prompt += "\n请不要再提问，直接给出最终优化建议总结。"

    messages = [{"role": "system", "content": system_prompt}]
    for msg in chat_history:
        messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
    if not chat_history:
        messages.append({"role": "user", "content": "你好，请帮我看看这些服务能力是否合理。"})

    async def generate():
        try:
            resp = await llm.complete(messages, temperature=0.7)
            yield _sse_line({"type": "text", "content": resp.content})
            yield _sse_line({"type": "done"})
        except Exception as e:
            yield _sse_line({"type": "error", "message": str(e)})

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ============================================================
#  通用端点
# ============================================================

class CustomTaskRequest(BaseModel):
    prompt: str
    system_prompt: str = ""
    max_steps: int = 30
    use_mcp: bool = False
    mcp_servers: Optional[list[dict]] = None


@router.get("/tasks")
async def list_available_tasks():
    return [
        {"name": name, "config": get_task(name).__dict__}
        for name in list_tasks()
        if get_task(name)
    ]


@router.post("/custom")
async def run_custom_task(req: CustomTaskRequest):
    agent, _ = await build_agent(
        name="custom", system_prompt=req.system_prompt,
        max_steps=req.max_steps, use_mcp=req.use_mcp,
    )
    if req.use_mcp and req.mcp_servers:
        assert isinstance(agent, MCPAgent)
        for srv in req.mcp_servers:
            await agent.connect(ServerConfig(**srv))

    ctx = await task_manager.submit(agent, req.prompt)
    return await sse_response(ctx)
