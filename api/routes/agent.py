"""Agent 路由：每个任务独立端点，处理各自的输入格式。

端点列表（与旧版一一对应）：
  POST /api/agent/code_analysis              文件上传 → 代码分析
  POST /api/agent/service_packaging          文件上传 → 服务封装（含 ZIP 回传 + 会话记忆）
  POST /api/agent/mcp_test                   表单 → MCP 测试
  POST /api/agent/service_evaluation         表单+文件 → 服务评测
  POST /api/agent/scenario_intake               表单 → 想定场景追问（grill-me）
  POST /api/agent/mcp_service_recommendation 表单 → MCP 服务推荐
  POST /api/agent/meta_app_validation        表单+文件 → 元应用数据验证
  POST /api/agent/aml_report                 文件/URL → AML 报告生成
  POST /api/agent/aml_model_evaluation       表单+文件/URL → AML 模型评测（支持数据适配）
  POST /api/agent/aml_auto_generate            表单+文件 → 算法模型想定式开发
  POST /api/agent/meta_app/run               表单 → 元应用执行
  POST /api/agent/capability_describe        表单 → 能力描述翻译（直接 LLM）
  POST /api/agent/capability_chat            表单 → 引导式问答（直接 LLM）
  POST /api/agent/custom                     JSON → 自定义 prompt 任务
  GET  /api/agent/tasks                      列出所有预定义任务
"""

from __future__ import annotations

import asyncio
import json
import os
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
    find_main_file,
    parse_dataset_file,
    read_paper_content,
    resolve_file_or_url,
    resolve_project_dir,
    save_upload,
)
from api.services.sse import sse_response, event_to_legacy, _sse_line
from micro_agent.core.config import config
from micro_agent.core.llm import LLM
from micro_agent.core.mcp_agent import MCPAgent
from micro_agent.core.meta_app_agent import MetaAppAgent
from micro_agent.task.base import get_task, list_tasks, render_prompt
from micro_agent.tool.mcp.connection import ServerConfig

import tasks.builtin  # noqa: F401

router = APIRouter(prefix="/api/agent", tags=["agent"])

WORKSPACE = str(config.workspace)


# ============================================================
#  端点：代码分析
# ============================================================

@router.post("/code_analysis")
async def code_analysis(file: UploadFile = File(...)):
    saved = await save_upload(file, Path(WORKSPACE))
    project_dir = resolve_project_dir(saved, Path(WORKSPACE))
    main_code = find_main_file(project_dir)

    prompt = render_prompt(
        "code_analysis.md.j2",
        workspace=WORKSPACE, input_dir=project_dir, main_code=main_code,
        temp_dir="temp", function_info_path="function.json",
    )
    agent, _ = await build_agent(name="code_analysis", system_prompt=get_task("code_analysis").system_prompt)
    ctx = await task_manager.submit(agent, prompt)

    return await sse_response(
        ctx,
        output_files=[{"name": "function", "file": f"{WORKSPACE}/temp/function.json"}],
        cleanup=partial(cleanup_paths, str(saved), project_dir),
    )


# ============================================================
#  端点：服务封装（支持会话记忆 + Skills + RAG）
# ============================================================

_packaging_retriever = None


async def _get_packaging_retriever():
    """延迟初始化服务封装知识库检索器（模块级单例）。"""
    global _packaging_retriever
    if _packaging_retriever is not None:
        return _packaging_retriever

    knowledge_dir = Path(config.workspace) / "knowledge" / "service_packaging"
    if not knowledge_dir.exists():
        return None

    from micro_agent.core.rag.embedding import EmbeddingRetriever
    _packaging_retriever = EmbeddingRetriever(
        model=config.rag.embedding_model,
        chunk_size=config.rag.chunk_size,
        api_key=config.llm.api_key,
        base_url=config.llm.base_url,
    )
    await _packaging_retriever.load_directory(knowledge_dir)
    return _packaging_retriever


