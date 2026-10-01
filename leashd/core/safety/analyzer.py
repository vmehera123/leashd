"""Bash command parser and path classifier."""

import re
import shlex
from collections.abc import Iterator
from typing import Literal
from urllib.parse import urlsplit

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
    re.compile(r"\.env($|\.(?!(?:example|sample|template|dist)(?![\w.-])))"),
    re.compile(r"\.ssh/"),
    re.compile(r"\.aws/"),
    re.compile(r"\.gnupg/"),
    re.compile(r"\.key$"),
    re.compile(r"\.pem$"),
    re.compile(r"\.p12$"),
    re.compile(r"\.pfx$"),
    re.compile(r"id_rsa"),
    re.compile(r"id_ed25519"),
    re.compile(r"id_ecdsa"),
    re.compile(r"id_dsa"),
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


_EXECUTOR_FLAGS = frozenset({"-c", "-e", "--command", "--eval", "eval"})

_EXECUTOR_COMMANDS = frozenset(
    {"sqlite3", "sqlite", "duckdb", "sed", "awk", "gawk", "mawk"}
)

_COMMAND_POSITION_RE = re.compile(r"\|\||&&|[|;&(]")


def shell_match_texts(command: str) -> list[str]:
    """The text a rule pattern should be matched against, quoting respected.

    A shell command carries two kinds of text: what the shell executes, and
    string literals it merely passes along. Matching patterns against the raw
    command conflates them, so ``grep "rm -rf" tests/`` and a heredoc writing
    a test *about* the deny floor read as destructive commands. Every one of
    those is a search or an edit that runs nothing.

    Returns the command with quoted contents blanked out — the skeleton the
    shell acts on — plus the contents of any quoted run handed to something
    that executes it (``bash -c``, ``eval``, ``psql -c``), which is real
    executable text wearing quotes.

    A flag is not the only way to hand over a program. ``sqlite3 db "DROP
    TABLE users"`` passes its statement positionally, so the deny floor read
    it as an inert string while the identical ``psql -c`` form was denied;
    ``sed``/``awk`` hand over a language with ``s///e`` and ``system()`` in
    it. A quoted run is therefore also a payload when the command in command
    position is one of those interpreters. Anchored allow rules
    (``^sed\\s+…``) still see only the skeleton, so the flags a read rule
    checks stay decisive.
    """
    skeleton: list[str] = []
    payloads: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in command:
        if escaped:
            (current if quote else skeleton).append(ch)
            escaped = False
            continue
        if ch == "\\":
            (current if quote else skeleton).append(ch)
            escaped = True
            continue
        if quote is None and ch in "'\"":
            quote = ch
            skeleton.append(ch)
            continue
        if ch == quote:
            body = "".join(current)
            current = []
            quote = None
            skeleton.append(ch)
            if _preceded_by_executor("".join(skeleton)):
                payloads.append(body)
            continue
        (current if quote else skeleton).append(ch)
    if current:
        # Unbalanced quote: treat the tail as ordinary shell text rather than
        # letting an unclosed quote hide the rest of the command from a rule.
        skeleton.append("".join(current))
    return ["".join(skeleton), *payloads]


def _preceded_by_executor(skeleton_so_far: str) -> bool:
    unquoted = skeleton_so_far.replace("'", " ").replace('"', " ")
    tokens = unquoted.split()
    if tokens and tokens[-1] in _EXECUTOR_FLAGS:
        return True
    running_text = _COMMAND_POSITION_RE.split(unquoted)[-1].strip()
    if (
        tokens
        and _SQL_STATEMENT_FLAG_RE.fullmatch(tokens[-1])
        and SQL_CLIENT_RE.match(running_text)
    ):
        return True
    running = running_text.split()
    return bool(running) and running[0] in _EXECUTOR_COMMANDS


_SQL_STATEMENT_FLAG_RE = re.compile(r"-[A-Za-z]*[ce]")


_HEREDOC_START_RE = re.compile(
    r"<<(?!<)(?P<dash>-?)\s*(?P<q>['\"]?)(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=q)"
)


def _consume_heredoc_bodies(
    command: str,
    index: int,
    pending: list[tuple[str, bool]],
    current: list[str],
) -> int:
    """Copy heredoc bodies through verbatim, stopping after the last terminator.

    *index* is the newline that opens the first body. Everything up to and
    including each delimiter line is appended to *current* unsplit, so a
    ``cat <<'EOF'`` payload is never mistaken for the commands it describes.
    Returns the index of the newline that closes the last body — it ends the
    command as well, so the caller splits on it. An unterminated heredoc
    consumes the rest of the command.
    """
    current.append(command[index])
    pos = index + 1
    while pending and pos < len(command):
        end = command.find("\n", pos)
        line = command[pos:] if end == -1 else command[pos:end]
        current.append(line)
        delim, strip_tabs = pending[0]
        candidate = line.lstrip("\t") if strip_tabs else line
        if candidate.strip() == delim:
            pending.pop(0)
            if not pending:
                return len(command) if end == -1 else end
        if end == -1:
            return len(command)
        current.append("\n")
        pos = end + 1
    return pos


