"""Shared exception types for leashd."""


class LeashdError(Exception):
    """Base exception for all leashd errors."""


class ConfigError(LeashdError):
    """Configuration is invalid or missing."""


class AgentError(LeashdError):
    """Error from the AI agent backend."""


class SafetyError(LeashdError):
    """A safety policy violation occurred."""


class ApprovalTimeoutError(LeashdError):
    """User did not respond to an approval request in time."""


class SessionError(LeashdError):
    """Session management error."""


class StorageError(LeashdError):
    """Persistent storage error."""


class PluginError(LeashdError):
    """Plugin lifecycle error."""


class InteractionTimeoutError(LeashdError):
    """User did not respond to an interaction prompt in time."""


class ConnectorError(LeashdError):
    """Connector failed after exhausting retries."""


class DaemonError(LeashdError):
    """Daemon lifecycle error (start/stop)."""
