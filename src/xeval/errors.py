"""Domain-specific exceptions with safe, actionable messages."""


class XEvalError(Exception):
    """Base exception for expected harness failures."""


class ConfigurationError(XEvalError):
    """Configuration or probe validation failed."""


class EndpointError(XEvalError):
    """The black-box endpoint could not produce a usable response."""


class JudgeError(XEvalError):
    """The judge returned malformed or unusable output."""