def split_chain_segments(command: str) -> list[str]:
    """Split a shell command on chain operators (&&, ||, ;, newline), quote-aware.

    Operators inside single or double quotes are NOT treated as chain
    separators.  This prevents false positives like
    ``echo "test && rm -rf /"`` being split into two segments.

    A bare newline separates commands exactly as ``;`` does, and leaving it
    joined hid every line but the first from the per-segment scan: a
    ``for … do`` body, or any script pasted as one Bash call, classified on
    its opening line alone, so ``echo hi\\ncurl https://host/x`` was cleared
    outright by the read-only ``echo`` allow. Heredoc bodies are exempt —
    they are data, not commands — and are copied through unsplit.

    Pipes (``|``) are never split on — they stay inside their segment so
    that deny patterns like ``curl.*\\|.*bash`` can still match.

    Inspired by openclaw ``splitCommandChainWithOperators()`` which uses a
    character-by-char scanner that tracks quote state before splitting.
    """
    segments: list[str] = []
    current: list[str] = []
    pending_heredocs: list[tuple[str, bool]] = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    i = 0
    length = len(command)

    while i < length:
        ch = command[i]

        if escaped:
            current.append(ch)
            escaped = False
            i += 1
            continue

        if ch == "\\":
            escaped = True
            current.append(ch)
            i += 1
            continue

        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            current.append(ch)
            i += 1
            continue

        if ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            current.append(ch)
            i += 1
            continue

        if not in_single_quote and not in_double_quote:
            if ch == "<":
                heredoc = _HEREDOC_START_RE.match(command, i)
                if heredoc:
                    pending_heredocs.append(
                        (heredoc.group("delim"), bool(heredoc.group("dash")))
                    )
                    current.append(command[i : heredoc.end()])
                    i = heredoc.end()
                    continue

            if ch == "\n" and pending_heredocs:
                i = _consume_heredoc_bodies(command, i, pending_heredocs, current)
                continue

            if i + 1 < length and command[i : i + 2] in ("&&", "||"):
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 2
                continue

            if ch in ";\n":
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 1
                continue

        current.append(ch)
        i += 1

    seg = "".join(current).strip()
    if seg:
        segments.append(seg)

    return [
        cleaned
        for cleaned in (_LEADING_CONTINUATION_RE.sub("", seg) for seg in segments)
        if cleaned
    ]


_LEADING_CONTINUATION_RE = re.compile(r"^(?:\\\n\s*)+")


def split_pipeline_stages(segment: str) -> list[str]:
    stages: list[str] = []
    current: list[str] = []
    in_single = in_double = in_backtick = escaped = False
    depth = 0
    i = 0
    while i < len(segment):
        ch = segment[i]
        following = segment[i + 1 : i + 2]
        if escaped:
            escaped = False
        elif ch == "\\" and not in_single:
            escaped = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif in_single or in_double:
            pass
        elif ch == "`":
            in_backtick = not in_backtick
        elif in_backtick:
            pass
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif depth == 0 and ch == "\n":
            break
        elif depth == 0 and ch == "|" and following == "|":
            current.append("||")
            i += 2
            continue
        elif depth == 0 and ch == "|":
            stages.append("".join(current).strip())
            current = []
            i += 2 if following == "&" else 1
            continue
        current.append(ch)
        i += 1
    stages.append("".join(current).strip())
    return [stage for stage in stages if stage]


_REDIRECT_RE = re.compile(
    r"\s*(?:&>>?|\d?>>?|\d?<<<|\d?<<|\d?<)\s*(?:&\d+|[^\s;|&<>]+)"
)


def strip_redirections(command: str) -> str:
    """Drop shell redirections (``2>&1``, ``>/dev/null``, ``<in``) outside quotes.

    Anchored policy patterns such as ``^agent-browser\\s+tab...\\s*$`` otherwise
    fail on ``agent-browser tab 2>&1`` and fall through to the mutation rule,
    and an approval key picks up the bare file descriptor
    (``Bash::agent-browser tab 2``). Quoted text is left untouched so a ``>``
    inside an argument cannot corrupt the command a deny rule sees.
    """
    out: list[str] = []
    in_single = in_double = escaped = False
    i = 0
    while i < len(command):
        ch = command[i]
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\":
            out.append(ch)
            escaped = True
        elif ch == "'" and not in_double:
            in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
        elif not in_single and not in_double:
            match = _REDIRECT_RE.match(command, i)
            if match and match.end() > i:
                i = match.end()
                continue
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()


_ENV_ASSIGN_PREFIX_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*="
    r"(?:'[^']*'|\"[^\"$`\\]*\"|[^\s$`|<>&;()'\"\\])*\s+(?=\S)"
)
_KEYWORD_PREFIX_RE = re.compile(r"^(?:do|then|else|elif|if|while|until|!)\s+(?=\S)")
_RUNNER_PREFIX_RE = re.compile(
    r"^(?:command|exec|nohup|time|stdbuf|nice)\s+(?=\S)"
    r"|^env(?:\s+(?:-[iv0]+|-u\s*[A-Za-z_]\w*|--unset=[A-Za-z_]\w*|-C\s*\S+|--chdir=\S+))*"
    r"\s+(?=[^\s|;&])"
    r"|^xargs(?:\s+(?:-[0rtpxo]+|-[ILnPsdEa]\s*\S+|--null|--no-run-if-empty|--verbose))*"
    r"\s+(?=[^\s|;&-])"
)
_TIMEOUT_PREFIX_RE = re.compile(r"^timeout\s+(?:-\S+\s+)*[\d.]+[smhd]?\s+(?=\S)")
_GROUP_OPEN_RE = re.compile(r"^[({]\s*(?=\S)")
_FUNCTION_HEADER_RE = re.compile(
    r"^(?:function\s+)?[A-Za-z_][\w-]*\s*\(\)\s*\{\s*(?=\S)"
    r"|^function\s+[A-Za-z_][\w-]*\s*\{\s*(?=\S)"
)


def strip_command_wrappers(command: str) -> str:
    """Peel wrappers that hide the real command from ``^``-anchored rules.

    ``for y in 1 2; do agent-browser eval …; done`` splits into a ``do
    agent-browser eval …`` segment, ``timeout 30 agent-browser click @e5``
    leads with the runner, and ``SP=/tmp/x agent-browser open …`` leads with an
    inline assignment. All three used to classify as *unmatched*, which the
    auto-mode hybrid gate hands straight to Claude's native policy — so the
    wrapper was enough to slip a gated command past leashd entirely.

    ``for`` is deliberately not peeled: its header is a word list, not a
    command. Assignment values containing ``$``/backtick/operators are left
    alone so ``FOO=$(rm -rf /) ls`` still reaches the deny rules intact.
    """
    prev = None
    while command != prev:
        prev = command
        for pattern in (
            _ENV_ASSIGN_PREFIX_RE,
            _TIMEOUT_PREFIX_RE,
            _KEYWORD_PREFIX_RE,
            _RUNNER_PREFIX_RE,
            _GROUP_OPEN_RE,
            _FUNCTION_HEADER_RE,
        ):
            command = pattern.sub("", command, count=1)
    return command


