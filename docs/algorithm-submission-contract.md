# IoEB Algorithm Submission Contracts

IoEB supports two explicit validation profiles. They share the existing `main_process` convention but serve different purposes.

## Legacy-compatible profile

This profile preserves the published platform template in `ioeb/docs/guide/code-template.md`:

- a standalone Python file may use any filename;
- a ZIP or directory uses `main.py`;
- the top-level callable is `main_process`;
- parameters and returns use type annotations and a Google-style docstring;
- initialization happens inside the callable rather than through mutable module state.

Run it without `--strict`:

```bash
mcp-packager validate ./algorithm.py
```

Missing dependency locks, missing machine-readable tests, and legacy ambiguous annotations such as `Optional[Dict]` are warnings in this profile. The package can be previewed, but `productionReady` remains false and a verification report without deterministic cases cannot pass the publish gate.

## Production v1 profile

Production v1 adds the information required to reproduce and verify a service:

```text
algorithm/
├── main.py
├── requirements.txt
└── ioeb_algorithm.json
```

Create a complete starting package with:

```bash
mcp-packager init --output ./algorithm
mcp-packager validate ./algorithm --strict
```

Production requirements are:

- `main.py` defines exactly one top-level `main_process`;
- every exposed type is explicitly JSON-compatible;
- every third-party dependency is pinned with `==`;
- `ioeb_algorithm.json` uses `ioeb.algorithm-package/v1`;
- `tests` contains at least one deterministic input and exact expected output;
- optional `parameterConstraints` supplies recursive numeric, string, array, or object bounds.

Example constraints:

```json
{
  "parameterConstraints": {
    "precision": {"minimum": 1, "maximum": 200},
    "name": {"minLength": 1, "maxLength": 100},
    "values": {"minItems": 1, "maxItems": 1000}
  }
}
```

Supported numeric constraints are `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, and `multipleOf`. Strings support `minLength`, `maxLength`, and `pattern`; arrays support `minItems` and `maxItems`. Enumerated choices should use `Literal[...]` in the Python signature.

Arrays also support `uniqueItems`, recursive `items`, and tuple-aligned `prefixItems`. Objects support `minProperties`, `maxProperties`, recursive `properties`, `required`, and boolean `additionalProperties`. Nested `description` values are exposed through MCP `tools/list`.

For example, a `dict[str, float]` parameter can be narrowed to a closed object without introducing a custom Python model:

```json
{
  "parameterConstraints": {
    "initial_conditions": {
      "properties": {
        "mass": {
          "description": "Mass in kilograms.",
          "exclusiveMinimum": 0.0
        },
        "velocity": {
          "description": "Initial velocity.",
          "minimum": -1000000.0,
          "maximum": 1000000.0
        }
      },
      "required": ["mass", "velocity"],
      "additionalProperties": false
    }
  }
}
```

These fragments may only refine types already declared by the Python annotation. They cannot replace or contradict the annotation.

The validator rejects unknown parameters, incompatible constraints, conflicting bounds, invalid defaults, test arguments that violate the generated input schema, and expected values that violate the declared return schema before any image build begins.

## Compatibility and publication

Legacy compatibility is an ingestion path, not a production quality claim. A future backend may accept legacy submissions and return remediation guidance or generate a draft manifest from user-supplied examples, but it must not invent a functional oracle. Only a package that passes production validation, Docker verification, and the quality gate can be published.