@router.post("/service_packaging")
async def service_packaging(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(default=None),
):
    saved = await save_upload(file, Path(WORKSPACE))
    project_dir = resolve_project_dir(saved, Path(WORKSPACE))
    main_code = find_main_file(project_dir)
    output_dir = f"{WORKSPACE}/app-demo-output"

    retriever = await _get_packaging_retriever()

    skill_names = ["mcp_protocol", "docker_packaging", "code_analysis_patterns"]
    llm_profile = "reasoning"

    agent, sid = await build_agent(
        name="service_packaging",
        system_prompt=get_task("service_packaging").system_prompt,
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
        "memory_loaded": len(agent.memory) if sid and session_id else 0,
        "session_id": sid,
        "session_resumed": bool(session_id),
    }

    prompt = render_prompt(
        "service_packaging.md.j2",
        workspace=WORKSPACE, input_dir=project_dir, main_code=main_code,
        output_dir=output_dir, temp_dir="temp", function_info_path="function.json",
    )
    ctx = await task_manager.submit(agent, prompt)

    return await sse_response(
        ctx,
        zip_dir=output_dir,
        cleanup=partial(cleanup_paths, str(saved), project_dir),
        session_id=sid,
        components_meta=components_meta,
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
#  端点：MCP 服务推荐
# ============================================================

@router.post("/mcp_service_recommendation")
async def mcp_service_recommendation(
    message: str = Form(...),
    service_type: str = Form(...),
    scenario_summary: str = Form(default=""),
    parsed_intent: str = Form(default=""),
    user_remark: str = Form(default=""),
    session_id: Optional[str] = Form(default=None),
):
    prompt = render_prompt(
        "mcp_service_recommendation.md.j2",
        message=message,
        service_type=service_type,
        workspace=WORKSPACE,
        scenario_summary=scenario_summary,
        parsed_intent=parsed_intent,
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
    try:
        await asyncio.wait_for(
            agent.connect(ServerConfig(
                connection_type="stdio",
                command="python",
                args=["-m", "app.mcp.mysql_server.server"],
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
    session_id: Optional[str] = Form(default=None),
):
    from tasks.aml_auto_generate import build_aml_auto_generate_prompt

    cleanup_files: list[str] = []
    paper_content = ""
    dataset_info: dict = {}

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
    message: str = Form(...),
    app_config: str = Form(...),
    use_sim_only: Optional[str] = Form(default=None),
):
    try:
        meta_config = json.loads(app_config)
    except Exception as e:
        raise HTTPException(400, f"app_config 非法 JSON: {e}")

    use_sim = True
    if use_sim_only is not None:
        use_sim = str(use_sim_only).strip().lower() in ("1", "true", "yes", "on")

    llm = LLM(config.llm)
    agent = MetaAppAgent(llm=llm)
    await agent.initialize_from_config(meta_config, use_sim=use_sim)

    ctx = await task_manager.submit(agent, message)
    info = meta_config.get("info") or {}
    allow_viz = info.get("outputVisualization", False)

    async def generate():
        yield _sse_line({"status": "start"})
        finalized_payload = None
        last_action_result = None

        async for event in ctx.subscribe():
            if event.type == "tool_result":
                tool_name = event.data.get("tool", "").lower()
                output = event.data.get("result", "")
                if tool_name == "finalize_meta_result":
                    try:
                        finalized_payload = json.loads(output)
                    except Exception:
                        pass
                elif tool_name not in ("terminate",):
                    last_action_result = output
            yield _sse_line(event_to_legacy(event))

        if finalized_payload and isinstance(finalized_payload, dict):
            text_result = finalized_payload.get("text_result")
            viz = finalized_payload.get("visualization_data") if allow_viz else None
            file_result = finalized_payload.get("file_result")
        else:
            text_result = last_action_result
            viz = None
            file_result = None

        yield _sse_line({
            "is_final_result": True,
            "final_results": {
                "text_result": text_result,
                "visualization_data": viz,
                "file_result": file_result,
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