_INERT_VALUE = r"(?:'[^']*'|\"(?:[^\"$`\\]|\$(?!\())*\"|[^\s$`|<>&;()'\"\\]|\$(?!\())*"
_INERT_ASSIGNMENT = r"[A-Za-z_][A-Za-z0-9_]*=" + _INERT_VALUE
_EXPORTABLE_NAME = (
    r"(?!(?:LD_|DYLD_|GIT_|PYTHON|PERL|RUBY|NODE_|LESS|BASH_ENV\b|ENV\b|PATH\b|"
    r"PAGER\b|EDITOR\b|VISUAL\b|PROMPT_COMMAND\b|IFS\b|PS4\b|SHELLOPTS\b|BASHOPTS\b))"
    r"[A-Za-z_][A-Za-z0-9_]*"
)
_EXPORTED_ASSIGNMENT = _EXPORTABLE_NAME + r"(?:=" + _INERT_VALUE + r")?"
_SHELL_CONTROL_SEGMENT_RE = re.compile(
    r"^(?:done|fi|esac|do|then|else|;;|\{|\}|\)|break|continue|:|"
    r"(?:exit|return)(?:\s+\d+)?|"
    r"for\s+[A-Za-z_][A-Za-z0-9_]*\s+in(?:\s.*)?|"
    r"case\s+(?:\"[^\"`]*\"|[^\s`;|&<>()]+)\s+in|"
    r"(?:function\s+)?[A-Za-z_][\w-]*\s*\(\)\s*\{?|"
    r"function\s+[A-Za-z_][\w-]*\s*\{?|"
    + _INERT_ASSIGNMENT
    + r"|(?:export|readonly|local|typeset|declare(?:\s+-[a-zA-Z]+)?)"
    + r"(?:\s+"
    + _EXPORTED_ASSIGNMENT
    + r")+|"
    r"unset(?:\s+-[fv])?(?:\s+[A-Za-z_][A-Za-z0-9_]*)+|"
    r"set(?:\s+(?:[-+][a-zA-Z]+|pipefail|errexit|nounset|xtrace|allexport|noclobber|noglob))+|"
    r"set\s+--(?:\s+[^`|<>&;()]*)?|"
    r"shopt\s+-[su](?:\s+\w+)+)$"
)


def is_shell_control_segment(segment: str) -> bool:
    """Whether a chain segment is pure control structure, not a command.

    ``done``/``fi``, a literal ``for x in a b`` header and a standalone
    ``SP=/tmp/x`` assignment carry no tool call, so they must not decide a
    compound command's classification — otherwise the scaffolding (unmatched,
    hence ``default_action``) outvotes the real command beside it. An
    assignment whose value can execute (``$``, backtick, an operator) is NOT
    inert and stays in the vote.

    A keyword in front (``then break``) and a redirection behind (``done >
    log.txt``) do not turn scaffolding into a command either.
    """
    text = strip_redirections(segment.strip())
    prev = None
    while text != prev:
        prev = text
        text = _KEYWORD_PREFIX_RE.sub("", text, count=1)
    return bool(_SHELL_CONTROL_SEGMENT_RE.match(text))


def strip_benign_prefixes(command: str) -> str:
    """Normalize a command down to the tool call a policy rule should see.

    Strips ``cd``/``sleep`` prefix segments in any order (``sleep 1 && cd /a &&
    npm test`` → ``npm test``), then peels command wrappers and redirections.
    """
    prev = None
    while command != prev:
        prev = command
        command = strip_cd_prefix(command)
        command = strip_sleep_prefix(command)
        command = strip_command_wrappers(command)
    return strip_redirections(command)


