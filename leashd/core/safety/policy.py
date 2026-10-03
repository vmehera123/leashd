"""YAML-driven policy engine — loads rules and classifies tool calls."""

import re
from collections.abc import Iterable
from enum import Enum
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

from leashd.core.safety.analyzer import (
    SQL_CLIENT_RE,
    RiskLevel,
    command_units,
    docker_exec_command,
    expand_literal_variables,
    inline_shell_script,
    keeps_to_its_streams,
    loopback_write_scope,
    mentions_credential_path,
    names_what_it_reads,
    network_read_scope,
    public_read_scope,
    quotes_credential_path,
    reads_without_writing,
    remote_shell_command,
    shell_match_texts,
    split_chain_segments,
    strip_benign_prefixes,
    unquoted_credential_path,
    written_heredoc_head,
)

logger = structlog.get_logger()

_AWK_RE = re.compile(r"^[gm]?awk\b")

_NETWORK_READ_RE = re.compile(r"^(?:curl|wget)\b")

_PUBLIC_READ_MARKER_RE = re.compile(r"^(?:curl|wget)\+(?:public|loopback)\b")

_REMOTE_READ_RULES = frozenset(
    {
        "read-only-bash",
        "docker-readonly",
        "sql-read",
        "credential-metadata",
        "loopback-read",
        "public-read",
    }
)

_AWK_UNSAFE_PROGRAM_RE = re.compile(
    r"\bsystem\s*\(|\bgetline\b|\bprintf?\b[^;{}()]*[>|]"
)

_MCP_PREFIX_RE = re.compile(r"^mcp__[a-zA-Z0-9_-]+__")


def normalize_tool_name(tool_name: str) -> str:
    """Strip the ``mcp__<server>__`` prefix from an MCP tool name.

    Claude Code names MCP tools ``mcp__playwright__browser_navigate``; the bare
    ``browser_navigate`` is what the sandbox, auto-approve keys and older policy
    rules use. A policy rule may name either form — see
    :meth:`PolicyEngine.classify`.
    """
    return _MCP_PREFIX_RE.sub("", tool_name)


class PolicyDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    action: PolicyDecision
    tools: list[str] = Field(default_factory=list)
    command_patterns: list[re.Pattern[str]] = Field(default_factory=list)
    path_patterns: list[re.Pattern[str]] = Field(default_factory=list)
    reason: str | None = None
    description: str | None = None
    risk_level: RiskLevel = "medium"


_TOOL_SEARCH_RULE = PolicyRule(
    name="tool-search",
    action=PolicyDecision.ALLOW,
    tools=["ToolSearch"],
    reason="Loads deferred tool schemas; every tool it loads is gated when called",
    risk_level="low",
)

_TRUSTED_SSH_READ_RULE = PolicyRule(
    name="trusted-ssh-read",
    action=PolicyDecision.ALLOW,
    tools=["Bash"],
    description="Read-only command on a trusted SSH host",
    risk_level="low",
)


