"""Input sanitization for trace evidence pipeline.

Protects markdown renderers from injection via untrusted trace data
(tool_name, service_id, detail text, etc.).
"""

from __future__ import annotations

import re


def sanitize_md_cell(value: str, max_len: int = 120) -> str:
    """Sanitize a string for safe inclusion in a markdown table cell.

    Escapes characters that could:
    - Break table structure (|, newlines)
    - Inject markdown formatting ([links](url), **bold**, `code`)
    - Inject HTML (<script>, <img onerror>)

    Args:
        value: Untrusted string from trace data
        max_len: Maximum output length (truncates with "…")

    Returns:
        Sanitized string safe for markdown table cells
    """
    if not isinstance(value, str):
        value = str(value)

    value = value.replace("\x00", "")

    # Strip HTML tags entirely (prevents <script>, <img onerror=...>, etc.)
    value = re.sub(r'<[^>]*>', '', value)

    # Escape markdown table breakers
    value = value.replace('|', '\\|')

    # Escape newlines (break table rows)
    value = value.replace('\n', ' ').replace('\r', ' ')

    # Escape markdown link syntax: [text](url) → \[text\]\(url)
    value = value.replace('[', '\\[').replace(']', '\\]')
    # Also break ]( pattern that forms link targets (even after bracket escaping,
    # the literal substring ](xxx) can still trigger some renderers)
    value = value.replace('](', ']\\(')

    # Neutralize fenced code blocks (``` could break out of table context)
    value = value.replace('```', '\'\'\'')

    # Escape markdown emphasis that could break rendering
    # Only escape sequences that start formatting (**, __, ~~)
    value = re.sub(r'(\*\*|__|~~)', r'\\\1', value)

    # Truncate
    if len(value) > max_len:
        value = value[:max_len - 1] + '…'

    return value


def sanitize_identifier(value: str, max_len: int = 80) -> str:
    """Sanitize a value expected to be an identifier (tool_name, service_id).

    More restrictive than sanitize_md_cell — only allows alphanumeric,
    hyphens, underscores, dots, colons, and forward slashes.

    Args:
        value: Untrusted identifier from trace data
        max_len: Maximum output length

    Returns:
        Sanitized identifier (non-matching chars replaced with _)
    """
    if not isinstance(value, str):
        value = str(value)

    # Allow common identifier chars only
    sanitized = re.sub(r'[^a-zA-Z0-9_\-\.:/]', '_', value)

    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len - 1] + '…'

    return sanitized


def sanitize_md_block(value: str, max_len: int = 2000) -> str:
    """Sanitize a multiline string for safe inclusion in markdown body text.

    Suitable for blockquotes, list items, and paragraphs where the content
    comes from untrusted trace data (e.g., planner reasoning, verification reason).

    Unlike sanitize_md_cell, this preserves newlines but neutralizes:
    - HTML tags (stripped)
    - Headings (# at line start → escaped)
    - Fenced code blocks (``` → ''')
    - Link/image syntax ([text](url))
    - Emphasis sequences that could break rendering

    Args:
        value: Untrusted multiline string from trace data
        max_len: Maximum output length (truncates with "…[truncated]")

    Returns:
        Sanitized string safe for markdown body content
    """
    if not isinstance(value, str):
        value = str(value)

    # Strip HTML tags entirely
    value = re.sub(r'<[^>]*>', '', value)

    # Neutralize fenced code blocks
    value = value.replace('```', "'''")

    # Escape heading syntax at line starts
    value = re.sub(r'^(#{1,6})\s', r'\\\1 ', value, flags=re.MULTILINE)

    # Escape markdown link/image syntax: [text](url) and ![alt](url)
    value = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', value)

    # Escape remaining square brackets (potential link injection)
    value = value.replace('[', '\\[').replace(']', '\\]')

    # Neutralize emphasis sequences (**bold**, __underline__, ~~strike~~)
    value = re.sub(r'(\*\*|__|~~)', r'\\\1', value)

    # Neutralize horizontal rules (---, ***, ___) at line start
    value = re.sub(r'^([-*_]{3,})\s*$', r'\\\1', value, flags=re.MULTILINE)

    # Truncate
    if len(value) > max_len:
        value = value[:max_len - 12] + "\n…[truncated]"

    return value


