# IoEB production algorithm template v1

This package extends the legacy IoEB code template with deterministic service metadata and test oracles.

- Keep the entry file as `main.py` and the entry function as `main_process`.
- Use JSON-compatible type annotations and a Google-style docstring.
- Pin every third-party dependency with `==` in `requirements.txt`.
- Describe numeric, string, array, and nested object bounds in `parameterConstraints`.
- Add representative deterministic cases with exact expected outputs to `tests`.

Validate before submission:

```bash
mcp-packager validate . --strict
```