_CAPTURE_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\$\(")


def unwrap_capture_assignment(command: str) -> str:
    """Peel ``NAME=$( … )`` down to the command whose output it captures.

    ``v=$(curl -sS https://pypi.org/pypi/x/json | python3 …)`` runs a `curl`;
    naming the assignment instead left the approval prompt titled ``Bash::-s``,
    because the key generator skipped the whole ``v=$(curl`` token as an
    inline environment assignment and started at the next word.

    Only an assignment that is *entirely* one substitution is peeled — the
    closing paren has to be the last character — so ``v=$(a)$(b)`` and
    ``v=$(a) rm -rf c`` keep their full text and stay one decision.
    """
    if not _CAPTURE_ASSIGNMENT_RE.match(command):
        return command
    start = command.index("$(") + 2
    depth = 1
    in_single = in_double = escaped = False
    for position in range(start, len(command)):
        ch = command[position]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    inner = command[start:position].strip()
                    return inner if position == len(command) - 1 else command
    return command


def _matching_paren(text: str, start: int) -> int:
    depth = 1
    in_single = in_double = escaped = False
    for position in range(start, len(text)):
        ch = text[position]
        if escaped:
            escaped = False
        elif ch == "\\" and not in_single:
            escaped = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif in_single or in_double:
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return position
    return len(text)


def _closing_backtick(text: str, start: int) -> int:
    escaped = False
    for position in range(start, len(text)):
        ch = text[position]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "`":
            return position
    return len(text)


def _consume_heredocs(
    text: str,
    start: int,
    pending: list[tuple[str, bool, bool]],
    bodies: list[str],
) -> int:
    position = start
    lines: list[str] = []
    while pending and position < len(text):
        end = text.find("\n", position)
        line = text[position:] if end == -1 else text[position:end]
        delimiter, quoted, strip_tabs = pending[0]
        if (line.lstrip("\t") if strip_tabs else line).strip() == delimiter:
            pending.pop(0)
            if not quoted:
                _collect_substitutions("\n".join(lines), bodies, quotes_literal=True)
            lines = []
        else:
            lines.append(line)
        position = len(text) if end == -1 else end + 1
    if pending and lines and not pending[0][1]:
        _collect_substitutions("\n".join(lines), bodies, quotes_literal=True)
    return position


def _collect_substitutions(
    text: str, bodies: list[str], *, quotes_literal: bool
) -> None:
    pending: list[tuple[str, bool, bool]] = []
    in_single = in_double = escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        shell_quoting = not quotes_literal and not in_double
        if escaped:
            escaped = False
        elif ch == "\\" and not in_single:
            escaped = True
        elif in_single:
            in_single = ch != "'"
        elif ch == "'" and shell_quoting:
            in_single = True
        elif ch == '"' and not quotes_literal:
            in_double = not in_double
        elif text.startswith("$(", i):
            end = _matching_paren(text, i + 2)
            if text.startswith("$((", i) and _matching_paren(text, i + 3) == end - 1:
                i += 3
                continue
            bodies.append(text[i + 2 : end])
            i = end + 1
            continue
        elif shell_quoting and text.startswith(("<(", ">("), i):
            end = _matching_paren(text, i + 2)
            bodies.append(text[i + 2 : end])
            i = end + 1
            continue
        elif ch == "`":
            end = _closing_backtick(text, i + 1)
            bodies.append(text[i + 1 : end])
            i = end + 1
            continue
        elif (
            ch == "<"
            and shell_quoting
            and (heredoc := _HEREDOC_START_RE.match(text, i))
        ):
            pending.append(
                (
                    heredoc.group("delim"),
                    bool(heredoc.group("q")),
                    bool(heredoc.group("dash")),
                )
            )
            i = heredoc.end()
            continue
        elif ch == "\n" and pending and not in_double:
            i = _consume_heredocs(text, i + 1, pending, bodies)
            continue
        i += 1


def command_substitutions(command: str) -> list[str]:
    bodies: list[str] = []
    _collect_substitutions(command, bodies, quotes_literal=False)
    return [body.strip() for body in bodies if body.strip()]


_FUNCTION_DEFINITION_RE = re.compile(
    r"^(?:function\s+(?P<keyword>[A-Za-z_][\w-]*)\s*(?:\(\))?|(?P<name>[A-Za-z_][\w-]*)\s*\(\))\s*\{"
)
_FUNCTION_SCOPE_BREAKERS_RE = re.compile(
    r"(?<![$<>])\((?!\))|\bunset\b|\bsource\b|(?:^|[\s;&|])\.\s"
)


def _function_call_head(text: str) -> str:
    prev = None
    while text != prev:
        prev = text
        for pattern in (_ENV_ASSIGN_PREFIX_RE, _KEYWORD_PREFIX_RE, _GROUP_OPEN_RE):
            text = pattern.sub("", text, count=1)
    words = text.split(maxsplit=1)
    return words[0] if words else ""


def command_units(command: str) -> list[tuple[str, str]]:
    """The commands a Bash call runs, each tagged with how it runs.

    A call to a function defined earlier in the same command is not a unit of
    its own: the function's body is, and every command in that body is
    classified where it is defined. Voting on the call as well asked the human
    to approve ``Bash::js`` for ``js() { agent-browser eval --stdin; }; js``.
    Only a plain call counts — ``command curl`` and ``timeout 5 curl`` run the
    binary, not a function that shadows it — and only when nothing in the
    command can take the definition back out of scope: a subshell, ``unset``,
    or a sourced file.
    """
    track_functions = not _FUNCTION_SCOPE_BREAKERS_RE.search(
        shell_match_texts(command)[0]
    )
    functions: set[str] = set()
    units: list[tuple[str, str]] = []
    for segment in split_chain_segments(command):
        for body in command_substitutions(segment):
            units.extend(
                (text, kind if kind == "pipeline" else "substituted")
                for text, kind in command_units(body)
            )
        peeled = segment
        while (stripped := _KEYWORD_PREFIX_RE.sub("", peeled, count=1)) != peeled:
            peeled = stripped
        definition = _FUNCTION_DEFINITION_RE.match(peeled)
        if track_functions and definition:
            functions.add(definition.group("keyword") or definition.group("name"))
        if (
            is_shell_control_segment(segment)
            or unwrap_capture_assignment(peeled) != peeled
        ):
            continue
        stages = split_pipeline_stages(segment)
        if len(stages) < 2:
            if _function_call_head(segment) not in functions:
                units.append((segment, "command"))
            continue
        units.append((segment, "pipeline"))
        units.extend(
            (stage, "command" if position == 0 else "piped")
            for position, stage in enumerate(stages)
            if _function_call_head(stage) not in functions
        )
    return units


_NETWORK_READ_BINARIES = frozenset({"curl", "wget"})

_SHELL_OPERATOR_TOKENS = frozenset({"|", "||", "&&", ";", "&", ">", ">>", "<", "<<"})

_SWITCH = "switch"
_VALUE = "value"
_REFUSE = "refuse"
_METHOD = "method"
_OUTPUT = "output"
_OUTPUT_DIR = "output-dir"
_CWD_OUTPUT = "cwd-output"
_URL = "url"

_TAKES_VALUE = frozenset({_VALUE, _REFUSE, _METHOD, _OUTPUT, _OUTPUT_DIR, _URL})

_CURL_SHORT_FLAGS = {
    **dict.fromkeys("0123456#:BfgGIijkLlMNnpqRsSvVZah", _SWITCH),
    **dict.fromkeys("AbCeEHmPrtuUwyYz", _VALUE),
    **dict.fromkeys("dFTxKQ", _REFUSE),
    **dict.fromkeys("oDc", _OUTPUT),
    **dict.fromkeys("OJ", _CWD_OUTPUT),
    "X": _METHOD,
}

_CURL_LONG_FLAGS = {
    **dict.fromkeys(
        (
            "--user-agent",
            "--cookie",
            "--referer",
            "--header",
            "--max-time",
            "--connect-timeout",
            "--retry",
            "--retry-delay",
            "--retry-max-time",
            "--write-out",
            "--user",
            "--range",
            "--time-cond",
            "--speed-limit",
            "--speed-time",
            "--limit-rate",
            "--max-filesize",
            "--max-redirs",
            "--cacert",
            "--capath",
            "--cert",
            "--cert-type",
            "--key",
            "--key-type",
            "--pass",
            "--ciphers",
            "--curves",
            "--proto",
            "--proto-redir",
            "--proto-default",
            "--tls-max",
            "--expect100-timeout",
            "--keepalive-time",
            "--happy-eyeballs-timeout-ms",
            "--interface",
            "--local-port",
            "--noproxy",
            "--oauth2-bearer",
            "--aws-sigv4",
            "--continue-at",
            "--create-file-mode",
            "--parallel-max",
            "--request-target",
        ),
        _VALUE,
    ),
    **dict.fromkeys(
        (
            "--output",
            "--dump-header",
            "--cookie-jar",
            "--trace",
            "--trace-ascii",
            "--stderr",
            "--etag-save",
            "--hsts",
            "--alt-svc",
            "--libcurl",
        ),
        _OUTPUT,
    ),
    **dict.fromkeys(
        ("--remote-name", "--remote-name-all", "--remote-header-name"), _CWD_OUTPUT
    ),
    "--output-dir": _OUTPUT_DIR,
    "--request": _METHOD,
    "--url": _URL,
}

_WGET_SHORT_FLAGS = {
    **dict.fromkeys("qvdhVcNSrmkKpExFb46", _SWITCH),
    **dict.fromkeys("TtUwQlARDIXBn", _VALUE),
    **dict.fromkeys("eiH", _REFUSE),
    **dict.fromkeys("Ooa", _OUTPUT),
    "P": _OUTPUT_DIR,
}

_WGET_LONG_FLAGS = {
    **dict.fromkeys(
        (
            "--timeout",
            "--tries",
            "--user-agent",
            "--header",
            "--wait",
            "--waitretry",
            "--user",
            "--password",
            "--http-user",
            "--http-password",
            "--quota",
            "--limit-rate",
            "--level",
            "--referer",
            "--max-redirect",
            "--dns-timeout",
            "--connect-timeout",
            "--read-timeout",
            "--ca-certificate",
            "--certificate",
            "--private-key",
            "--load-cookies",
            "--accept",
            "--reject",
            "--accept-regex",
            "--reject-regex",
            "--domains",
            "--exclude-domains",
            "--include-directories",
            "--exclude-directories",
            "--restrict-file-names",
            "--progress",
            "--base",
            "--local-encoding",
            "--remote-encoding",
        ),
        _VALUE,
    ),
    **dict.fromkeys(
        (
            "--output-document",
            "--output-file",
            "--append-output",
            "--save-cookies",
            "--warc-file",
            "--rejected-log",
        ),
        _OUTPUT,
    ),
    "--directory-prefix": _OUTPUT_DIR,
    "--method": _METHOD,
}

_NETWORK_FLAGS = {
    "curl": (_CURL_SHORT_FLAGS, _CURL_LONG_FLAGS),
    "wget": (_WGET_SHORT_FLAGS, _WGET_LONG_FLAGS),
}

_REFUSED_LONG_FLAG_PREFIXES = (
    "--data",
    "--form",
    "--json",
    "--upload",
    "--post",
    "--body",
    "--proxy",
    "--preproxy",
    "--socks",
    "--unix-socket",
    "--abstract-unix-socket",
    "--resolve",
    "--connect-to",
    "--doh",
    "--config",
    "--variable",
    "--expand",
    "--url-query",
    "--input-file",
    "--execute",
    "--span-hosts",
    "--mail",
    "--quote",
    "--ftp",
    "--telnet",
)

_READ_METHODS = frozenset({"GET", "HEAD"})

_DISCARDED_OUTPUTS = frozenset({"-", "/dev/null", "/dev/stdout", "/dev/stderr"})

_URL_TOKEN_RE = re.compile(r"^(?:--url=)?(?:[a-zA-Z][a-zA-Z0-9+.-]*://)")

_SCHEMELESS_URL_RE = re.compile(
    r"(?P<host>localhost|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f:.]+\]|"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+"
    r"[A-Za-z](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
    r"(?::\d{1,5})?(?:[/?#]\S*)?"
)


def _url_host(token: str) -> str | None:
    """The host a URL token names, or ``None`` when it is not fixed here.

    Only the authority has to be literal. ``https://api.github.com/repos/$r``
    inside a ``for`` loop connects to the same host every time and is most of
    what a research turn actually runs, but ``https://api.github.com$SUFFIX``
    does not — the variable sits where the host is still being decided.
    """
    token = token.removeprefix("--url=")
    if not _URL_TOKEN_RE.match(token):
        match = _SCHEMELESS_URL_RE.fullmatch(token)
        return match.group("host").strip("[]").lower() if match else None
    parts = urlsplit(token)
    if "$" in parts.netloc or "`" in parts.netloc:
        return None
    return parts.hostname.lower() if parts.hostname else None


def _write_target(value: str, *, is_dir: bool) -> str | None:
    if value in _DISCARDED_OUTPUTS:
        return ""
    if not value or "$" in value or "`" in value:
        return None
    if is_dir:
        return value
    return (value.rsplit("/", 1)[0] or "/") if "/" in value else "."


def network_read_scope(command: str) -> str | None:
    """The destination a read-only ``curl``/``wget`` should be approved for.

    Returns ``curl <host>[,<host>…][><dir>]`` — the scope a human is really
    being asked to trust, naming where the bytes come from and, when the
    fetch writes to disk, where they land — or ``None`` when the invocation
    is not a plain read, when its destination is not literal, or when it
    names a credential path.

    The punctuation matters because ``_matches_auto_approved`` treats a
    stored key as covering any key extending it at a *space* boundary. Hosts
    are comma-joined and a write target follows ``>`` with no space, so
    neither a second host nor an output path can be appended to widen a grant
    already given; the host still leads, so a truncated button label shows
    the part that decides the answer.

    Keying network calls on their full command line meant every URL was a
    separate decision: 116 of one day's 141 approval prompts were ``curl``,
    across 19 hosts, and "Approve all" bound to one literal invocation so the
    next path under the same host asked again. A host is the unit a human can
    actually reason about; the guards below keep the grant to reads of it.

    Anything that can move data outward or redirect the connection
    disqualifies the command: a body/upload flag, a method other than
    GET/HEAD, a proxy or ``--resolve`` override, a flag file, or a URL list.
    Command substitution and variables disqualify it too — the host must be
    visible here, not assembled at runtime — as does a credential path, so a
    grant for a host never widens into ``-o ~/.ssh/authorized_keys``.

    Every argument has to be accounted for. A flag's value is skipped only
    when the flag is known to take one, and whatever is left must be a URL —
    schemed or not, since ``curl -s '127.0.0.1:8000/api?x=1'`` is how a dev
    server is usually probed. An unrecognised word is a reason to ask rather
    than a word to ignore: ignoring it let ``curl https://ok.example evil.example``
    key on ``ok.example`` alone. Output that is thrown away (``-o /dev/null``,
    ``-D -``) is not a write.
    """
    if "$(" in command or "`" in command:
        return None
    try:
        tokens = shlex.split(command.replace("\\\n", " "))
    except ValueError:
        return None
    if not tokens or tokens[0] not in _NETWORK_READ_BINARIES:
        return None

    binary = tokens[0]
    short_flags, long_flags = _NETWORK_FLAGS[binary]
    hosts: set[str] = set()
    outputs: set[str] = set()
    remaining = iter(tokens[1:])
    for token in remaining:
        if token in _SHELL_OPERATOR_TOKENS:
            break
        if token.startswith("--") and len(token) > 2:
            name, has_inline, inline = token.partition("=")
            if name.startswith(_REFUSED_LONG_FLAG_PREFIXES):
                return None
            role = long_flags.get(name, _SWITCH)
            if role not in _TAKES_VALUE:
                if role == _CWD_OUTPUT:
                    outputs.add(".")
                continue
            value = inline if has_inline else next(remaining, None)
        elif token.startswith("-") and len(token) > 1:
            role = _SWITCH
            value = None
            for position, letter in enumerate(token[1:], start=2):
                role = short_flags.get(letter, _REFUSE)
                if role == _CWD_OUTPUT:
                    outputs.add(".")
                if role in _TAKES_VALUE:
                    value = token[position:] or next(remaining, None)
                    break
            if role not in _TAKES_VALUE:
                continue
        else:
            role, value = _URL, token
        if role == _REFUSE or value is None:
            return None
        if role == _METHOD:
            if value.upper() not in _READ_METHODS:
                return None
        elif role in (_OUTPUT, _OUTPUT_DIR):
            target = _write_target(value, is_dir=role == _OUTPUT_DIR)
            if target is None:
                return None
            if target:
                outputs.add(target)
        elif role == _URL:
            host = _url_host(value)
            if not host:
                return None
            hosts.add(host)

    if not hosts:
        return None
    if any(pattern.search(command) for pattern in _CREDENTIAL_PATTERNS):
        return None

    destination = ",".join(sorted(hosts))
    if outputs:
        return f"{binary} {destination}>{','.join(sorted(outputs))}"
    return f"{binary} {destination}"


_IDENTITY_SHORT_FLAGS = {"curl": frozenset("bEnuU"), "wget": frozenset()}

_IDENTITY_LONG_FLAGS = frozenset(
    {
        "--cookie",
        "--cookie-jar",
        "--user",
        "--cert",
        "--key",
        "--pass",
        "--oauth2-bearer",
        "--aws-sigv4",
        "--netrc",
        "--netrc-file",
        "--netrc-optional",
        "--negotiate",
        "--ntlm",
        "--digest",
        "--basic",
        "--anyauth",
        "--delegation",
        "--location-trusted",
        "--password",
        "--http-user",
        "--http-password",
        "--load-cookies",
        "--certificate",
        "--private-key",
        "--ask-password",
        "--use-askpass",
        "--auth-no-challenge",
    }
)

_PUBLIC_HEADER_NAMES = frozenset(
    {
        "accept",
        "accept-language",
        "accept-encoding",
        "user-agent",
        "cache-control",
        "range",
    }
)

_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".lan", ".localhost", ".home.arpa")