def sanitize_md_inline(value: str, max_len: int = 200) -> str:
    """Sanitize a single-line string for safe inline markdown use.

    For use in bullet points, bold labels, etc. Strips newlines
    and applies same escaping as sanitize_md_block.

    Args:
        value: Untrusted inline string
        max_len: Maximum length

    Returns:
        Sanitized single-line string
    """
    if not isinstance(value, str):
        value = str(value)

    # Flatten to single line
    value = value.replace('\n', ' ').replace('\r', ' ')

    # Apply block-level sanitization (handles HTML, links, emphasis, etc.)
    value = sanitize_md_block(value, max_len=max_len + 50)

    # Truncate to final length
    if len(value) > max_len:
        value = value[:max_len - 1] + "…"

    return value


def validate_tool_name(name: str) -> tuple[bool, str]:
    """Validate a tool_name from trace data.

    Returns:
        (is_valid, cleaned_name) — cleaned_name is safe even if invalid
    """
    if not name or not isinstance(name, str):
        return False, "(empty)"

    # Tool names should match: prefix__tool_name or prefix-service__tool
    # Allow: alphanumeric, hyphens, underscores, dots, colons
    is_valid = bool(re.match(r'^[a-zA-Z0-9_\-\.:]+$', name)) and len(name) <= 200

    cleaned = sanitize_identifier(name)
    return is_valid, cleaned


# ---------------------------------------------------------------------------
# Secret / PII redaction for evidence artifacts
# ---------------------------------------------------------------------------

# Patterns that indicate secrets in text content (log lines, planner thoughts, etc.)
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # API keys: sk-..., key-..., AKIA..., etc.
    (re.compile(r'\b(sk-[a-zA-Z0-9_-]{20,})\b'), '[REDACTED_API_KEY]'),
    (re.compile(r'\b(AKIA[A-Z0-9]{16})\b'), '[REDACTED_AWS_KEY]'),
    (re.compile(r'\b(key-[a-zA-Z0-9_-]{20,})\b'), '[REDACTED_API_KEY]'),
    # Bearer tokens
    (re.compile(r'([Bb]earer\s+)[a-zA-Z0-9\-._~+/]+=*', re.ASCII), r'\1[REDACTED_TOKEN]'),
    # Generic "token": "..." or token=... patterns (also x-api-key header style)
    (re.compile(r'(["\']?(?:token|api_key|api[-_]key|apikey|secret|password|passwd|api[-_]?secret|access[-_]?key|x-api-key)["\']\s*[:=]\s*["\']?)([^"\'&\s]{8,})', re.IGNORECASE),
     r'\1[REDACTED]'),
    # Header-style: "X-Api-Key: value" or "Authorization: value"
    (re.compile(r'((?:x-api-key|authorization|x-auth-token)\s*:\s*)(\S{8,})', re.IGNORECASE),
     r'\1[REDACTED]'),
    # Connection strings with passwords: ://user:password@host
    (re.compile(r'(://[^:]+:)([^@]{4,})(@)'), r'\1[REDACTED]\3'),
    # Environment variable style: SECRET_KEY=value or AWS_SECRET_ACCESS_KEY=value
    (re.compile(r'((?:SECRET|KEY|TOKEN|PASSWORD|PASSWD|ACCESS_KEY|API_KEY|AUTH)[A-Z_]*\s*=\s*)(\S{8,})', re.IGNORECASE),
     r'\1[REDACTED]'),
    # Hex/base64 strings that look like secrets (40+ chars of hex or 32+ base64)
    (re.compile(r'\b([a-f0-9]{40,})\b'), '[REDACTED_HEX]'),
    # JWT tokens (three base64 segments separated by dots)
    (re.compile(r'\beyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}\b'), '[REDACTED_JWT]'),
]


def redact_secrets(text: str) -> str:
    """Redact potential secrets, tokens, and credentials from free-text content.

    Applied to evidence artifacts that embed raw trace/log content (planner
    thought previews, verification raw_text) before they are persisted to disk.

    This is a best-effort defense-in-depth measure. It catches common patterns
    but cannot guarantee all secrets are removed.

    Args:
        text: Raw text that may contain secrets.

    Returns:
        Text with recognized secret patterns replaced by [REDACTED_*] markers.
    """
    if not text or not isinstance(text, str):
        return ""

    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