class Classification(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    tool_name: str
    tool_input: dict[str, Any]
    risk_level: RiskLevel = "medium"
    description: str = ""
    deny_reason: str | None = None
    matched_rule: PolicyRule | None = None
    matched_command: str | None = None


class PolicyEngine:
    def __init__(
        self,
        policy_paths: list[Path] | None = None,
        *,
        trusted_ssh_hosts: Iterable[str] = (),
        guarded_loopback_ports: Iterable[int] = (),
    ) -> None:
        self.rules: list[PolicyRule] = []
        self._trusted_ssh_hosts = frozenset(trusted_ssh_hosts)
        self._guarded_loopback_ports = frozenset(guarded_loopback_ports)
        self.settings: dict[str, Any] = {
            "default_action": "require_approval",
            # NOTE: not consumed — the effective approval/interaction window is
            # LeashdConfig.approval_timeout_seconds / interaction_timeout_seconds
            # (None = no expiry). Kept for back-compat of policy YAML files;
            # wiring this back is a separate, out-of-scope cleanup.
            "approval_timeout_seconds": 300,
        }
        if policy_paths:
            for path in policy_paths:
                self._load_policy(path)
            logger.info(
                "policy_engine_initialized",
                total_rules=len(self.rules),
                policy_count=len(policy_paths),
                default_action=self.settings.get("default_action"),
            )

    def _load_policy(self, path: Path) -> None:
        with open(path) as f:
            data = yaml.safe_load(f)

        if not data:
            return

        if "settings" in data:
            self.settings.update(data["settings"])
            if "default_action" in data["settings"]:
                PolicyDecision(self.settings["default_action"])  # fail-fast

        rules_data = data.get("rules", [])
        for rule_data in rules_data:
            self.rules.append(self._parse_rule(rule_data))
        logger.debug("policy_loaded", path=str(path), rule_count=len(rules_data))

    def _parse_rule(self, data: dict[str, Any]) -> PolicyRule:
        # Normalize tools: accept both "tool" (single) and "tools" (list)
        tools: list[str] = []
        if "tools" in data:
            tools = (
                data["tools"] if isinstance(data["tools"], list) else [data["tools"]]
            )
        elif "tool" in data:
            tools = [data["tool"]]

        command_patterns = [re.compile(p) for p in data.get("command_patterns", [])]
        path_patterns = [re.compile(p) for p in data.get("path_patterns", [])]

        action_str = data["action"]
        action = PolicyDecision(action_str)

        return PolicyRule(
            name=data["name"],
            action=action,
            tools=tools,
            command_patterns=command_patterns,
            path_patterns=path_patterns,
            reason=data.get("reason"),
            description=data.get("description"),
            risk_level=data.get("risk_level", "medium"),
        )

    def set_trusted_ssh_hosts(self, hosts: Iterable[str]) -> None:
        self._trusted_ssh_hosts = frozenset(hosts)

    def classify(self, tool_name: str, tool_input: dict[str, Any]) -> Classification:
        """Classify one tool call: the rules first, then a trusted SSH read."""
        by_rules = self._classify_by_rules(tool_name, tool_input)
        if tool_name != "Bash":
            return by_rules
        return self._trusted_remote_read(tool_input, by_rules) or by_rules

    def _trusted_remote_read(
        self, tool_input: dict[str, Any], by_rules: Classification
    ) -> Classification | None:
        """Allow a read-only command on a host the user marked as trusted.

        ``ssh build-box 'docker compose ps'`` asked every time, and the answer
        was always yes: most remote commands are listings, logs and ``select``
        queries. The remote command is judged by
        the rules a local one is, and runs unasked only when every part of it
        is a read that writes nothing.

        The override is narrow. The rules must have gated the call for being
        an ``ssh`` login and nothing else, so a credential path on this side of
        the connection, or any deny, keeps its verdict. No native permission
        mode reviews a remote command after this, which is why the read has to
        pass :func:`reads_without_writing` and name no credential path, quoted
        or not, as well as match a read rule.
        """
        rule = by_rules.matched_rule
        if (
            not self._trusted_ssh_hosts
            or rule is None
            or rule.action != PolicyDecision.REQUIRE_APPROVAL
        ):
            return None
        command = str(tool_input.get("command", ""))
        session = remote_shell_command(command)
        if session is None:
            return None
        destination, remote = session
        if destination not in self._trusted_ssh_hosts:
            return None
        login = self._classify_by_rules("Bash", {"command": f"ssh {destination}"})
        if login.matched_rule is not rule or mentions_credential_path(command):
            return None
        if quotes_credential_path(remote) or not self._reads_only(remote):
            return None
        return Classification(
            category=_TRUSTED_SSH_READ_RULE.name,
            tool_name="Bash",
            tool_input=tool_input,
            risk_level=_TRUSTED_SSH_READ_RULE.risk_level,
            description=f"{_TRUSTED_SSH_READ_RULE.description}: {destination}",
            matched_rule=_TRUSTED_SSH_READ_RULE,
        )

    def _reads_only(self, command: str) -> bool:
        """Whether every command in *command* is a read, however it is spelled.

        A loop over literal values is judged once per value, and the script
        of ``sh -c '…'`` by the commands inside it.
        """
        return all(
            self._reads_only_as_written(variant)
            for variant in expand_literal_variables(command)
        )

    def _reads_only_as_written(self, command: str) -> bool:
        units = command_units(command)
        for text, kind in units:
            rule = self._classify_by_rules("Bash", {"command": text}).matched_rule
            if kind == "pipeline":
                if rule is not None and rule.action == PolicyDecision.DENY:
                    return False
                continue
            script = inline_shell_script(text) if keeps_to_its_streams(text) else None
            if script is not None:
                if not self._reads_only(script):
                    return False
                continue
            if (
                rule is None
                or rule.action != PolicyDecision.ALLOW
                or rule.name not in _REMOTE_READ_RULES
                or not reads_without_writing(text)
                or not names_what_it_reads(text)
                or (
                    rule.name != "credential-metadata"
                    and unquoted_credential_path(text)
                )
            ):
                return False
        return bool(units)

    def _classify_by_rules(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> Classification:
        """Classify one tool call against the rules, first match wins.

        A rule's ``tools`` entry matches an MCP call by its full Claude Code
        name (``mcp__leadline__get_opportunity``), which scopes it to that
        server, or by its bare name (``get_opportunity``), which matches that
        tool on any server.

        ``ToolSearch`` is allowed when no rule names it. Claude Code defers
        ``WebFetch``, ``WebSearch`` and every MCP tool until ToolSearch loads
        their schemas, so refusing it silently refused all of them, including
        the ones the policy allows.
        """
        command_texts: tuple[list[str], list[str], list[str]] = ([], [], [])
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            allow_texts, gate_texts = self._bash_match_texts(command)
            head = written_heredoc_head(command)
            ask_texts = self._bash_match_texts(head)[1] if head else gate_texts
            command_texts = (allow_texts, gate_texts, ask_texts)
        names = {tool_name, normalize_tool_name(tool_name)}
        for rule in (*self.rules, _TOOL_SEARCH_RULE):
            if self._rule_matches(rule, names, tool_name, tool_input, command_texts):
                return Classification(
                    category=rule.name,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    risk_level=rule.risk_level,
                    description=rule.description or rule.reason or rule.name,
                    deny_reason=rule.reason,
                    matched_rule=rule,
                )

        return Classification(
            category="unmatched",
            tool_name=tool_name,
            tool_input=tool_input,
            risk_level="medium",
            description=f"Unmatched tool call: {tool_name}",
        )

    def evaluate(self, classification: Classification) -> PolicyDecision:
        if classification.matched_rule:
            return classification.matched_rule.action

        default = self.settings.get("default_action", "require_approval")
        return PolicyDecision(default)

    def _bash_match_texts(self, command: str) -> tuple[list[str], list[str]]:
        """The texts a Bash rule's patterns are matched against.

        The normalized command — :func:`strip_benign_prefixes` peels
        ``cd``/``sleep`` prefixes, wrappers and redirections so an anchored
        pattern still recognizes ``agent-browser tab 2>&1``. Stripping the
        redirection also removes where the command *writes*, which hid
        ``echo … >> ~/.ssh/authorized_keys`` from every rule in the file and
        left it cleared by the read-only ``echo`` allow. So a redirecting
        command contributes its original text as well; a rule only has to
        match one of the candidates.

        Normalizing is linear in the command length and identical for every
        rule, so it is done once per call rather than once per rule — a 100K
        character command was paying it a dozen times over.

        Two commands are judged by what they amount to rather than how they
        are spelled. A ``curl``/``wget`` read is matched in the canonical form
        :func:`network_read_scope` gives it (``curl 127.0.0.1``), because the
        URL a dev-server probe needs quoted is blanked out of the skeleton.
        ``docker exec <container> <cmd>`` is allowed by what ``<cmd>`` is.
        """
        # Local import — browser_tools imports from safety modules, so
        # defer this to call time to keep the module graph acyclic.
        from leashd.plugins.builtin.browser_tools import strip_agent_browser_flags

        raw = strip_agent_browser_flags(command)
        normalized = strip_agent_browser_flags(strip_benign_prefixes(command))
        candidates = shell_match_texts(normalized)
        skeleton, payloads = candidates[0], candidates[1:]
        inner = docker_exec_command(normalized)
        public: str | None = None
        if inner is not None:
            allow_texts = self._bash_match_texts(inner)[0]
        elif payloads and SQL_CLIENT_RE.match(skeleton):
            allow_texts = payloads
        elif _AWK_RE.match(skeleton) and any(
            _AWK_UNSAFE_PROGRAM_RE.search(payload) for payload in payloads
        ):
            allow_texts = []
        elif _NETWORK_READ_RE.match(skeleton):
            public = public_read_scope(normalized)
            read = None if public else network_read_scope(normalized)
            if public is None and read is None:
                public = loopback_write_scope(normalized, self._guarded_loopback_ports)
            allow_texts = [public or read or skeleton]
        else:
            allow_texts = [skeleton]
        if public is None:
            allow_texts = [
                text for text in allow_texts if not _PUBLIC_READ_MARKER_RE.match(text)
            ]
        gate_texts = list(candidates)
        if raw != normalized and (">" in raw or "<" in raw):
            gate_texts += shell_match_texts(raw)
        return allow_texts, gate_texts

    def _rule_matches(
        self,
        rule: PolicyRule,
        names: set[str],
        tool_name: str,
        tool_input: dict[str, Any],
        command_texts: tuple[list[str], list[str], list[str]],
    ) -> bool:
        """Whether *rule* covers this call.

        A deny rule reads everything the command carries. An approval rule
        skips the body of a heredoc that is only being written to a file.
        """
        if names.isdisjoint(rule.tools):
            return False

        if rule.command_patterns:
            if tool_name != "Bash":
                return False
            allow_texts, gate_texts, ask_texts = command_texts
            if rule.action == PolicyDecision.ALLOW:
                if not allow_texts or not all(
                    any(p.search(text) for p in rule.command_patterns)
                    for text in allow_texts
                ):
                    return False
            elif not any(
                p.search(text)
                for text in (
                    gate_texts if rule.action == PolicyDecision.DENY else ask_texts
                )
                for p in rule.command_patterns
            ):
                return False

        if rule.path_patterns:
            path = tool_input.get("file_path") or tool_input.get("path") or ""
            if not any(p.search(path) for p in rule.path_patterns):
                return False

        return True

    @staticmethod
    def _split_chain_segments(command: str) -> list[str]:
        return split_chain_segments(command)

    def classify_compound(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> Classification:
        """Classify a tool call with compound command awareness.

        For Bash commands containing chain operators (``&&``, ``||``, ``;``),
        each chained segment is evaluated independently.  Pipe sequences
        within a segment are kept intact so deny patterns like
        ``curl.*\\|.*bash`` can still match.

        Verdicts combine strictly: any deny segment denies the whole command,
        then any approval segment gates it, and only a command whose every
        segment is positively allowed is allowed. Reporting the first
        segment's classification for that last case made the verdict depend
        on word order — ``echo hi; python3 /tmp/x.py`` was allowed while the
        same pair reversed asked — so a leading ``echo``/``ls``/``cat`` was
        enough to launder any unmatched command past ``default_action``.

        The reported classification carries ``matched_command``: the segment
        that decided the verdict, which the approval prompt names.

        For non-compound commands and non-Bash tools, behaviour is identical
        to :meth:`classify`.
        """
        if tool_name != "Bash":
            return self.classify(tool_name, tool_input)

        command = tool_input.get("command", "")
        whole = command.strip()
        units = command_units(command)

        if not units:
            return self.classify(tool_name, tool_input)

        if len(units) == 1:
            text = units[0][0]
            if text == whole:
                return self.classify(tool_name, tool_input)
            only = self.classify(tool_name, {**tool_input, "command": text})
            return only.model_copy(
                update={"tool_input": tool_input, "matched_command": text}
            )

        classified = [
            (self.classify(tool_name, {**tool_input, "command": text}), text, kind)
            for text, kind in units
        ]

        for action, verdict in (
            (PolicyDecision.DENY, "denied"),
            (PolicyDecision.REQUIRE_APPROVAL, "requires approval"),
        ):
            for unit, text, kind in classified:
                if kind == "pipeline" and action != PolicyDecision.DENY:
                    continue
                if not unit.matched_rule or unit.matched_rule.action != action:
                    continue
                if text == whole:
                    return unit.model_copy(update={"tool_input": tool_input})
                return Classification(
                    category=unit.category,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    risk_level=unit.risk_level,
                    description=f"Compound command {verdict}: {unit.description}",
                    deny_reason=unit.deny_reason,
                    matched_rule=unit.matched_rule,
                    matched_command=text,
                )

        decisive = [
            (unit, text) for unit, text, kind in classified if kind != "pipeline"
        ]
        unit, text = next(
            ((unit, text) for unit, text in decisive if unit.matched_rule is None),
            decisive[0],
        )
        return Classification(
            category=unit.category,
            tool_name=tool_name,
            tool_input=tool_input,
            risk_level=unit.risk_level,
            description=unit.description,
            deny_reason=unit.deny_reason,
            matched_rule=unit.matched_rule,
            matched_command=text,
        )