_PUBLIC_WRITE_DIRS = ("/tmp", "/private/tmp")  # noqa: S108


def _is_public_header(value: str | None) -> bool:
    if value is None:
        return False
    name, colon, _ = value.partition(":")
    return bool(colon) and name.strip().lower() in _PUBLIC_HEADER_NAMES


def _sends_identity(binary: str, tokens: list[str]) -> bool:
    short_flags, long_flags = _NETWORK_FLAGS[binary]
    identity_short = _IDENTITY_SHORT_FLAGS[binary]
    remaining = iter(tokens)
    for token in remaining:
        if token in _SHELL_OPERATOR_TOKENS:
            return False
        if token.startswith("--") and len(token) > 2:
            name, has_inline, inline = token.partition("=")
            if name in _IDENTITY_LONG_FLAGS:
                return True
            if name == "--header":
                value = inline if has_inline else next(remaining, None)
                if not _is_public_header(value):
                    return True
            elif long_flags.get(name, _SWITCH) in _TAKES_VALUE and not has_inline:
                next(remaining, None)
            continue
        if not token.startswith("-") or len(token) == 1:
            continue
        for position, letter in enumerate(token[1:], start=2):
            if letter in identity_short:
                return True
            if short_flags.get(letter) in _TAKES_VALUE:
                value = token[position:] or next(remaining, None)
                if binary == "curl" and letter == "H" and not _is_public_header(value):
                    return True
                break
    return False


