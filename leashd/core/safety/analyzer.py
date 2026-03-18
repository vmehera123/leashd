"""Bash command parser and path classifier."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RiskLevel = Literal["low", "medium", "high", "critical"]


class CommandAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)

    original: str
    commands: list[str]
    has_pipe: bool = False
    has_chain: bool = False
    has_sudo: bool = False
    has_subshell: bool = False
    has_redirect: bool = False
    risk_factors: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "low"

    @property
    def is_compound(self) -> bool:
        return self.has_pipe or self.has_chain or self.has_subshell


class PathAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    operation: str
    is_credential: bool = False
    has_traversal: bool = False
    sensitivity: str = "normal"
    reason: str = ""


_CREDENTIAL_PATTERNS = [
    re.compile(r"\.env($|\.)"),
    re.compile(r"\.ssh/"),
    re.compile(r"\.aws/"),
    re.compile(r"\.gnupg/"),
    re.compile(r"\.key$"),
    re.compile(r"\.pem$"),
    re.compile(r"\.p12$"),
    re.compile(r"\.pfx$"),
    re.compile(r"id_rsa"),
    re.compile(r"id_ed25519"),
    re.compile(r"credentials"),
    re.compile(r"secrets?\."),
    re.compile(r"\.keystore$"),
    re.compile(r"token\.json$"),
]


_CD_PREFIX_RE = re.compile(r"^cd(\s+[^$`|<>&;]*)?\s*(&&|;|\|\|)\s*")


def strip_cd_prefix(command: str) -> str:
    """Strip leading ``cd <path> &&`` segments from a shell command.

    Loops to handle chained cds: ``cd /a && cd /b && ls`` → ``ls``.
    Paths containing dangerous characters (``$`|<>&;``) are NOT stripped
    so that ``cd$(rm -rf /) && ls`` passes through unchanged.
    A bare ``cd /path`` with no chain operator is returned as-is.
    """
    prev = None
    while command != prev:
        prev = command
        command = _CD_PREFIX_RE.sub("", command)
    return command


_SLEEP_PREFIX_RE = re.compile(r"^sleep(\s+[^$`|<>&;]*)?\s*(&&|;|\|\|)\s*")


def strip_sleep_prefix(command: str) -> str:
    """Strip leading ``sleep <duration> &&`` segments from a shell command.

    Loops to handle chained sleeps: ``sleep 1 && sleep 2 && npm test`` → ``npm test``.
    Arguments containing dangerous characters (``$`|<>&;``) are NOT stripped
    so that ``sleep$(rm -rf /) && ls`` passes through unchanged.
    A bare ``sleep 5`` with no chain operator is returned as-is.
    """
    prev = None
    while command != prev:
        prev = command
        command = _SLEEP_PREFIX_RE.sub("", command)
    return command


def strip_benign_prefixes(command: str) -> str:
    """Strip leading ``cd`` and ``sleep`` prefix segments.

    Handles arbitrary ordering: ``sleep 1 && cd /a && npm test`` → ``npm test``.
    """
    prev = None
    while command != prev:
        prev = command
        command = strip_cd_prefix(command)
        command = strip_sleep_prefix(command)
    return command


def analyze_bash(command: str) -> CommandAnalysis:
    """Analyze a bash command for structural features and risk factors."""
    risk_factors: list[str] = []

    has_pipe = "|" in command
    has_chain = "&&" in command or "||" in command or ";" in command
    has_subshell = "$(" in command or "`" in command
    has_redirect = any(op in command for op in [">", ">>", "<"])
    has_sudo = bool(re.search(r"\bsudo\b", command))

    if has_sudo:
        risk_factors.append("uses sudo")
    if has_subshell:
        risk_factors.append("contains subshell")
    if has_pipe and has_redirect:
        risk_factors.append("pipe with redirect")

    # Split on pipes and chains to get individual commands
    parts = re.split(r"\s*[|;]\s*|\s*&&\s*|\s*\|\|\s*", command)
    commands = [part.strip() for part in parts if part.strip()]

    # Check for dangerous patterns
    if re.search(r"\brm\s.*-.*r.*f|\brm\s+-rf", command):
        risk_factors.append("recursive force delete")
    if re.search(r"\bchmod\s+777\b", command):
        risk_factors.append("world-writable permissions")
    if re.search(r"\b(curl|wget)\b.*\|\s*\b(bash|sh|zsh)\b", command):
        risk_factors.append("remote code execution via pipe")
    if re.search(r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)\b", command, re.IGNORECASE):
        risk_factors.append("database destructive operation")

    if len(risk_factors) >= 2:
        risk_level: RiskLevel = "critical"
    elif risk_factors:
        risk_level = "high"
    elif has_pipe or has_chain or has_subshell:
        risk_level = "medium"
    else:
        risk_level = "low"

    return CommandAnalysis(
        original=command,
        commands=commands,
        has_pipe=has_pipe,
        has_chain=has_chain,
        has_sudo=has_sudo,
        has_subshell=has_subshell,
        has_redirect=has_redirect,
        risk_factors=risk_factors,
        risk_level=risk_level,
    )


def analyze_path(path: str, operation: str = "read") -> PathAnalysis:
    """Classify a file path for credential sensitivity and traversal risk."""
    has_traversal = ".." in path
    is_credential = False
    sensitivity = "normal"
    reason = ""

    if has_traversal:
        sensitivity = "high"
        reason = "Path contains traversal components"

    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(path):
            is_credential = True
            sensitivity = "critical"
            reason = f"Matches credential pattern: {pattern.pattern}"
            break

    if operation in ("write", "edit") and sensitivity == "normal":
        sensitivity = "elevated"

    return PathAnalysis(
        path=path,
        operation=operation,
        is_credential=is_credential,
        has_traversal=has_traversal,
        sensitivity=sensitivity,
        reason=reason,
    )
