"""Command-line interface for the standalone packaging engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from mcp_packager.amq_suite import prepare_amq_suite
from mcp_packager.batch import run_template_batch
from mcp_packager.engine import build_package, create_plan, validate_package
from mcp_packager.quality import aggregate_quality, score_verification
from mcp_packager.scaffold import TEMPLATE_VERSION, create_scaffold
from mcp_packager.verifier import verify_artifact_docker, verify_artifact_static


def _print_json(value: Dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source", type=Path, help="Python file, project directory, or ZIP package")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="require the production manifest, tests, and reproducible dependencies",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-packager",
        description="Validate and compile IoEB algorithm packages into verified MCP services.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="create a versioned IoEB production algorithm template"
    )
    init_parser.add_argument("--output", "-o", type=Path, required=True)
    init_parser.add_argument("--force", action="store_true")

    validate_parser = subparsers.add_parser("validate", help="validate an algorithm package")
    _add_source_options(validate_parser)

    plan_parser = subparsers.add_parser("plan", help="emit the deterministic packaging plan")
    _add_source_options(plan_parser)

    build_parser_command = subparsers.add_parser("build", help="generate an MCP service artifact")
    _add_source_options(build_parser_command)
    build_parser_command.add_argument("--output", "-o", type=Path, required=True)
    build_parser_command.add_argument("--force", action="store_true")

    verify_parser = subparsers.add_parser("verify", help="verify a generated service artifact")
    verify_parser.add_argument("artifact", type=Path)
    verify_parser.add_argument(
        "--docker",
        action="store_true",
        help="build, start, initialize, list tools, and execute manifest test cases",
    )
    verify_parser.add_argument("--build-timeout", type=int, default=600)
    verify_parser.add_argument("--startup-timeout", type=int, default=60)
    verify_parser.add_argument("--execution-timeout", type=int, default=120)
    verify_parser.add_argument("--keep-image", action="store_true")
    verify_parser.add_argument(
        "--no-cache", action="store_true", help="disable Docker layer cache for this build"
    )

    score_parser = subparsers.add_parser(
        "score", help="compute strict AMQ-compatible D1/D2/D3 quality metrics"
    )
    score_parser.add_argument("verification_report", type=Path)

    aggregate_parser = subparsers.add_parser(
        "aggregate", help="aggregate multiple quality reports for system indicators"
    )
    aggregate_parser.add_argument("quality_reports", type=Path, nargs="+")

    batch_parser = subparsers.add_parser(
        "batch", help="run a strict Template Track batch evaluation"
    )
    batch_parser.add_argument("root", type=Path)
    batch_parser.add_argument("--output", "-o", type=Path, required=True)
    batch_parser.add_argument("--docker", action="store_true")
    batch_parser.add_argument("--build-timeout", type=int, default=600)
    batch_parser.add_argument("--startup-timeout", type=int, default=60)
    batch_parser.add_argument("--execution-timeout", type=int, default=120)
    batch_parser.add_argument(
        "--no-cache", action="store_true", help="disable Docker layer cache for every case"
    )

    suite_parser = subparsers.add_parser(
        "amq-suite", help="prepare a leakage-aware trusted suite from AMQ-Bench"
    )
    suite_parser.add_argument("dataset", type=Path, help="AMQ-Bench JSONL dataset")
    suite_parser.add_argument("--status", type=Path, required=True, help="sample_status.json")
    suite_parser.add_argument(
        "--development-ids",
        type=Path,
        help="JSON list used as the visible development seed",
    )
    suite_parser.add_argument("--output", "-o", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            output = create_scaffold(args.output, force=args.force)
            _print_json(
                {
                    "success": True,
                    "templateVersion": TEMPLATE_VERSION,
                    "output": str(output),
                }
            )
            return 0

        if args.command == "validate":
            report = validate_package(args.source, strict=args.strict)
            _print_json(report.to_dict())
            return 0 if report.valid else 2

        if args.command == "plan":
            report, plan = create_plan(args.source, strict=args.strict)
            result: Dict[str, Any] = {"validation": report.to_dict()}
            if plan is not None:
                result["plan"] = plan.to_dict()
            _print_json(result)
            return 0 if plan is not None else 2

        if args.command == "build":
            report, plan, artifact = build_package(
                args.source,
                args.output,
                strict=args.strict,
                force=args.force,
            )
            result = {"validation": report.to_dict()}
            if plan is not None:
                result["plan"] = plan.to_dict()
            if artifact is not None:
                result["artifact"] = str(artifact)
            _print_json(result)
            return 0 if artifact is not None else 2

        if args.command == "verify":
            if args.docker:
                verification = verify_artifact_docker(
                    args.artifact,
                    build_timeout=args.build_timeout,
                    startup_timeout=args.startup_timeout,
                    execution_timeout=args.execution_timeout,
                    keep_image=args.keep_image,
                    no_cache=args.no_cache,
                )
            else:
                verification = verify_artifact_static(args.artifact)
            _print_json(verification)
            return 0 if verification["success"] else 2

        if args.command == "score":
            verification = json.loads(
                args.verification_report.read_text(encoding="utf-8")
            )
            quality = score_verification(verification)
            _print_json(quality)
            return 0

        if args.command == "aggregate":
            reports = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in args.quality_reports
            ]
            _print_json(aggregate_quality(reports))
            return 0

        if args.command == "batch":
            batch = run_template_batch(
                args.root,
                docker=args.docker,
                build_timeout=args.build_timeout,
                startup_timeout=args.startup_timeout,
                execution_timeout=args.execution_timeout,
                no_cache=args.no_cache,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(batch, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            _print_json(
                {
                    "success": batch["success"],
                    "output": str(args.output.resolve()),
                    "summary": batch["summary"],
                    "aggregateQuality": batch["aggregateQuality"],
                }
            )
            return 0 if batch["success"] else 2

        if args.command == "amq-suite":
            suite = prepare_amq_suite(
                args.dataset,
                args.status,
                development_ids=args.development_ids,
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(
                    json.dumps(suite, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            _print_json(suite)
            return 0
    except (FileExistsError, OSError, ValueError) as exc:
        _print_json(
            {
                "success": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