def _is_public_host(host: str) -> bool:
    if "." not in host or ":" in host or host.endswith(_PRIVATE_HOST_SUFFIXES):
        return False
    return not host.replace(".", "").isdigit()


def _is_public_write_dir(target: str) -> bool:
    return ".." not in target.split("/") and any(
        target == root or target.startswith(root + "/") for root in _PUBLIC_WRITE_DIRS
    )


def public_read_scope(command: str) -> str | None:
    """``curl+public <host>…`` when a read reaches only public hosts anonymously.

    Stricter than :func:`network_read_scope`, whose scope a human approves
    per host: this form is allowed without asking, so it must carry nothing
    of the user's. Any ``$`` disqualifies (a variable can smuggle a token
    into a URL or header), as does every auth, cookie, certificate and netrc
    flag, and a header other than content negotiation. Hosts must be public
    DNS names, not IP literals or private suffixes, and a fetch may write
    only under ``/tmp`` — so a ``wget`` must name its output, because without
    ``-O`` it saves into the working directory. The policy engine drops any
    typed command spelling the ``+public`` form, so only this function
    produces it.
    """
    if "$" in command:
        return None
    scope = network_read_scope(command)
    if scope is None:
        return None
    binary, _, rest = scope.partition(" ")
    destination, _, outputs = rest.partition(">")
    if not all(_is_public_host(host) for host in destination.split(",")):
        return None
    if outputs and not all(
        _is_public_write_dir(target) for target in outputs.split(",")
    ):
        return None
    tokens = shlex.split(command.replace("\\\n", " "))
    if _sends_identity(binary, tokens[1:]):
        return None
    if binary == "wget" and not _names_wget_output(tokens[1:]):
        return None
    return f"{binary}+public {destination}"


