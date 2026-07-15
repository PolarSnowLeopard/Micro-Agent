"""AMQ-Bench-compatible quality scoring for generated MCP services."""

from __future__ import annotations

import re
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional


QUALITY_VERSION = "ioeb.amq-quality/v2"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]+")
CONSTRAINT_KEYS = {
    "const",
    "enum",
    "default",
    "format",
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "required",
}


def _rounded(value: float) -> float:
    return round(float(value), 4)


def _ratio(values: Iterable[bool]) -> float:
    items = list(values)
    return _rounded(sum(items) / len(items)) if items else 0.0


def _tokens(value: str) -> List[str]:
    return [token.lower() for token in TOKEN_PATTERN.findall(value)]


def _tool_distinguishability(tools: List[Dict[str, Any]]) -> float:
    if len(tools) <= 1:
        return 1.0
    distances = []
    for left_index, left in enumerate(tools):
        left_tokens = set(_tokens(left.get("description", "")))
        for right in tools[left_index + 1 :]:
            right_tokens = set(_tokens(right.get("description", "")))
            union = left_tokens | right_tokens
            similarity = len(left_tokens & right_tokens) / len(union) if union else 1.0
            distances.append(1.0 - similarity)
    return _rounded(mean(distances)) if distances else 1.0


def _schema_has_constraint(schema: Dict[str, Any]) -> bool:
    if any(key in schema for key in CONSTRAINT_KEYS):
        return True
    if schema.get("additionalProperties") is False:
        return True
    properties = schema.get("properties")
    if isinstance(properties, dict) and any(
        _schema_has_constraint(item)
        for item in properties.values()
        if isinstance(item, dict)
    ):
        return True
    items = schema.get("items")
    if isinstance(items, dict) and _schema_has_constraint(items):
        return True
    prefix_items = schema.get("prefixItems")
    return isinstance(prefix_items, list) and any(
        _schema_has_constraint(item) for item in prefix_items if isinstance(item, dict)
    )


