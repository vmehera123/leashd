"""Skill management — validate, install, remove, list, tag-query."""

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, ConfigDict

from leashd.config_store import (
    get_skills_config,
    remove_skill_metadata,
    save_skill_metadata,
)

logger = structlog.get_logger()

_SKILLS_DIR = Path.home() / ".claude" / "skills"

_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_NAME_MAX_LEN = 64


class SkillInfo(BaseModel):
    """Metadata for an installed skill."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    installed_at: str
    source: str
    tags: list[str] = []


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract YAML frontmatter between --- markers."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return {}
    fm_text = "\n".join(lines[1:end])
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _validate_name(name: str) -> None:
    if len(name) > _NAME_MAX_LEN:
        msg = f"Skill name too long (max {_NAME_MAX_LEN} chars): {name}"
        raise ValueError(msg)
    if not _NAME_PATTERN.match(name):
        msg = f"Invalid skill name (lowercase alphanumeric + hyphens only): {name}"
        raise ValueError(msg)


def _safe_extractall(zf: zipfile.ZipFile, target: Path) -> None:
    """Extract zip contents, blocking path traversal (zip slip)."""
    resolved = target.resolve()
    for member in zf.infolist():
        member_path = (resolved / member.filename).resolve()
        if member_path != resolved and not str(member_path).startswith(
            str(resolved) + os.sep
        ):
            msg = f"Zip path traversal blocked: {member.filename}"
            raise ValueError(msg)
    zf.extractall(target)


def validate_skill_zip(
    path: str | Path,
) -> tuple[str, str, str]:
    """Open zip, find SKILL.md, parse frontmatter, validate name + description.

    Returns (name, description, relative_dir) where relative_dir is the
    directory within the zip containing SKILL.md (empty string if at root).
    """
    path = Path(path)
    if not path.is_file():
        msg = f"Zip file not found: {path}"
        raise FileNotFoundError(msg)

    with zipfile.ZipFile(path) as zf:
        skill_md_paths = [
            n for n in zf.namelist() if n.endswith("SKILL.md") and n.count("/") <= 1
        ]
        if not skill_md_paths:
            msg = "No SKILL.md found in zip (checked root and one-level subdirectory)"
            raise ValueError(msg)

        skill_md_path = skill_md_paths[0]
        content = zf.read(skill_md_path).decode("utf-8")

    frontmatter = _parse_frontmatter(content)
    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")

    if not name:
        msg = "SKILL.md frontmatter missing required 'name' field"
        raise ValueError(msg)
    if not description:
        msg = "SKILL.md frontmatter missing required 'description' field"
        raise ValueError(msg)

    _validate_name(name)

    rel_dir = str(Path(skill_md_path).parent)
    if rel_dir == ".":
        rel_dir = ""

    return name, description, rel_dir


def install_skill(
    zip_path: str | Path,
    tags: list[str] | None = None,
) -> SkillInfo:
    """Validate, extract to ~/.claude/skills/{name}/, record metadata."""
    zip_path = Path(zip_path).resolve()
    name, description, rel_dir = validate_skill_zip(zip_path)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extractall(zf, tmp_path)

        source_dir = tmp_path / rel_dir if rel_dir else tmp_path
        target_dir = _SKILLS_DIR / name

        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, target_dir)

    now = datetime.now(timezone.utc).isoformat()
    skill_tags = tags or []
    save_skill_metadata(
        name=name,
        description=description,
        source=str(zip_path),
        installed_at=now,
        tags=skill_tags,
    )

    return SkillInfo(
        name=name,
        description=description,
        installed_at=now,
        source=str(zip_path),
        tags=skill_tags,
    )


def remove_skill(name: str) -> bool:
    """Remove ~/.claude/skills/{name}/ directory + config entry."""
    _validate_name(name)
    target_dir = _SKILLS_DIR / name
    removed_dir = False
    if target_dir.exists():
        shutil.rmtree(target_dir)
        removed_dir = True
    removed_config = remove_skill_metadata(name)
    return removed_dir or removed_config


def list_skills() -> list[SkillInfo]:
    """Read config metadata and return all installed skills."""
    config = get_skills_config()
    return [
        SkillInfo(
            name=name,
            description=entry.get("description", ""),
            installed_at=entry.get("installed_at", ""),
            source=entry.get("source", ""),
            tags=entry.get("tags", []),
        )
        for name, entry in config.items()
        if isinstance(entry, dict)
    ]


def get_skill(name: str) -> SkillInfo | None:
    """Get a single skill by name."""
    _validate_name(name)
    config = get_skills_config()
    entry = config.get(name)
    if not isinstance(entry, dict):
        return None
    return SkillInfo(
        name=name,
        description=entry.get("description", ""),
        installed_at=entry.get("installed_at", ""),
        source=entry.get("source", ""),
        tags=entry.get("tags", []),
    )


def get_skills_by_tag(tag: str) -> list[SkillInfo]:
    """Filter installed skills by tag."""
    return [s for s in list_skills() if tag in s.tags]


def has_installed_skills() -> bool:
    """Check if any skills are installed."""
    return bool(get_skills_config())


_BUILTIN_SKILL_DATA = Path(__file__).resolve().parent / "data" / "skills"

_AGENT_BROWSER_SKILL_TIMEOUT = 10

_ARTIFACT_DIR = ".leashd"


def _agent_browser_cli_output(*args: str) -> str | None:
    """Run ``agent-browser <args>`` and return stripped stdout, or None."""
    exe = shutil.which("agent-browser")
    if exe is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=_AGENT_BROWSER_SKILL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _agent_browser_skill_source() -> tuple[Path, str]:
    """Resolve the agent-browser skill to install and the version it came from.

    Prefers the installed CLI's own ``core`` skill, which ships version-matched
    with the binary — the vendored copy under ``data/skills`` is a snapshot and
    drifts as agent-browser adds commands. Falls back to the vendored copy when
    the CLI is absent or its layout is unrecognized.
    """
    path_out = _agent_browser_cli_output("skills", "path", "core")
    if path_out:
        candidate = Path(path_out.splitlines()[0].strip())
        if (candidate / "SKILL.md").is_file():
            version = _agent_browser_cli_output("--version") or "unknown"
            return candidate, version.replace("agent-browser", "").strip() or "unknown"
    return _BUILTIN_SKILL_DATA / "agent-browser", "builtin"


def _normalize_skill_name(skill_md: Path, name: str) -> None:
    """Rewrite the frontmatter ``name:`` so it matches the install directory.

    The CLI's copy is named ``core`` (it is one of several skills it ships);
    installed under ``~/.claude/skills/agent-browser`` that leaves the
    frontmatter disagreeing with the directory and the recorded metadata key.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return
        if lines[i].startswith("name:"):
            lines[i] = f"name: {name}"
            skill_md.write_text("\n".join(lines), encoding="utf-8")
            return


