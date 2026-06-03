"""Tests for redact_secrets() — Pass #30 security hardening."""
import sys
sys.path.insert(0, "/home/lyx/workspace/fdueblab/Micro-Agent/trace_evidence")

from sanitize import redact_secrets


def test_redact_bearer_token():
    text = 'Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig'
    result = redact_secrets(text)
    assert "eyJhbG" not in result
    assert "[REDACTED" in result


def test_redact_api_key_header():
    text = 'x-api-key: sk-proj-abc123def456ghi789'
    result = redact_secrets(text)
    assert "sk-proj-abc123" not in result
    assert "[REDACTED" in result


def test_redact_password_in_url():
    text = 'connecting to postgres://admin:SuperSecret123@db.example.com:5432/mydb'
    result = redact_secrets(text)
    assert "SuperSecret123" not in result
    assert "[REDACTED" in result


def test_redact_aws_key():
    text = 'AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
    result = redact_secrets(text)
    assert "wJalrXUtnFEMI" not in result
    assert "[REDACTED" in result


def test_no_false_positive_normal_text():
    text = 'The planner decided to click the submit button at coordinates (100, 200)'
    result = redact_secrets(text)
    assert result == text  # No redaction needed


def test_none_and_empty():
    assert redact_secrets(None) == ""
    assert redact_secrets("") == ""
    assert redact_secrets(123) == ""