def _names_wget_output(tokens: list[str]) -> bool:
    for token in tokens:
        if token in _SHELL_OPERATOR_TOKENS:
            return False
        if token.startswith("--output-document"):
            return True
        if not token.startswith("-") or token.startswith("--"):
            continue
        for letter in token[1:]:
            if letter == "O":
                return True
            if _WGET_SHORT_FLAGS.get(letter) in _TAKES_VALUE:
                break
    return False


_SHELL_CREDENTIAL_RE = re.compile(
    r"\.env\b(?!\.(?:example|sample|template|dist)\b)|\.ssh/|\.aws/|\.gnupg/|"
    r"authorized_keys|\.pem\b|\.key\b|\.p12\b|\.pfx\b|\.keystore\b|"
    r"id_rsa|id_ed25519|id_ecdsa|id_dsa|\.git-credentials\b|credentials\b|"
    r"secrets?\.|token\.json\b"
)


def mentions_credential_path(command: str) -> bool:
    """Whether the text the shell acts on names a credential path.

    Quoted runs are blanked first, so ``ssh host 'cat ~/.ssh/config'`` — a
    path on the remote machine — does not count, while a redirection such as
    ``< ~/.aws/credentials`` feeding that same connection does.
    """
    return bool(_SHELL_CREDENTIAL_RE.search(shell_match_texts(command)[0]))


_SSH_SWITCHES = frozenset("46aCfkNnqsTtvxy")
_SSH_VALUE_FLAGS = frozenset("bBceilmop")
_SCP_SWITCHES = frozenset("46BCpqrTv")
_SCP_VALUE_FLAGS = frozenset("ciloP")

_SSH_SCOPED_OPTIONS = frozenset(
    {
        "addressfamily",
        "batchmode",
        "checkhostip",
        "compression",
        "connectionattempts",
        "connecttimeout",
        "escapechar",
        "hashknownhosts",
        "identitiesonly",
        "kbdinteractiveauthentication",
        "loglevel",
        "numberofpasswordprompts",
        "passwordauthentication",
        "port",
        "preferredauthentications",
        "pubkeyauthentication",
        "requesttty",
        "serveralivecountmax",
        "serveraliveinterval",
        "stricthostkeychecking",
        "tcpkeepalive",
        "updatehostkeys",
        "user",
        "userknownhostsfile",
        "verifyhostkeydns",
        "visualhostkey",
    }
)

_REMOTE_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_REMOTE_USER_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
_PORT_RE = re.compile(r"\d{1,5}")


class _RemoteTarget:
    def __init__(self) -> None:
        self.user: str | None = None
        self.host: str | None = None
        self.port: str | None = None

    def set(self, field: str, value: str) -> bool:
        pattern = {
            "user": _REMOTE_USER_RE,
            "host": _REMOTE_HOST_RE,
            "port": _PORT_RE,
        }[field]
        if not pattern.fullmatch(value):
            return False
        current = getattr(self, field)
        if current is not None and current != value:
            return False
        setattr(self, field, value)
        return True

    def set_destination(self, destination: str) -> bool:
        destination = destination.removeprefix("ssh://")
        user, at, host = destination.rpartition("@")
        if at and not self.set("user", user):
            return False
        return self.set("host", host)

    def scope(self, binary: str) -> str | None:
        if self.host is None:
            return None
        name = f"{self.user}@{self.host}" if self.user else self.host
        port = f" -p {self.port}" if self.port and self.port != "22" else ""
        return f"{binary} {name}{port}"


def _apply_ssh_option(target: _RemoteTarget, setting: str) -> bool:
    key, _, value = setting.replace("=", " ", 1).partition(" ")
    key = key.strip().lower()
    value = value.strip()
    if key not in _SSH_SCOPED_OPTIONS or not value:
        return False
    if key in ("port", "user"):
        return target.set(key, value)
    return True


def _apply_login_flags(
    token: str,
    remaining: Iterator[str],
    target: _RemoteTarget,
    *,
    switches: frozenset[str],
    value_flags: frozenset[str],
    port_flag: str,
) -> bool:
    for position, letter in enumerate(token[1:], start=2):
        if letter in switches:
            continue
        if letter not in value_flags:
            return False
        value = token[position:] or next(remaining, None)
        if value is None or "$" in value or "`" in value:
            return False
        if _SHELL_CREDENTIAL_RE.search(value):
            return False
        if letter == port_flag:
            return target.set("port", value)
        if letter == "l" and port_flag == "p":
            return target.set("user", value)
        if letter == "o":
            return _apply_ssh_option(target, value)
        return True
    return True