_ARTIFACT_PATH_REWRITES: tuple[tuple[str, str], ...] = (
    ("/tmp/", f"{_ARTIFACT_DIR}/"),  # noqa: S108
    ('OUTPUT_DIR="${2:-.}"', f'OUTPUT_DIR="${{2:-{_ARTIFACT_DIR}}}"'),
)

_ARTIFACT_REWRITE_SUFFIXES: frozenset[str] = frozenset({".md", ".sh"})

MAX_BROWSER_TABS = 15

_TAB_DISCIPLINE_MARKER = "## One browser, many tabs"

_TAB_DISCIPLINE_SECTION = f"""
{_TAB_DISCIPLINE_MARKER}

leashd runs agent-browser against a shared persistent profile, and when
`browser.headless` is false every browser launch is a real window on the user's
screen. Treat the browser as one long-lived session:

- Use `agent-browser open <url>` for the first page only.
- For every page after that use `agent-browser tab new <url>`, switch with
  `agent-browser tab <id|label>`, and drop one with `agent-browser tab close <id|label>`.
- Keep at most {MAX_BROWSER_TABS} tabs open at once. Close a tab before opening
  the next one beyond that.
- Never call `agent-browser close` between pages. Each relaunch leaves its
  windows behind in the profile's session state, and the next launch restores
  every one of them — this is how a single run ends up with hundreds of windows.
- Reserve `--session <name>` for genuinely separate identities, such as a
  two-user auth test. Each session is a whole extra browser, so never use
  sessions to fan out over a URL list.

To visit many URLs, reuse the one browser and cycle tabs:

```bash
n=0
while IFS= read -r url; do
  agent-browser tab new --label "p$n" "$url" >/dev/null
  agent-browser get text body
  agent-browser tab close "p$n" >/dev/null
  n=$((n+1))
done < urls.txt
```

To overlap page loads, open up to {MAX_BROWSER_TABS} with `tab new` first, then
read and close them — never more than {MAX_BROWSER_TABS} open at a time.
"""


