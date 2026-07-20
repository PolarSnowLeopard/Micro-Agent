#!/usr/bin/env python3
"""Create a non-destructive, template-adapted AMQ-Bench subset.

The adapter sees only source code and ``wrap_intent``. Benchmark tasks, ground
truth, and deterministic verification criteria are copied into the derived
JSONL only after adaptation, and are never provided to the construction Agent.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Always use the checked-out implementation.  The benchmark server may also
# have an older ``micro_agent`` distribution installed in site-packages.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from micro_agent.packaging.analyzer import RepositoryAnalyzer  # noqa: E402
from micro_agent.packaging.discovery import (  # noqa: E402
    CapabilityDesign,
    CapabilityDesignValidationError,
    CapabilityDiscoveryWorkflow,
)
from micro_agent.packaging.template_adapter import (  # noqa: E402
    TemplateValidationReport,
    build_template_adapter_agent,
    template_adapter_prompt,
    validate_algorithm_template,
    verify_template_contract_runtime,
)
from scripts.run_amq_agentic_generation import prepare_source  # noqa: E402


EXPECTED_MINI30_SIZE = 30
PROTECTED_FIELDS = (
    "sample_id",
    "wrap_intent",
    "task",
    "ground_truth",
    "evaluation_criteria",
    "difficulty",
    "tags",
    "required_env",
    "_label_meta",
)
COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adapt official AMQ mini30 to the IOEB ZIP template")
    parser.add_argument("--benchmark-file", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repo-cache-root", type=Path, action="append", default=[])
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--sample-id",
        action="append",
        default=[],
        help="Adapt only the named mini30 sample; repeat for multiple samples.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_mini30(path: Path) -> list[dict[str, Any]]:
    samples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [sample.get("sample_id") for sample in samples]
    if len(samples) != EXPECTED_MINI30_SIZE:
        raise ValueError(f"mini30 must contain exactly {EXPECTED_MINI30_SIZE} rows, got {len(samples)}")
    if len(set(ids)) != len(ids) or any(not sample_id for sample_id in ids):
        raise ValueError("mini30 contains missing or duplicate sample_id values")
    for sample in samples:
        if not sample.get("wrap_intent") or not sample.get("repo_info", {}).get("url"):
            raise ValueError(f"sample is missing wrap_intent/repo_info: {sample.get('sample_id')}")
    return samples


def ensure_output_outside_source_repo(benchmark_file: Path, output_root: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(benchmark_file.parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return
    source_root = Path(result.stdout.strip()).resolve()
    try:
        output_root.resolve().relative_to(source_root)
    except ValueError:
        return
    raise ValueError(f"output root must be outside the original AMQ-Bench repository: {source_root}")


def acquire_output_lock(output_root: Path) -> Any:
    output_root.mkdir(parents=True, exist_ok=True)
    handle = (output_root / ".adaptation.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(
            f"another adaptation process is already writing {output_root}"
        ) from exc
    return handle


def _json_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_l0(sample: dict[str, Any]) -> bool:
    candidates = [
        sample.get("complexity"),
        sample.get("complexity_level"),
        sample.get("metadata", {}).get("complexity"),
        sample.get("metadata", {}).get("complexity_level"),
        sample.get("repo_info", {}).get("category"),
    ]
    return any(str(value).strip().upper().startswith("L0") for value in candidates if value is not None)


def _write_l0_entrypoint(project: Path, intent: str) -> None:
    reason = "This repository does not expose a packageable algorithm capability for the requested intent."
    (project / "main.py").write_text(
        '"""IOEB template entrypoint for an explicit unsupported negative control."""\n\n'
        "from __future__ import annotations\n\n\n"
        "def main_process(request: dict[str, object]) -> dict[str, object]:\n"
        '    """Reject a request that the source repository cannot implement.\n\n'
        "    Args:\n"
        "        request: JSON-compatible request payload.\n\n"
        "    Returns:\n"
        "        This function never returns successfully.\n"
        '    """\n'
        f"    raise RuntimeError({reason!r})\n",
        encoding="utf-8",
    )
    if not (project / "requirements.txt").exists():
        (project / "requirements.txt").write_text("", encoding="utf-8")
    readme = project / "README.ioeb.md"
    with readme.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n## Negative-control behavior\n\n"
            "This is an explicit unsupported entrypoint. It does not claim or fake algorithm "
            "functionality absent from the source repository.\n\n"
            f"Requested intent: {intent}\n"
        )


