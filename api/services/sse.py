"""SSE 协议适配：AgentEvent → 前端旧版格式转换、流式响应生成、cleanup 回调。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from fastapi.responses import StreamingResponse

from core.schema import AgentEvent
from core.task import TaskContext


def event_to_legacy(event: AgentEvent) -> dict:
    """将 AgentEvent 转换为前端期望的旧版 SSE 格式。

    旧版格式：每个 step_result 包含 thought / action / action_result 字段。
    """
    d: dict[str, Any] = {"step": event.step}
    t = event.type

    if t == "think":
        d["thought"] = event.data.get("thought", "")
        if "rag_docs" in event.data:
            d["rag_docs"] = event.data["rag_docs"]
    elif t == "tool_call":
        tool_name = event.data.get("tool", "")
        args = event.data.get("arguments", {})
        if tool_name == "bash" and "command" in args:
            d["action"] = f"{tool_name}: {args['command']}"
        else:
            d["action"] = tool_name
            if args:
                d["action_input"] = args
    elif t == "tool_result":
        tool = event.data.get("tool", "")
        output = event.data.get("result", "")
        d["action_result"] = f"Observed output of cmd `{tool}` executed:\n{output}"
    elif t == "error":
        d["error"] = event.data.get("error", "")
    elif t == "done":
        d["is_last"] = True
        d["result"] = event.data.get("result", "")
    return d


OutputFileSpec = dict  # {"name": str, "file": str}


async def sse_response(
    ctx: TaskContext,
    *,
    output_files: Sequence[OutputFileSpec] | None = None,
    zip_dir: str | None = None,
    cleanup: Callable[[], None] | None = None,
    session_id: str | None = None,
    components_meta: dict | None = None,
) -> StreamingResponse:
    """构建 SSE StreamingResponse。

    参数:
        ctx: TaskManager.submit() 返回的 TaskContext
        output_files: 任务完成后需要读取的输出文件列表
        zip_dir: 如果非空，任务完成后将此目录打包为 ZIP base64 返回
        cleanup: finally 阶段执行的清理回调
        session_id: 会话 ID，通过 header 返回给前端
    """

    async def generate():
        from api.services.files import pack_directory_as_zip_base64

        try:
            yield _sse_line({"status": "start"})

            if components_meta:
                yield _sse_line({"status": "components", **components_meta})

            async for event in ctx.subscribe():
                yield _sse_line(event_to_legacy(event))

            final: dict[str, Any] = {"is_last": True, "is_final_result": True}
            final_results: dict[str, Any] = {}

            if zip_dir and os.path.isdir(zip_dir):
                try:
                    zip_path = Path(zip_dir)
                    final_results["service_package"] = pack_directory_as_zip_base64(zip_dir)
                    final_results["output_files"] = [
                        {"name": f.name, "size": f.stat().st_size}
                        for f in sorted(zip_path.iterdir()) if f.is_file()
                    ]
                except Exception as e:
                    final_results["error"] = f"压缩目录失败: {e}"

            if output_files and not zip_dir:
                for spec in output_files:
                    name, fpath = spec["name"], spec["file"]
                    try:
                        p = Path(fpath)
                        if p.exists():
                            text = p.read_text(encoding="utf-8")
                            try:
                                final_results[name] = json.loads(text)
                            except json.JSONDecodeError:
                                final_results[name] = text
                    except Exception:
                        pass

            if final_results:
                final["final_results"] = final_results

            yield _sse_line(final)

        finally:
            if cleanup:
                try:
                    cleanup()
                except Exception:
                    pass

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Task-ID": ctx.task_id,
    }
    if session_id:
        headers["X-Session-ID"] = session_id

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=headers,
    )


def _sse_line(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