_SEARCH_ENGINE_MARKER = "## Default search engine"

_SEARCH_ENGINE_SECTION = f"""
{_SEARCH_ENGINE_MARKER}

Search with Google unless it is unavailable. Upstream's worked example above
opens `duckduckgo.com`, which is why agents reach for it first, but Google
carries the operators research tasks depend on:

```bash
agent-browser tab new --label q "https://www.google.com/search?q=<query>&num=20"
```

- `&num=20` returns more results per page, so fewer round trips.
- `site:`, `OR`, `-exclude`, and quoted phrases behave as documented; DuckDuckGo
  degrades several of them silently.
- Fall back to `duckduckgo.com/?q=` or `bing.com/search?q=` only when Google is
  blocked, rate-limited, or returns no parseable results — say which engine
  produced a result when it was not Google.

Search pages count against the {MAX_BROWSER_TABS}-tab budget like any other
page: reuse one labelled search tab rather than opening a fresh one per query.
"""


SEARCH_SCRIPT_NAME = "search-batch.sh"

_SEARCH_SCRIPT_RELPATH = f"templates/{SEARCH_SCRIPT_NAME}"

_SEARCH_SCRIPT_SOURCE = _BUILTIN_SKILL_DATA / SEARCH_SCRIPT_NAME

_BATCHED_SEARCH_MARKER = "## Batched searches"

_BATCHED_SEARCH_SECTION = f"""
{_BATCHED_SEARCH_MARKER}

Google answers a burst of result-page requests with its `/sorry/index`
interstitial. Measured against leashd's persistent profile: 15 tabs opened
back-to-back lost 2 pages, the same 15 queries run one tab at a time lost none,
and a throwaway browser with no profile was refused on its very first request.
So the profile is what makes searching work at all, and the arrival rate is what
gets a run cut off part-way.

Run multi-query research through the shipped helper rather than a hand-rolled
`for` loop over `tab new`:

```bash
~/.claude/skills/agent-browser/{_SEARCH_SCRIPT_RELPATH} \\
  -n 4 -j 2:5 -c 10 "first query" "second query" "third query"
~/.claude/skills/agent-browser/{_SEARCH_SCRIPT_RELPATH} -f queries.txt
```

It holds `-n` tabs in flight, waits a random `-j min:max` seconds between opens,
prints `title<TAB>url` per query, and closes each tab as soon as it is read. On a
`/sorry` page it backs off and retries once, then falls back to DuckDuckGo and
Bing, unwrapping both engines' redirect URLs. It never calls
`agent-browser close`.
"""


def _upsert_section(root: Path, marker: str, section: str) -> bool:
    """Insert or refresh one of leashd's appended SKILL.md sections.

    Replaces an existing section in place — from its ``##`` marker up to the
    next same-level heading — so edits to the wording reach skills that are
    already installed. A plain "append when the marker is absent" check would
    leave every existing install pinned to whatever text shipped first.

    Returns True when the file changed.
    """
    skill_md = root / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    start = text.find(marker)
    if start == -1:
        updated = f"{text.rstrip()}\n{section}"
    else:
        end = text.find("\n## ", start + len(marker))
        updated = f"{text[:start].rstrip()}\n{section}"
        if end != -1:
            updated = f"{updated}\n{text[end + 1 :]}"
    if updated == text:
        return False
    skill_md.write_text(updated, encoding="utf-8")
    return True