def _ssh_scope(remaining: Iterator[str]) -> str | None:
    target = _RemoteTarget()
    for word in remaining:
        if word in _SHELL_OPERATOR_TOKENS or word.startswith("--"):
            break
        if word.startswith("-") and len(word) > 1:
            if not _apply_login_flags(
                word,
                remaining,
                target,
                switches=_SSH_SWITCHES,
                value_flags=_SSH_VALUE_FLAGS,
                port_flag="p",
            ):
                return None
            continue
        if target.host is not None:
            break
        if "$" in word or "`" in word or not target.set_destination(word):
            return None
    return target.scope("ssh")


def _scp_scope(remaining: Iterator[str]) -> str | None:
    target = _RemoteTarget()
    paths: list[str] = []
    for token in remaining:
        if token in _SHELL_OPERATOR_TOKENS:
            break
        if token.startswith("-") and len(token) > 1 and not paths:
            if not _apply_login_flags(
                token,
                remaining,
                target,
                switches=_SCP_SWITCHES,
                value_flags=_SCP_VALUE_FLAGS,
                port_flag="P",
            ):
                return None
            continue
        paths.append(token)
    if len(paths) < 2:
        return None
    *sources, destination = paths
    for source in sources:
        if "$" in source or "`" in source or _SHELL_CREDENTIAL_RE.search(source):
            return None
        if ":" in source.split("/", 1)[0]:
            return None
    remote, colon, _ = destination.partition(":")
    if not colon or "/" in remote or "$" in remote or "`" in remote:
        return None
    if not target.set_destination(remote):
        return None
    return target.scope("scp")


def remote_login_scope(command: str) -> str | None:
    """The host an ``ssh`` session or ``scp`` upload should be approved for.

    Returns ``ssh [user@]host[ -p port]`` (or ``scp …`` for an upload), or
    ``None`` when the invocation is not one a host-wide grant can safely cover.

    Keying on the whole invocation made "Approve all" grant one literal remote
    command: an agent tending a training pod ran seven different ``ssh … root@pod
    '…'`` calls in an hour and every one of them asked again. The remote
    command runs on the far machine, so the host is the thing being trusted —
    which also means the grant is only as narrow as the host, and anything that
    changes what the connection reaches declines: a jump host, a proxy or local
    command, port or agent forwarding, a config file, a variable in the
    destination. ``scp`` collapses only for an upload of literal local paths;
    a download writes to this machine and keeps its full-invocation key.
    """
    lexer = shlex.shlex(command.replace("\\\n", " "), posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    remaining = iter(lexer)
    try:
        binary = next(remaining, None)
        if binary == "ssh":
            return _ssh_scope(remaining)
        if binary == "scp":
            return _scp_scope(remaining)
    except ValueError:
        return None
    return None


_DOCKER_EXEC_PREFIX = (
    r"(?:docker|podman)\s+(?:container\s+)?exec"
    r"(?:\s+(?:-[it]*[euw](?:=|\s+)[^\s\"'`$]+"
    r"|--(?:env|user|workdir|detach-keys)(?:=|\s+)[^\s\"'`$]+|-[dit]+"
    r"|--(?:interactive|tty|detach)))*"
    r"\s+\w[\w.-]*\s+"
    r"|(?:(?:docker|podman)\s+compose|docker-compose|podman-compose)"
    r"(?:\s+(?:-f|--file|-p|--project-name|--profile|--project-directory)"
    r"(?:=|\s+)[^\s\"'`$]+)*"
    r"\s+exec(?:\s+(?:-[euw](?:=|\s+)[^\s\"'`$]+"
    r"|--(?:env|user|workdir|index)(?:=|\s+)[^\s\"'`$]+|-[dT]+"
    r"|--(?:detach|no-TTY|no-tty)))*"
    r"\s+\w[\w.-]*\s+"
)

_DOCKER_EXEC_RE = re.compile(rf"^(?:{_DOCKER_EXEC_PREFIX})(?P<inner>\S.*)$", re.DOTALL)

SQL_CLIENT_RE = re.compile(
    rf"^(?:{_DOCKER_EXEC_PREFIX})?"
    r"(?:sqlite3?|duckdb|psql|mysql|mariadb)\b"
    r"(?!.*\s(?:-f|--file|-init)\b)"
)

_CONTAINER_ENVIRONMENT_RE = re.compile(
    r"^(?:env|printenv|export|set|declare|compgen)\b|/proc/[^\s/]*/environ"
)


def docker_exec_command(command: str) -> str | None:
    """The command a ``docker exec`` / ``docker compose exec`` runs inside.

    ``docker exec db df -h /`` is a disk check and ``docker exec db psql -c
    "SELECT …"`` is a query, but both were one unmatched ``docker exec`` to
    the policy — the most-asked docker command in a week of sessions. Judging
    what runs inside by the same rules as a local command lets reads through.

    Declines, leaving the whole call unmatched, when a flag the prefix does
    not know is present (``--privileged``, a quoted ``-e``) and when the inner
    command dumps the container's environment: that is where a compose
    project keeps its passwords, which is also why ``docker inspect`` is only
    allowed with a ``--format``.
    """
    match = _DOCKER_EXEC_RE.match(command)
    if not match:
        return None
    inner = match.group("inner")
    if _CONTAINER_ENVIRONMENT_RE.search(inner):
        return None
    return inner


def analyze_bash(command: str) -> CommandAnalysis:
    """Analyze a bash command for structural features and risk factors."""
    risk_factors: list[str] = []

    has_pipe = "|" in command
    has_chain = any(op in command for op in ("&&", "||", ";", "\n"))
    has_subshell = "$(" in command or "`" in command
    has_redirect = any(op in command for op in [">", ">>", "<"])
    has_sudo = bool(re.search(r"\bsudo\b", command))

    if has_sudo:
        risk_factors.append("uses sudo")
    if has_subshell:
        risk_factors.append("contains subshell")
    if has_pipe and has_redirect:
        risk_factors.append("pipe with redirect")

    parts = re.split(r"\s*[|;\n]\s*|\s*&&\s*|\s*\|\|\s*", command)
    commands = [part.strip() for part in parts if part.strip()]

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
