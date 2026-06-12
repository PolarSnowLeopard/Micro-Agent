"""Tests for sanitize module — markdown injection prevention."""
import sys
sys.path.insert(0, '/home/lyx/workspace/fdueblab/Micro-Agent/trace_evidence')

from sanitize import sanitize_md_cell, sanitize_identifier, validate_tool_name


def test_pipe_escape():
    """Pipe chars must be escaped to prevent table breakout."""
    result = sanitize_md_cell("col1 | col2 | col3")
    # Unescaped pipes should not exist (escaped = \\|)
    assert result.replace('\\|', '') == "col1  col2  col3"


def test_html_stripped():
    """HTML tags must be removed."""
    result = sanitize_md_cell('<script>alert("xss")</script>')
    assert "<script>" not in result
    assert "alert" in result  # text content preserved


def test_markdown_link_neutralized():
    """Markdown links must be neutralized."""
    result = sanitize_md_cell("[click me](http://evil.com)")
    # Escaped brackets prevent link rendering
    assert result.startswith("\\[") or "[click" not in result


def test_newlines_removed():
    """Newlines break table rows."""
    result = sanitize_md_cell("line1\nline2\rline3")
    assert "\n" not in result
    assert "\r" not in result


def test_truncation():
    """Long strings get truncated."""
    long_str = "A" * 500
    result = sanitize_md_cell(long_str, max_len=50)
    assert len(result) <= 55  # allow for "..." suffix


def test_identifier_safe():
    """Identifiers only allow safe chars."""
    result = sanitize_identifier("mcp-demo__evil<script>tool")
    assert "<" not in result
    assert ">" not in result
    assert "mcp" in result


def test_validate_tool_name_normal():
    """Normal tool names pass validation."""
    valid, cleaned = validate_tool_name("mcp-demo-openfda__search_drugs")
    assert valid is True
    assert cleaned == "mcp-demo-openfda__search_drugs"


def test_validate_tool_name_malicious():
    """Malicious tool names are flagged and cleaned."""
    valid, cleaned = validate_tool_name("tool<img src=x onerror=alert(1)>")
    assert valid is False
    assert "<" not in cleaned


def test_validate_tool_name_empty():
    """Empty tool names are invalid."""
    valid, cleaned = validate_tool_name("")
    assert valid is False


def test_sanitize_md_cell_backtick():
    """Backticks that could create code injection."""
    result = sanitize_md_cell("normal `code` and ```block```")
    # Should not create fenced code blocks
    assert "```" not in result


if __name__ == "__main__":
    tests = [f for f in dir() if f.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            globals()[t]()
            passed += 1
            print(f"  PASS: {t}")
        except AssertionError as e:
            print(f"  FAIL: {t} — {e}")
        except Exception as e:
            print(f"  ERROR: {t} — {e}")
    print(f"\n{passed}/{len(tests)} security tests passed")