def _extract_runtime(verification: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if verification.get("verificationVersion") == "ioeb.mcp-runtime-verification/v1":
        return verification
    runtime = verification.get("runtime")
    return runtime if isinstance(runtime, dict) else None


def _extract_tools(runtime: Dict[str, Any]) -> List[Dict[str, Any]]:
    tools = runtime.get("tools")
    if isinstance(tools, list):
        return [tool for tool in tools if isinstance(tool, dict)]
    for check in runtime.get("checks", []):
        if check.get("name") == "tools/list" and check.get("success"):
            return [
                {
                    "name": check.get("name", "main_process"),
                    "description": check.get("description", ""),
                    "inputSchema": check.get("inputSchema") or {},
                    "outputSchema": check.get("outputSchema") or {},
                }
            ]
    return []


def _d1_metrics(
    verification: Dict[str, Any], runtime: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    build_success: Optional[bool] = None
    if verification.get("mode") == "docker":
        checks = verification.get("checks", [])
        if any(check.get("name") == "docker:build" for check in checks):
            build_success = any(
                check.get("name") == "docker:build" and check.get("success") is True
                for check in checks
            )
        elif verification.get("error", {}).get("type") not in {"DockerUnavailable", None}:
            build_success = False

    health_success: Optional[bool] = None
    if runtime is not None:
        check_names = {
            check.get("name"): check.get("success")
            for check in runtime.get("checks", [])
        }
        health_success = bool(
            check_names.get("initialize") and check_names.get("tools/list")
        )
    elif verification.get("mode") == "docker" and build_success is True:
        health_success = False

    if build_success is False or health_success is False:
        score: Optional[float] = 0.0
    elif build_success is True and health_success is True:
        score = 1.0
    else:
        score = None
    return {
        "score": score,
        "buildSuccess": build_success,
        "healthSuccess": health_success,
    }


def _goe_metrics(tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not tools:
        return {
            "score": 0.0,
            "toolSelectionSupport": 0.0,
            "argumentConstructionSupport": 0.0,
            "toolDescCoverage": 0.0,
            "toolDescInformativeness": 0.0,
            "toolDescDistinguishability": 0.0,
            "paramDescCoverage": 0.0,
            "paramTypeCoverage": 0.0,
            "constraintRichness": 0.0,
            "requiredClarity": 0.0,
        }

    descriptions = [tool.get("description", "").strip() for tool in tools]
    desc_coverage = _ratio(bool(description) for description in descriptions)
    desc_informativeness = _rounded(
        min(sum(len(_tokens(description)) for description in descriptions) / (25 * len(tools)), 1.0)
    )
    desc_distinguishability = _tool_distinguishability(tools)

    parameters: List[Dict[str, Any]] = []
    for tool in tools:
        schema = tool.get("inputSchema") or {}
        required = set(schema.get("required") or [])
        for name, parameter_schema in (schema.get("properties") or {}).items():
            parameters.append(
                {
                    "name": name,
                    "schema": parameter_schema if isinstance(parameter_schema, dict) else {},
                    "required": name in required,
                }
            )
    param_desc_coverage = _ratio(
        bool(parameter["schema"].get("description", "").strip())
        for parameter in parameters
    )
    param_type_coverage = _ratio(
        bool(
            parameter["schema"].get("type")
            or parameter["schema"].get("anyOf")
            or parameter["schema"].get("enum")
            or "const" in parameter["schema"]
        )
        for parameter in parameters
    )
    constraint_richness = _ratio(
        _schema_has_constraint(parameter["schema"])
        for parameter in parameters
    )
    required_clarity = _ratio(
        parameter["required"] or "default" in parameter["schema"]
        for parameter in parameters
    )

    tool_selection = _rounded(
        mean([desc_coverage, desc_informativeness, desc_distinguishability])
    )
    argument_construction = _rounded(
        mean(
            [
                param_desc_coverage,
                param_type_coverage,
                constraint_richness,
                required_clarity,
            ]
        )
    )
    return {
        "score": _rounded(mean([tool_selection, argument_construction])),
        "toolSelectionSupport": tool_selection,
        "argumentConstructionSupport": argument_construction,
        "toolDescCoverage": desc_coverage,
        "toolDescInformativeness": desc_informativeness,
        "toolDescDistinguishability": desc_distinguishability,
        "paramDescCoverage": param_desc_coverage,
        "paramTypeCoverage": param_type_coverage,
        "constraintRichness": constraint_richness,
        "requiredClarity": required_clarity,
    }


def _gov_metrics(
    tools: List[Dict[str, Any]], cases: List[Dict[str, Any]], probes: List[Dict[str, Any]]
) -> Dict[str, Any]:
    return_desc_coverage = _ratio(
        any(marker in tool.get("description", "").lower() for marker in ("return", "output", "返回", "输出"))
        for tool in tools
    )
    successful_cases = [case for case in cases if case.get("success")]
    response_structure_rate = _ratio(
        isinstance(case.get("mcp"), (dict, list)) for case in successful_cases
    )
    error_handling_rate = _ratio(bool(probe.get("handled")) for probe in probes)
    error_specificity = _ratio(bool(probe.get("specific")) for probe in probes)
    result_interpretability = _rounded(
        mean([return_desc_coverage, response_structure_rate])
    )
    error_recoverability = _rounded(
        0.6 * error_handling_rate + 0.4 * error_specificity
    )
    return {
        "score": _rounded(mean([result_interpretability, error_recoverability])),
        "resultInterpretability": result_interpretability,
        "errorRecoverability": error_recoverability,
        "returnDescCoverage": return_desc_coverage,
        "responseStructureRate": response_structure_rate,
        "errorHandlingRate": error_handling_rate,
        "errorSpecificity": error_specificity,
    }


def _classify_failure(
    verification: Dict[str, Any],
    d1: Dict[str, Any],
    d3_score: Optional[float],
    probe_gate: Optional[bool],
) -> Optional[str]:
    error_type = verification.get("error", {}).get("type", "")
    if error_type == "DockerUnavailable":
        return "docker_unavailable"
    if d1["buildSuccess"] is False:
        log = (verification.get("buildLogTail") or "").lower()
        if "no matching distribution" in log or "could not find a version" in log:
            return "dependency_error"
        if "modulenotfounderror" in log or "importerror" in log:
            return "import_error"
        if "syntaxerror" in log or "indentationerror" in log:
            return "code_error"
        return "build_error"
    if d1["healthSuccess"] is False:
        runtime_log = " ".join(
            str(value).lower()
            for value in (
                verification.get("containerLogTail"),
                verification.get("error", {}).get("message"),
            )
            if value
        )
        if "modulenotfounderror" in runtime_log or "importerror" in runtime_log:
            return "import_error"
        if "timed out" in runtime_log or "timeout" in runtime_log:
            return "runtime_timeout"
        return "protocol_or_runtime_error"
    if d3_score == 0.0:
        return "functional_mismatch"
    if probe_gate is False:
        return "input_validation_failure"
    return None


def score_verification(verification: Dict[str, Any]) -> Dict[str, Any]:
    """Score a static, Docker, or raw runtime verification report."""
    runtime = _extract_runtime(verification)
    tools = _extract_tools(runtime) if runtime else []
    cases = runtime.get("cases", []) if runtime else []
    probes = runtime.get("probes", []) if runtime else []
    d1 = _d1_metrics(verification, runtime)

    d2: Optional[Dict[str, Any]] = None
    if runtime is not None and tools:
        goe = _goe_metrics(tools)
        gov = _gov_metrics(tools, cases, probes)
        d2 = {"score": _rounded(mean([goe["score"], gov["score"]])), "goe": goe, "gov": gov}

    d3_score: Optional[float] = None
    if runtime is not None and cases:
        d3_score = 1.0 if all(case.get("success") for case in cases) else 0.0
    d3 = {
        "score": d3_score,
        "passedCases": sum(bool(case.get("success")) for case in cases),
        "totalCases": len(cases),
        "differentialMatches": sum(bool(case.get("differentialMatch")) for case in cases),
        "expectedMatches": sum(bool(case.get("expectedMatch")) for case in cases),
    }

    probe_gate: Optional[bool] = None
    if runtime is not None:
        probe_gate = all(
            bool(probe.get("handled")) and bool(probe.get("specific"))
            for probe in probes
        )
    input_validation = {
        "passed": probe_gate,
        "passedProbes": sum(
            bool(probe.get("handled")) and bool(probe.get("specific"))
            for probe in probes
        ),
        "totalProbes": len(probes),
    }

    provisional: Optional[float] = None
    aqs: Optional[float] = None
    if d2 is not None and d3_score is not None:
        provisional = _rounded(0.4 * d2["score"] + 0.6 * d3_score)
        if d1["score"] is not None:
            aqs = _rounded(d1["score"] * provisional)
    publishable = bool(
        d1["score"] == 1.0 and d3_score == 1.0 and probe_gate is True
    )
    quality_gate = bool(publishable and d2 is not None and d2["score"] >= 0.7)
    return {
        "qualityVersion": QUALITY_VERSION,
        "profile": "amq-compatible-strict-oracle",
        "d1Availability": d1,
        "d2Usability": d2,
        "d3Utility": d3,
        "inputValidationGate": input_validation,
        "aqs": aqs,
        "provisionalQualityWithoutBuild": provisional,
        "publishable": publishable,
        "qualityGatePassed": quality_gate,
        "failureCategory": _classify_failure(
            verification, d1, d3_score, probe_gate
        ),
        "notes": [
            "AQS is null until Docker build and MCP health are both evaluated."
        ] if aqs is None else [],
    }


def aggregate_quality(reports: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    items = list(reports)

    def known(path: str) -> List[float]:
        values: List[float] = []
        for item in items:
            value: Any = item
            for key in path.split("."):
                value = value.get(key) if isinstance(value, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append(float(value))
        return values

    def average(path: str) -> Optional[float]:
        values = known(path)
        return _rounded(mean(values)) if values else None

    return {
        "qualityVersion": QUALITY_VERSION,
        "samples": len(items),
        "meanAqs": average("aqs"),
        "meanUsability": average("d2Usability.score"),
        "utilityPassRate": average("d3Utility.score"),
        "publishableRate": _ratio(bool(item.get("publishable")) for item in items),
        "qualityGatePassRate": _ratio(bool(item.get("qualityGatePassed")) for item in items),
        "inputValidationGatePassRate": _ratio(
            item.get("inputValidationGate", {}).get("passed") is True
            for item in items
        ),
        "failureCategories": {
            category: sum(item.get("failureCategory") == category for item in items)
            for category in sorted(
                {item.get("failureCategory") for item in items if item.get("failureCategory")}
            )
        },
    }
