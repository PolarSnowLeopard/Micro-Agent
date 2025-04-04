class ToolError(Exception):
    """Raised when a tool encounters an error."""

    def __init__(self, message):
        self.message = message


class MicroAgentError(Exception):
    """Base exception for all MicroAgent errors"""


class TokenLimitExceeded(MicroAgentError):
    """Exception raised when the token limit is exceeded"""