def _human_git_identity() -> tuple[str, str]:
    def read(key: str) -> str:
        result = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True, timeout=30)
        return result.stdout.strip() if result.returncode == 0 else ""

    name, email = read("user.name"), read("user.email")
    lowered = f"{name} {email}".lower()
    if not name or not email or "codex" in lowered or "openai" in lowered:
        raise RuntimeError("a configured human git user.name/user.email is required for derived repositories")
    return name, email


def _commit_derived_repo(project: Path, sample_id: str) -> str:
    name, email = _human_git_identity()
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
    )
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True, timeout=30, env=env)
    subprocess.run(["git", "add", "--all"], cwd=project, check=True, timeout=60, env=env)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", f"Adapt {sample_id} to IOEB template"],
        cwd=project,
        check=True,
        timeout=120,
        env=env,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_readme(project: Path, sample: dict[str, Any], source_sha: str) -> None:
    (project / "README.ioeb.md").write_text(
        "# IOEB template adaptation\n\n"
        "This directory is a derived copy prepared for template-compliant evaluation. "
        "The original AMQ-Bench repository and sample are unchanged.\n\n"
        f"- Sample: `{sample['sample_id']}`\n"
        f"- Source commit: `{sample['repo_info']['commit_sha']}`\n"
        f"- Source tree SHA-256: `{source_sha}`\n"
        f"- Requested intent: {sample['wrap_intent']}\n\n"
        "The adaptation Agent received only the requested intent and repository source. "
        "It did not receive the benchmark task, ground truth, or verification script.\n",
        encoding="utf-8",
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _protected_digest(sample: dict[str, Any]) -> str:
    return _json_digest(
        {key: value for key, value in sample.items() if key not in {"repo_info", "template_adaptation"}}
    )


_TEMPLATE_CANDIDATE_FILES = (
    "main.py",
    "requirements.txt",
    "tests_ioeb/test_template_contract.py",
)


def _save_template_snapshot(project: Path, run_dir: Path) -> None:
    """Persist the latest complete candidate independently of event history."""

    files = {
        relative: path.read_text(encoding="utf-8", errors="replace")
        for relative in _TEMPLATE_CANDIDATE_FILES
        if (path := project / relative).is_file() and not path.is_symlink()
    }
    if not files:
        return
    _write_json_atomic(
        run_dir / "candidate_snapshot.json",
        {
            "schemaVersion": "ioeb.template-candidate-snapshot/v1",
            "files": files,
        },
    )


def recover_last_template_writes(run_dir: Path) -> dict[str, str]:
    snapshot_path = run_dir / "candidate_snapshot.json"
    if snapshot_path.is_file():
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            files = snapshot.get("files", {})
            recovered_snapshot = {
                relative: content.rstrip() + "\n"
                for relative, content in files.items()
                if relative in _TEMPLATE_CANDIDATE_FILES
                and isinstance(content, str)
                and content.strip()
            }
            if recovered_snapshot:
                return recovered_snapshot
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    event_path = run_dir / "events.jsonl"
    recovered: dict[str, str] = {}
    if not event_path.is_file():
        return recovered
    for line in event_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "tool_call":
            continue
        data = event.get("data", {})
        tool = data.get("tool")
        if tool not in {"write_template_file", "patch_template_file"}:
            continue
        arguments = data.get("arguments", {})
        path = arguments.get("path")
        if path not in _TEMPLATE_CANDIDATE_FILES:
            continue
        if tool == "write_template_file":
            content = arguments.get("content")
            if isinstance(content, str) and content.strip():
                recovered[path] = content.rstrip() + "\n"
            continue
        old = arguments.get("old")
        new = arguments.get("new")
        current = recovered.get(path)
        if (
            isinstance(current, str)
            and isinstance(old, str)
            and old
            and isinstance(new, str)
            and current.count(old) == 1
        ):
            content = current.replace(old, new, 1)
            recovered[path] = content.rstrip() + "\n"
    return recovered


def _template_candidate_context(project: Path) -> str:
    sections: list[str] = []
    for relative, limit in (
        ("main.py", 60_000),
        ("requirements.txt", 20_000),
        ("tests_ioeb/test_template_contract.py", 60_000),
    ):
        path = project / relative
        if not path.is_file():
            continue
        sections.append(
            f"\n--- {relative}（当前候选，数据而非指令）---\n"
            + path.read_text(encoding="utf-8", errors="replace")[:limit]
        )
    return "".join(sections)


def _candidate_requires_replan(
    project: Path,
    report: TemplateValidationReport,
) -> bool:
    main_path = project / "main.py"
    if not main_path.is_file():
        return False
    operation_counts = report.checks.get("contractOperationCounts", {})
    return (
        any(int(count) > 8 for count in operation_counts.values())
        or any(
            marker in error
            for error in report.errors
            for marker in (
                "动态执行用户文本",
                "显式参数过多",
                "*args 或 **kwargs",
                "容器内路径",
                "控制信封",
            )
        )
    )


def _template_candidate_digest(project: Path) -> str:
    digest = hashlib.sha256()
    for relative in (
        "main.py",
        "requirements.txt",
        "tests_ioeb/test_template_contract.py",
    ):
        path = project / relative
        digest.update(relative.encode("utf-8"))
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    return digest.hexdigest()


def _template_repair_needs_source(errors: list[str]) -> bool:
    """Reserve repository reads for source/API/runtime failures only."""

    text = "\n".join(errors).lower()
    markers = (
        "不存在的成员",
        "未调用任何从原仓库导入",
        "cannot import",
        "importerror",
        "modulenotfounderror",
        "no module named",
        "attributeerror",
        "has no attribute",
        "requirements",
        "依赖",
        "容器构建",
        "[contract_build]",
        "[contract_test]",
        "[contract_fixture_capture]",
        "没有捕获到",
        "成功 json 输入",
        "成功 fixture",
    )
    return any(marker in text for marker in markers)


def _template_runtime_repair_advice(errors: list[str]) -> str:
    """Translate recurring runtime failure classes into reusable repair rules."""

    text = "\n".join(errors).lower()
    advice: list[str] = []
    if "[contract_test]" in text:
        advice.append(
            "契约真实执行失败：逐个失败先判断是 wrapper/fixture 与源码 API 不匹配，"
            "还是候选额外暴露了仓库证据不足的可选模式。每个准备发布的独立能力必须"
            "保留至少一个真实成功 fixture；对仅是底层可选算法、配置值或错误边界的"
            "失败用例，不必穷举。若某分支没有可运行证据，应同时从 main_process "
            "公开接口和契约测试删除该能力，不能只删测试而保留虚假接口；若能力有"
            "源码证据，则修正输入转换或 fixture，保留领域断言。"
        )
    if "必须是可审计的 json 字面量" in text:
        advice.append(
            "契约输入不可静态提取：只重写 tests_ioeb/test_template_contract.py，"
            "保留每个真实公开分支一个最小成功测试。每次 `main_process(...)` 的"
            "位置参数和关键字值必须直接写成 Constant/List/Tuple/Dict 组成的"
            " JSON 字面量；过长输入可先赋给同一测试函数内的局部变量，但变量值"
            "必须仍是纯 JSON 表达式；允许由常量组成的 list/string 拼接和重复"
            "（如 `[0.0] * 1024`），禁止模块变量、pytest fixture、helper 返回值、"
            "`**kwargs`、推导式或函数调用。若真实能力必须依赖文件或模型，应先把"
            " main_process 改为接受可内联的 Base64/文本/结构化内容，或在缺少"
            "真实运行资产时拒绝适配；不能在测试中临时生成随机模型、checkpoint"
            "或服务端路径来绕过契约。"
        )
    if any(
        marker in text
        for marker in (
            "size of tensor",
            "must match the size",
            "shape mismatch",
            "output_shape",
            "expected size",
            "broadcast_tensors",
        )
    ):
        advice.append(
            "张量/批次形状不一致：先读取原仓库被调用函数及其现有测试，逐维记录"
            "输入和输出的 batch/sequence/channel/feature 语义。优先修正测试 fixture "
            "或 main.py 中多余/缺失的 unsqueeze、transpose、reshape；不得通过"
            "裁剪、重复、广播 target 或修改领域断言掩盖错误。返回 JSON 时保留原始"
            "批次语义，并让 output_shape 与真实序列化结果完全一致。"
        )
    if any(
        marker in text
        for marker in (
            "temporary failure in name resolution",
            "urlopen error",
            "downloading:",
            "connectionerror",
        )
    ):
        advice.append(
            "隔离运行触发联网下载：只允许使用仓库已提交或已声明随部署提供的真实"
            "模型/数据资产，并从仓库相对路径加载。不要关闭无网络验证、不要 mock "
            "下载，也不能用随机权重替代预训练资产；若仓库没有所需资产，应明确拒绝"
            "适配该能力。"
        )
    if any(
        marker in text
        for marker in (
            "parseexception",
            "parse error",
            "invalid formula",
        )
    ):
        advice.append(
            "领域解析器拒绝 fixture：从原仓库测试、doctest 或文档示例选择一个"
            "真实可解析的最小输入，并保持 main.py 调用真实解析 API。不要改解析器、"
            "吞异常或降低断言；先单独核对输入语法，再修正契约 fixture。"
        )
    if (
        "控制信封" in text
        or (
            'result["success"]' in text
            and any(marker in text for marker in ("is true", "== true"))
        )
    ):
        advice.append(
            "异常被控制信封吞掉：删除 `except ...: return {'success': False, "
            "'error': ...}` 以及成功结果中的 success/operation/result 包装。"
            "成功分支直接返回领域字典/数组；失败分支保留原异常或 `raise "
            "ValueError(... ) from exc`。先让隔离运行输出真实 traceback，再依据"
            "真实 API/依赖错误修复，禁止继续断言 success=true 掩盖根因。"
        )
    if any(
        marker in text
        for marker in (
            "is not a state",
            "is not a constant",
            "is not a 'specified' symbol",
            "is not a specified symbol",
        )
    ):
        advice.append(
            "对象身份/名称映射错误：保持公开 JSON 字段稳定，不要用 sympify、"
            "dynamicsymbols、Enum 构造器等重新制造库对象。先枚举源对象公开的"
            "合法 state/constant/specified/enum 集合，再把每个真实对象按 str、"
            "去除时间后缀和仅用于展示的分隔符后的规范化别名建立索引"
            "（例如对两侧名称都执行 lower，并移除 `(t)` 与所有非字母数字字符）；"
            "将用户键映射回该集合中的原对象，未知键应明确拒绝。"
        )
    if any(
        marker in text
        for marker in (
            "sympifyerror",
            "could not parse",
            "tokenerror",
        )
    ):
        advice.append(
            "格式化结果被错误地重新解析：LaTeX、pretty-print、repr 或其他展示字符串"
            "只能作为最终 JSON 字符串输出，禁止再交给 sympify/parse/eval。"
            "应直接遍历原符号对象并调用一次稳定序列化函数；矩阵必须在 `tolist()` 后"
            "用嵌套列表逐元素格式化，不能用会把返回字符串重新强制转换为符号的"
            " `Matrix.applyfunc(serializer)`。"
        )
    if "invalid equivalence entry" in text:
        advice.append(
            "等价关系参数出现嵌套容器：读取目标 API 的 equivalencies 参数约定。"
            "Astropy 等价关系构造器通常已经返回 equivalency 列表，应直接传"
            " `equivalencies=u.spectral()`，不能再次包装为"
            " `equivalencies=[u.spectral()]`。其他返回列表的工厂同样按此处理。"
        )
    if (
        "keyerror:" in text
        or " in {}" in text
        or (
            "assertionerror: assert" in text
            and any(marker in text for marker in (" in result", "not in result"))
        )
    ):
        advice.append(
            "调用成功但领域集合为空或缺少断言字段：先沿真实源码、测试和数据列追踪"
            "输入标签对应的 canonical vocabulary/enum/key，判断是公开输入需要映射"
            "（大小写、分隔符、别名）还是当前 fixture 没有源码证据。若 API 承诺接受"
            "该别名，应在 main.py 显式规范化并映射到真实值；否则只把 fixture 改成"
            "原仓库测试/doctest/示例中确实产生非空结果的字面量。禁止伪造返回键、"
            "硬填空结果或删除领域断言来制造通过。"
        )
    if "deprecationwarning" in text or "deprecated" in text:
        advice.append(
            "运行日志已明确标记当前源码 API deprecated：使用 search_project_text "
            "搜索警告给出的替代类/函数及其 run/extract 用法，读取替代实现后重构 "
            "main.py。不得继续围绕弃用入口反复调整参数，也不得仅屏蔽警告。"
        )
    if (
        "datapoint does not have an 'ecg' attribute" in text
        or "does not seem to be a ecgrawdataframe" in text
        or "no valid sampling rate attribute" in text
    ):
        advice.append(
            "当前 Pipeline.run 接受的是 datapoint 对象，不是 pandas DataFrame/Series "
            "本身。读取 run() 对 datapoint 属性的访问方式；在 main_process 内构造一个"
            "局部轻量适配对象（如 SimpleNamespace），令其 .ecg 为源码校验要求的 "
            "DataFrame，并提供 .sampling_rate_hz/.sampling_rate 等 run() 查找的"
            "采样率属性。不要把 fs 动态属性挂到 Series 后直接传入。"
        )
    if "numpy' has no attribute 'trapz" in text:
        advice.append(
            "这是 NumPy 2.x 移除 np.trapz 与上游算法库未适配的确定性依赖冲突。"
            "检查原仓库依赖约束；若该版本仍调用 np.trapz，在 requirements.txt "
            "将 NumPy 约束为兼容的 `numpy>=1.26,<2`。禁止在 main.py monkeypatch "
            "numpy、复制上游算法或吞掉 HRV 计算错误。"
        )
    if not advice:
        return ""
    return (
        "\n检测到跨仓库常见运行时失败模式，修复时必须遵循：\n- "
        + "\n- ".join(advice)
    )


async def adapt_one(
    sample: dict[str, Any],
    *,
    output_root: Path,
    cache_roots: list[Path],
    max_attempts: int,
    resume: bool,
) -> dict[str, Any]:
    sample_id = sample["sample_id"]
    run_dir = output_root / "adaptation" / sample_id
    repo_dir = output_root / "adapted_repos" / sample_id
    derived_path = run_dir / "derived_sample.json"
    summary_path = run_dir / "summary.json"
    if resume and derived_path.is_file() and summary_path.is_file() and (repo_dir / ".git").is_dir():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("status") == "ready":
            negative_control = bool(summary.get("negativeControl"))
            resumed_report = validate_algorithm_template(
                repo_dir,
                allow_explicit_unsupported=negative_control,
                require_contract_test=not negative_control,
                allow_runtime_collected_contract=not negative_control,
            )
            adaptation_metadata: dict[str, Any] = {}
            metadata_path = repo_dir / "template_adaptation.json"
            if metadata_path.is_file():
                try:
                    adaptation_metadata = json.loads(
                        metadata_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    adaptation_metadata = {}
            contract_verified = negative_control or bool(
                adaptation_metadata.get("contractRuntime", {}).get("passed")
            )
            if resumed_report.passed and contract_verified:
                print(f"[{sample_id}] resume: ready", flush=True)
                return json.loads(derived_path.read_text(encoding="utf-8"))
            print(
                f"[{sample_id}] resume: stale validation/runtime proof, regenerating",
                flush=True,
            )

    recovered_writes = recover_last_template_writes(run_dir) if resume else {}
    recovered_design_data: dict[str, Any] | None = None
    recovered_design_path = run_dir / "capability_design.json"
    if resume and recovered_design_path.is_file():
        try:
            candidate = json.loads(
                recovered_design_path.read_text(encoding="utf-8")
            )
            if isinstance(candidate, dict):
                recovered_design_data = candidate
        except (OSError, json.JSONDecodeError):
            recovered_design_data = None
    shutil.rmtree(run_dir, ignore_errors=True)
    shutil.rmtree(repo_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    summary: dict[str, Any] = {
        "sampleId": sample_id,
        "status": "failed",
        "constructionInput": "source_and_wrap_intent_only",
        "protectedFieldsDigest": _protected_digest(sample),
    }

    try:
        with tempfile.TemporaryDirectory(prefix=f".{sample_id}-", dir=repo_dir.parent) as temp_dir:
            source = Path(temp_dir) / "source"
            staged = Path(temp_dir) / "adapted"
            cache_source, clone_seconds = await asyncio.to_thread(prepare_source, sample, source, cache_roots)
            source_tree_sha = await asyncio.to_thread(_tree_digest, source)
            await asyncio.to_thread(shutil.copytree, source, staged, ignore=COPY_IGNORE)
            summary.update(
                {
                    "repositorySource": cache_source,
                    "cloneSeconds": round(clone_seconds, 3),
                    "originalCommit": sample["repo_info"]["commit_sha"],
                    "originalTreeSha256": source_tree_sha,
                }
            )

            original_main: str | None = None
            if (staged / "main.py").exists():
                original_main = "ioeb_original_main.py"
                if (staged / original_main).exists():
                    raise RuntimeError(f"cannot preserve original main.py because {original_main} already exists")
                (staged / "main.py").replace(staged / original_main)
            if (staged / "requirements.txt").exists():
                shutil.copy2(staged / "requirements.txt", staged / "requirements.original.txt")
            _write_readme(staged, sample, source_tree_sha)

            is_l0 = _is_l0(sample)
            errors: list[str] = []
            runtime_report = None
            static_repair_attempts = 0
            runtime_repair_attempts = 0
            runtime_repair_budget = max(2, max_attempts)
            no_op_attempts = 0
            no_op_attempt_budget = max(2, (max_attempts + 1) // 2)
            capability_design = None
            capability_count = None
            if is_l0:
                _write_l0_entrypoint(staged, sample["wrap_intent"])
            else:
                event_path = run_dir / "events.jsonl"
                discovery_ir = await asyncio.to_thread(
                    RepositoryAnalyzer().analyze,
                    staged,
                )
                capability_design = None
                discovery_errors: list[str] = []
                if recovered_design_data is not None:
                    try:
                        capability_design = CapabilityDesign.validate(
                            recovered_design_data,
                            known_symbols=discovery_ir.known_symbols,
                            known_files={
                                file.path for file in discovery_ir.files
                            },
                        )
                    except CapabilityDesignValidationError as exc:
                        discovery_errors = exc.errors
                    else:
                        (run_dir / "capability_design.json").write_text(
                            capability_design.to_json() + "\n",
                            encoding="utf-8",
                        )
                        summary["reusedCapabilityDiscovery"] = True
                if capability_design is None:
                    discovery = CapabilityDiscoveryWorkflow(
                        project_dir=staged,
                        ir=discovery_ir,
                        design_path=run_dir / "capability_design.json",
                    )
                    with event_path.open("a", encoding="utf-8") as handle:
                        async for event in discovery.run(
                            sample["wrap_intent"]
                        ):
                            handle.write(
                                json.dumps(
                                    {
                                        "phase": "capability_discovery",
                                        **event.to_dict(),
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                )
                                + "\n"
                            )
                    capability_design = discovery.store.design
                    discovery_errors = discovery.store.last_errors or []
                capability_count = (
                    len(capability_design.capabilities)
                    if capability_design is not None
                    else None
                )
                summary["capabilityDiscovery"] = {
                    "completed": capability_design is not None,
                    "decision": (
                        capability_design.decision
                        if capability_design is not None
                        else None
                    ),
                    "capabilityCount": (
                        len(capability_design.capabilities)
                        if capability_design is not None
                        else 0
                    ),
                    "errors": discovery_errors,
                }
                for relative, content in recovered_writes.items():
                    target = staged / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                _save_template_snapshot(staged, run_dir)
                recovered_report = validate_algorithm_template(
                    staged,
                    require_contract_test=True,
                    allow_runtime_collected_contract=True,
                    max_contract_success_fixtures=capability_count,
                )
                if recovered_writes:
                    _write_json_atomic(run_dir / "validation_recovered.json", recovered_report.to_dict())
                    summary["recoveredFromPriorRun"] = True
                if _candidate_requires_replan(staged, recovered_report):
                    summary["candidateReplans"] = 1
                    errors = [
                        "此前完整候选的语义边界过宽或含动态执行。候选已保留，"
                        "必须在其基础上收敛为 1–6 个内聚能力，最多 12 个显式参数、"
                        "8 个不同 operation 和 30 个 fixture；不得从空白重新开始。",
                        *recovered_report.errors,
                    ]
                elif recovered_report.passed:
                    runtime_report = await asyncio.to_thread(
                        verify_template_contract_runtime,
                        staged,
                        max_contract_success_fixtures=capability_count,
                    )
                    _write_json_atomic(
                        run_dir / "contract_runtime_validation_initial.json",
                        runtime_report.to_dict(),
                    )
                    summary["adaptationAttempts"] = 0
                    errors = [] if runtime_report.passed else runtime_report.errors
                else:
                    errors = recovered_report.errors
                attempt = 0
                while errors:
                    runtime_phase = (
                        runtime_report is not None
                        and not runtime_report.passed
                    )
                    if runtime_phase:
                        if runtime_repair_attempts >= runtime_repair_budget:
                            break
                        runtime_repair_attempts += 1
                    else:
                        if static_repair_attempts >= max_attempts:
                            break
                        static_repair_attempts += 1
                    attempt += 1
                    ir = await asyncio.to_thread(RepositoryAnalyzer().analyze, staged)
                    repair_candidate = staged / "main.py"
                    repair_mode = bool(errors and repair_candidate.is_file())
                    agent = build_template_adapter_agent(
                        staged,
                        ir,
                        repair=repair_mode,
                        repair_source_reads=(
                            _template_repair_needs_source(errors)
                            if repair_mode
                            else True
                        ),
                        capability_count=capability_count,
                    )
                    prompt = template_adapter_prompt(
                        ir,
                        sample["wrap_intent"],
                        original_main,
                        capability_design=capability_design,
                    )
                    if errors:
                        prompt += (
                            "\n上一次确定性校验错误，请全部修复：\n"
                            + "\n".join(errors)
                            + _template_runtime_repair_advice(errors)
                        )
                    if repair_mode:
                        prompt += (
                            _template_candidate_context(staged)
                            + "\n只修改上述错误；优先用 patch_template_file 精确替换，"
                            "需要整体重构时才用 write_template_file 提交完整文件。"
                            "最后必须调用 verify_template，纯文本说明不算完成。"
                        )
                    candidate_before = _template_candidate_digest(staged)
                    with event_path.open("a", encoding="utf-8") as handle:
                        async for event in agent.run(prompt):
                            handle.write(
                                json.dumps({"attempt": attempt, **event.to_dict()}, ensure_ascii=False, default=str)
                                + "\n"
                            )
                    _save_template_snapshot(staged, run_dir)
                    if _template_candidate_digest(staged) == candidate_before:
                        if runtime_phase:
                            runtime_repair_attempts -= 1
                        else:
                            static_repair_attempts -= 1
                        no_op_attempts += 1
                        if no_op_attempts >= no_op_attempt_budget:
                            break
                        continue
                    report = validate_algorithm_template(
                        staged,
                        require_contract_test=True,
                        allow_runtime_collected_contract=True,
                        max_contract_success_fixtures=capability_count,
                    )
                    _write_json_atomic(run_dir / f"validation_attempt_{attempt}.json", report.to_dict())
                    errors = report.errors
                    if not report.passed:
                        runtime_report = None
                        if _candidate_requires_replan(staged, report):
                            summary["candidateReplans"] = (
                                int(summary.get("candidateReplans", 0)) + 1
                            )
                            errors = [
                                "当前完整候选的语义边界过宽或含动态执行。候选已保留，"
                                "必须直接重构为 1–6 个内聚能力，最多 12 个显式参数、"
                                "8 个不同 operation 和 30 个 fixture；不得从空白重新开始。",
                                *report.errors,
                            ]
                        continue
                    runtime_report = await asyncio.to_thread(
                        verify_template_contract_runtime,
                        staged,
                        max_contract_success_fixtures=capability_count,
                    )
                    _write_json_atomic(
                        run_dir / f"contract_runtime_validation_attempt_{attempt}.json",
                        runtime_report.to_dict(),
                    )
                    errors = [] if runtime_report.passed else runtime_report.errors
                    if runtime_report.passed:
                        summary["adaptationAttempts"] = attempt
                        break
                summary["staticRepairAttempts"] = static_repair_attempts
                summary["runtimeRepairAttempts"] = runtime_repair_attempts
                summary["runtimeRepairBudget"] = runtime_repair_budget
                summary["noOpAttempts"] = no_op_attempts
                summary["noOpAttemptBudget"] = no_op_attempt_budget

            report = validate_algorithm_template(
                staged,
                allow_explicit_unsupported=is_l0,
                require_contract_test=not is_l0,
                allow_runtime_collected_contract=not is_l0,
                max_contract_success_fixtures=capability_count,
            )
            _write_json_atomic(run_dir / "validation.json", report.to_dict())
            if not report.passed:
                raise RuntimeError("template validation failed: " + "; ".join(report.errors))
            if not is_l0 and (runtime_report is None or not runtime_report.passed):
                runtime_errors = (
                    runtime_report.errors
                    if runtime_report is not None
                    else ["模板契约未执行"]
                )
                raise RuntimeError(
                    "template contract runtime validation failed: "
                    + "; ".join(runtime_errors)
                )

            metadata = {
                "schemaVersion": "ioeb.amq-template-adaptation/v1",
                "sampleId": sample_id,
                "createdAt": datetime.now().astimezone().isoformat(),
                "constructionInput": "source_and_wrap_intent_only",
                "benchmarkFieldsHiddenFromAgent": ["task", "ground_truth", "evaluation_criteria"],
                "originalRepoInfo": sample["repo_info"],
                "originalTreeSha256": source_tree_sha,
                "negativeControl": is_l0,
                "capabilityDesign": (
                    capability_design.to_dict()
                    if capability_design is not None
                    else None
                ),
                "validation": report.to_dict(),
                "contractRuntime": (
                    runtime_report.to_dict() if runtime_report is not None else None
                ),
            }
            _write_json_atomic(staged / "template_adaptation.json", metadata)
            staged.rename(repo_dir)
            commit = await asyncio.to_thread(_commit_derived_repo, repo_dir, sample_id)

        derived = dict(sample)
        derived["repo_info"] = {"url": repo_dir.resolve().as_uri(), "commit_sha": commit}
        derived["template_adaptation"] = {
            "schemaVersion": "ioeb.amq-template-adaptation/v1",
            "originalRepoInfo": sample["repo_info"],
            "originalTreeSha256": summary["originalTreeSha256"],
            "constructionInput": "source_and_wrap_intent_only",
            "negativeControl": is_l0,
            "contractRuntime": (
                runtime_report.to_dict() if runtime_report is not None else None
            ),
        }
        if _protected_digest(derived) != summary["protectedFieldsDigest"]:
            raise RuntimeError("protected benchmark fields changed during adaptation")
        _write_json_atomic(derived_path, derived)
        summary.update(
            {
                "status": "ready",
                "derivedCommit": commit,
                "templatePassed": True,
                "contractRuntimePassed": bool(
                    is_l0 or (runtime_report is not None and runtime_report.passed)
                ),
                "negativeControl": is_l0,
            }
        )
        return derived
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        summary["totalSeconds"] = round(time.perf_counter() - started, 3)
        _write_json_atomic(summary_path, summary)
        print(f"[{sample_id}] {summary['status']} seconds={summary['totalSeconds']}", flush=True)


async def main() -> int:
    args = parse_args()
    if args.concurrency < 1 or args.max_attempts < 1:
        raise SystemExit("--concurrency and --max-attempts must be >= 1")
    benchmark_file = args.benchmark_file.resolve()
    output_root = args.output_root.resolve()
    ensure_output_outside_source_repo(benchmark_file, output_root)
    samples = load_mini30(benchmark_file)
    if args.sample_id:
        selected_ids = list(dict.fromkeys(args.sample_id))
        known_ids = {sample["sample_id"] for sample in samples}
        unknown_ids = sorted(set(selected_ids) - known_ids)
        if unknown_ids:
            raise SystemExit("unknown --sample-id values: " + ", ".join(unknown_ids))
        selected = set(selected_ids)
        samples = [
            sample for sample in samples if sample["sample_id"] in selected
        ]
    original_sha = sha256_file(benchmark_file)
    output_lock = acquire_output_lock(output_root)
    cache_roots = [path.resolve() for path in args.repo_cache_root]
    if not cache_roots:
        cache_roots = [Path(os.getenv("REPO_CACHE_ROOT", ".repo_cache")).resolve()]

    protocol = {
        "schemaVersion": "ioeb.amq-template-subset/v1",
        "sourceBenchmark": str(benchmark_file),
        "sourceBenchmarkSha256": original_sha,
        "sampleCount": len(samples),
        "sourceSampleCount": EXPECTED_MINI30_SIZE,
        "selectedSampleIds": [sample["sample_id"] for sample in samples],
        "constructionInput": "source_and_wrap_intent_only",
        "protectedFields": list(PROTECTED_FIELDS),
        "concurrency": args.concurrency,
    }
    _write_json_atomic(output_root / "adaptation_protocol.json", protocol)
    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded(sample: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await adapt_one(
                sample,
                output_root=output_root,
                cache_roots=cache_roots,
                max_attempts=args.max_attempts,
                resume=args.resume,
            )

    results = await asyncio.gather(*(bounded(sample) for sample in samples), return_exceptions=True)
    failures = {
        samples[index]["sample_id"]: f"{type(result).__name__}: {result}"
        for index, result in enumerate(results)
        if isinstance(result, BaseException)
    }
    successful = {
        result["sample_id"]: result for result in results if isinstance(result, dict)
    }
    ordered = [successful[sample["sample_id"]] for sample in samples if sample["sample_id"] in successful]
    target = output_root / "data" / (
        (
            "mini30_template_adapted.selected.jsonl"
            if args.sample_id
            else "mini30_template_adapted.jsonl"
        )
        if not failures
        else "mini30_template_adapted.partial.jsonl"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(sample, ensure_ascii=False) + "\n" for sample in ordered), encoding="utf-8"
    )
    source_unchanged = sha256_file(benchmark_file) == original_sha
    summary = {
        **protocol,
        "complete": not failures and len(ordered) == len(samples),
        "successfulSamples": len(ordered),
        "failedSamples": failures,
        "derivedBenchmark": str(target),
        "derivedBenchmarkSha256": sha256_file(target),
        "sourceBenchmarkUnchanged": source_unchanged,
    }
    _write_json_atomic(output_root / "adaptation_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    fcntl.flock(output_lock.fileno(), fcntl.LOCK_UN)
    output_lock.close()
    return 0 if summary["complete"] and source_unchanged else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
