"""Bash command parser and path classifier."""

import re
import shlex
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
    running = _COMMAND_POSITION_RE.split(unquoted)[-1].split()
    return bool(running) and running[0] in _EXECUTOR_COMMANDS


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

    return segments


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


_ENV_ASSIGN_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=[^\s$`|<>&;()]*\s+(?=\S)")
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


_INERT_ASSIGNMENT = r"[A-Za-z_][A-Za-z0-9_]*=[^\s$`|<>&;()]*"
_EXPORTABLE_NAME = (
    r"(?!(?:LD_|DYLD_|GIT_|PYTHON|PERL|RUBY|NODE_|LESS|BASH_ENV\b|ENV\b|PATH\b|"
    r"PAGER\b|EDITOR\b|VISUAL\b|PROMPT_COMMAND\b|IFS\b|PS4\b|SHELLOPTS\b|BASHOPTS\b))"
    r"[A-Za-z_][A-Za-z0-9_]*"
)
_EXPORTED_ASSIGNMENT = _EXPORTABLE_NAME + r"(?:=[^\s$`|<>&;()]*)?"
_SHELL_CONTROL_SEGMENT_RE = re.compile(
    r"^(?:done|fi|esac|do|then|else|;;|\}|\)|break|continue|:|"
    r"(?:exit|return)(?:\s+\d+)?|"
    r"for\s+[A-Za-z_][A-Za-z0-9_]*\s+in(?:\s.*)?|"
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
    """
    return bool(_SHELL_CONTROL_SEGMENT_RE.match(segment.strip()))


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


def command_units(command: str) -> list[tuple[str, str]]:
    units: list[tuple[str, str]] = []
    for segment in split_chain_segments(command):
        for body in command_substitutions(segment):
            units.extend(
                (text, kind if kind == "pipeline" else "substituted")
                for text, kind in command_units(body)
            )
        if (
            is_shell_control_segment(segment)
            or unwrap_capture_assignment(segment) != segment
        ):
            continue
        stages = split_pipeline_stages(segment)
        if len(stages) < 2:
            units.append((segment, "command"))
            continue
        units.append((segment, "pipeline"))
        units.append((stages[0], "command"))
        units.extend((stage, "piped") for stage in stages[1:])
    return units


_NETWORK_READ_BINARIES = frozenset({"curl", "wget"})

_SHELL_OPERATOR_TOKENS = frozenset({"|", "||", "&&", ";", "&", ">", ">>", "<", "<<"})

_UPLOAD_FLAGS = frozenset(
    {
        "-d",
        "-F",
        "-T",
        "--data",
        "--data-ascii",
        "--data-binary",
        "--data-raw",
        "--data-urlencode",
        "--form",
        "--form-string",
        "--json",
        "--upload-file",
        "--post-data",
        "--post-file",
        "--body-data",
        "--body-file",
    }
)

_OPAQUE_TARGET_FLAGS = frozenset(
    {
        "-x",
        "--proxy",
        "--preproxy",
        "--socks4",
        "--socks4a",
        "--socks5",
        "--socks5-hostname",
        "--unix-socket",
        "--abstract-unix-socket",
        "--resolve",
        "--connect-to",
        "-K",
        "--config",
    }
)

_WGET_OPAQUE_TARGET_FLAGS = frozenset({"-i", "--input-file", "-e", "--execute"})

# Every short flag above, plus the output and method flags. A bundled cluster
# (``-sO``, ``-sd @payload``) hides them from an exact-match check, and getting
# their arguments right inside a cluster is not worth the subtlety — a cluster
# carrying any of these declines to collapse and costs one extra tap.
_SHORT_FLAGS_NEEDING_CARE = frozenset("dFTxKieXoO")

_SHORT_FLAG_CLUSTER_RE = re.compile(r"^-[a-zA-Z]{2,}$")

_METHOD_FLAGS = frozenset({"-X", "--request"})

_READ_METHODS = frozenset({"GET", "HEAD"})

_OUTPUT_FLAGS = frozenset({"-o", "--output", "--output-dir", "-O", "--output-document"})

_CWD_OUTPUT_FLAGS = frozenset({"-O", "--remote-name"})

_URL_TOKEN_RE = re.compile(r"^(?:--url=)?(?:[a-zA-Z][a-zA-Z0-9+.-]*://)")


def _url_host(token: str) -> str | None:
    """The host a URL token names, or ``None`` when it is not fixed here.

    Only the authority has to be literal. ``https://api.github.com/repos/$r``
    inside a ``for`` loop connects to the same host every time and is most of
    what a research turn actually runs, but ``https://api.github.com$SUFFIX``
    does not — the variable sits where the host is still being decided.
    """
    token = token.removeprefix("--url=")
    parts = urlsplit(token)
    if "$" in parts.netloc or "`" in parts.netloc:
        return None
    return parts.hostname.lower() if parts.hostname else None


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
    """
    if "$(" in command or "`" in command:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or tokens[0] not in _NETWORK_READ_BINARIES:
        return None

    binary = tokens[0]
    opaque = _OPAQUE_TARGET_FLAGS
    if binary == "wget":
        opaque = opaque | _WGET_OPAQUE_TARGET_FLAGS
    hosts: set[str] = set()
    output_dir: str | None = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_OPERATOR_TOKENS:
            break
        if _SHORT_FLAG_CLUSTER_RE.match(token) and (
            set(token[1:]) & _SHORT_FLAGS_NEEDING_CARE
        ):
            return None
        flag = token.split("=", 1)[0]
        if flag in _UPLOAD_FLAGS or flag in opaque:
            return None
        if flag in _METHOD_FLAGS or flag == "--method":
            value = (
                token.split("=", 1)[1]
                if "=" in token
                else (tokens[index + 1] if index + 1 < len(tokens) else "")
            )
            if value.upper() not in _READ_METHODS:
                return None
            index += 1 if "=" in token else 2
            continue
        if flag in _CWD_OUTPUT_FLAGS:
            output_dir = "."
            index += 1
            continue
        if flag in _OUTPUT_FLAGS:
            value = (
                token.split("=", 1)[1]
                if "=" in token
                else (tokens[index + 1] if index + 1 < len(tokens) else "")
            )
            if not value or "$" in value:
                return None
            if flag == "--output-dir":
                output_dir = value
            else:
                output_dir = value.rsplit("/", 1)[0] if "/" in value else "."
            index += 1 if "=" in token else 2
            continue
        if _URL_TOKEN_RE.match(token):
            host = _url_host(token)
            if not host:
                return None
            hosts.add(host)
        index += 1

    if not hosts:
        return None
    if any(pattern.search(command) for pattern in _CREDENTIAL_PATTERNS):
        return None

    destination = ",".join(sorted(hosts))
    if output_dir:
        return f"{binary} {destination}>{output_dir}"
    return f"{binary} {destination}"


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
