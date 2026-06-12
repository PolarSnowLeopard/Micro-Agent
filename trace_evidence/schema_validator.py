"""Schema Validator for trace_evidence pipeline outputs.

Validates evidence_card.json and checker_report.json against their
JSON Schema definitions. Uses jsonschema if available, falls back to
a lightweight built-in validator for environments without the package.
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

SCHEMA_DIR = Path(__file__).parent / "schemas"

EVIDENCE_CARD_SCHEMA_PATH = SCHEMA_DIR / "evidence_card_schema.json"
CHECKER_REPORT_SCHEMA_PATH = SCHEMA_DIR / "checker_report_schema.json"


@dataclass
class ValidationResult:
    """Result of a schema validation run."""
    valid: bool
    errors: list[str] = field(default_factory=list)
    schema_id: str = ""
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        status = "VALID" if self.valid else "INVALID"
        msg = f"[{status}] schema={self.schema_id}, errors={len(self.errors)}"
        if self.errors:
            msg += "\n  " + "\n  ".join(self.errors[:10])
            if len(self.errors) > 10:
                msg += f"\n  ... and {len(self.errors) - 10} more"
        return msg


def _load_schema(path: Path) -> dict:
    """Load a JSON Schema file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _try_jsonschema_validate(instance: dict, schema: dict) -> ValidationResult:
    """Validate using jsonschema library (full Draft 2020-12 support)."""
    import jsonschema
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(instance))
    error_msgs = []
    for e in errors:
        path = ".".join(str(p) for p in e.absolute_path) or "(root)"
        error_msgs.append(f"{path}: {e.message}")

    return ValidationResult(
        valid=len(errors) == 0,
        errors=error_msgs,
        schema_id=schema.get("$id", "unknown"),
    )


def _builtin_validate(instance: dict, schema: dict) -> ValidationResult:
    """Lightweight validation without jsonschema package.

    Checks: required fields, type checks (basic), pattern (regex), enum.
    Not a full JSON Schema implementation but catches common issues.
    """
    import re

    errors: list[str] = []
    warnings: list[str] = []

    def _check_type(value: Any, type_spec: Any, path: str) -> bool:
        """Check if value matches type specification."""
        if isinstance(type_spec, list):
            return any(_check_type(value, t, path) for t in type_spec)
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "object": dict,
            "array": list,
            "null": type(None),
        }
        expected = type_map.get(type_spec)
        if expected is None:
            return True
        return isinstance(value, expected)

    def _validate_obj(obj: Any, schema_node: dict, path: str):
        if schema_node.get("type") and not _check_type(obj, schema_node["type"], path):
            errors.append(f"{path}: expected type {schema_node['type']}, got {type(obj).__name__}")
            return

        if isinstance(obj, dict) and schema_node.get("type") in ("object", ["object", "null"]):
            # Check required
            for req in schema_node.get("required", []):
                if req not in obj:
                    errors.append(f"{path}: missing required field '{req}'")

            # Check properties
            props = schema_node.get("properties", {})
            for key, val in obj.items():
                if key in props:
                    _validate_obj(val, props[key], f"{path}.{key}")
                elif schema_node.get("additionalProperties") is False:
                    errors.append(f"{path}: unexpected field '{key}'")

        elif isinstance(obj, list) and "items" in schema_node:
            for i, item in enumerate(obj):
                _validate_obj(item, schema_node["items"], f"{path}[{i}]")

        elif isinstance(obj, str):
            if "pattern" in schema_node:
                if not re.match(schema_node["pattern"], obj):
                    errors.append(f"{path}: '{obj[:50]}' does not match pattern {schema_node['pattern']}")
            if "enum" in schema_node and obj not in schema_node["enum"]:
                errors.append(f"{path}: '{obj}' not in enum {schema_node['enum']}")
            if "minLength" in schema_node and len(obj) < schema_node["minLength"]:
                errors.append(f"{path}: string too short (min {schema_node['minLength']})")

        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            if "minimum" in schema_node and obj < schema_node["minimum"]:
                errors.append(f"{path}: {obj} < minimum {schema_node['minimum']}")

    _validate_obj(instance, schema, "(root)")

    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        schema_id=schema.get("$id", "unknown"),
        warnings=warnings,
    )


def validate(instance: dict, schema: dict) -> ValidationResult:
    """Validate instance against schema. Uses jsonschema if available."""
    try:
        return _try_jsonschema_validate(instance, schema)
    except ImportError:
        return _builtin_validate(instance, schema)


def validate_evidence_card(card: dict) -> ValidationResult:
    """Validate an evidence card dict against the evidence_card schema."""
    schema = _load_schema(EVIDENCE_CARD_SCHEMA_PATH)
    return validate(card, schema)


def validate_checker_report(report: dict) -> ValidationResult:
    """Validate a checker report dict against the checker_report schema."""
    schema = _load_schema(CHECKER_REPORT_SCHEMA_PATH)
    return validate(report, schema)


def validate_file(json_path: str | Path, schema_type: str = "auto") -> ValidationResult:
    """Validate a JSON file against its schema.

    Args:
        json_path: Path to the JSON file.
        schema_type: "card", "report", or "auto" (infers from filename).
    """
    path = Path(json_path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if schema_type == "auto":
        name = path.name.lower()
        if "report" in name or "checker" in name:
            schema_type = "report"
        else:
            schema_type = "card"

    if schema_type == "report":
        return validate_checker_report(data)
    else:
        return validate_evidence_card(data)


# --- CLI ---
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python schema_validator.py <json_file> [card|report|auto]")
        sys.exit(1)

    file_path = sys.argv[1]
    schema_type = sys.argv[2] if len(sys.argv) > 2 else "auto"

    result = validate_file(file_path, schema_type)
    print(result.summary())
    sys.exit(0 if result.valid else 1)