def _install_search_script(root: Path) -> bool:
    """Copy leashd's batched-search helper into the skill's templates directory.

    Shipped as a script rather than a documented loop because the loop agents
    write themselves opens every tab at once, which is what Google throttles.
    Returns True when the file was written or its contents changed.
    """
    if not _SEARCH_SCRIPT_SOURCE.is_file():
        return False
    target = root / _SEARCH_SCRIPT_RELPATH
    try:
        source_text = _SEARCH_SCRIPT_SOURCE.read_text(encoding="utf-8")
        if target.is_file() and target.read_text(encoding="utf-8") == source_text:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source_text, encoding="utf-8")
        target.chmod(0o755)
    except OSError:
        return False
    return True


def _apply_customisations(root: Path) -> tuple[tuple[str, bool], ...]:
    """Apply every leashd customisation to an installed agent-browser skill.

    Ordered so the appended sections land in reading order. Each entry reports
    whether it changed anything, so callers can log only real edits.
    """
    return (
        (
            "tab_discipline",
            _upsert_section(root, _TAB_DISCIPLINE_MARKER, _TAB_DISCIPLINE_SECTION),
        ),
        (
            "search_engine",
            _upsert_section(root, _SEARCH_ENGINE_MARKER, _SEARCH_ENGINE_SECTION),
        ),
        (
            "batched_search",
            _upsert_section(root, _BATCHED_SEARCH_MARKER, _BATCHED_SEARCH_SECTION),
        ),
        ("search_script", _install_search_script(root)),
    )


def _normalize_artifact_paths(root: Path) -> int:
    """Point the skill's example output paths at ``.leashd``.

    Upstream's examples write screenshots, HAR traces, and saved state to
    ``/tmp``. An agent that follows them puts its evidence somewhere temp
    cleanup reclaims — and outside the sandboxed project directory. Applied on
    every install so the redirect survives a CLI upgrade rather than living as
    a hand-patch in the vendored copy, which the next sync would overwrite.

    Returns the number of files rewritten.
    """
    rewritten = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in _ARTIFACT_REWRITE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        updated = text
        for old, new in _ARTIFACT_PATH_REWRITES:
            updated = updated.replace(old, new)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            rewritten += 1
    return rewritten


def ensure_agent_browser_skill() -> None:
    """Install (or refresh) the agent-browser skill.

    Re-copies whenever the resolved source version differs from what is
    recorded in skill metadata, so a ``brew upgrade agent-browser`` propagates
    the matching guide instead of leaving agents on a stale command surface.
    """
    source, version = _agent_browser_skill_source()
    target = _SKILLS_DIR / "agent-browser"
    installed = get_skill("agent-browser")
    expected_source = f"agent-browser@{version}"
    if (
        (target / "SKILL.md").is_file()
        and installed is not None
        and installed.source == expected_source
    ):
        backfilled = [
            name for name, applied in _apply_customisations(target) if applied
        ]
        if backfilled:
            logger.info(
                "agent_browser_skill_customisations_backfilled", added=backfilled
            )
        return
    if not (source / "SKILL.md").is_file():
        logger.warning("agent_browser_skill_source_missing", source=str(source))
        return
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    _normalize_skill_name(target / "SKILL.md", "agent-browser")
    _normalize_artifact_paths(target)
    _apply_customisations(target)
    save_skill_metadata(
        name="agent-browser",
        description="Browser automation CLI for AI agents",
        source=expected_source,
        installed_at=datetime.now(timezone.utc).isoformat(),
        tags=["browser"],
    )
    logger.info("agent_browser_skill_installed", version=version, source=str(source))


def remove_agent_browser_skill() -> None:
    """Remove the builtin agent-browser skill."""
    target = _SKILLS_DIR / "agent-browser"
    if target.exists():
        shutil.rmtree(target)
    remove_skill_metadata("agent-browser")
