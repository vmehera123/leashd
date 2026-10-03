---
name: release-prep
description: Prepare leashd for release. Picks the right semver bump, tidies CHANGELOG.md down to short highlight entries, brings README.md, docs/ and specs/ in line with the code, syncs the version across pyproject.toml, leashd/__init__.py and uv.lock, and runs what the GitHub pipeline runs (lint, mypy, tests with the coverage gate, E2E, package build) so CI will pass. Use whenever the user says "prepare leashd for release", "prep the release", "get ready to release" or asks to cut or bump a version.
argument-hint: "[version, to skip the semver decision]"
---

# Prepare leashd for release

Get the working tree ready for a release commit: $ARGUMENTS

This skill prepares, it does not ship. Never commit, tag, push or create the GitHub release (publishing a GitHub release triggers `.github/workflows/publish.yml`, which uploads to PyPI). Finish with a report and a suggested commit message, and let the user do the rest.

Work through the steps in order. Each one depends on the scope found in step 1.

## 1. Find what is being released

The last release is the newest commit whose subject starts with `Release version X.Y.Z`. Tags (`vX.Y.Z`) can lag behind, so trust the commit.

```bash
git log --grep='^Release version' -1 --format='%h %s' | cut -c1-60
git log <last-release>..HEAD --oneline
git diff <last-release> --stat          # commits + staged + unstaged
git status --short
```

Everything after that commit is unreleased, including staged and unstaged work. Read the diff of `leashd/` (not just the stat) well enough to say, in user terms, what changed: new commands, new config, changed defaults, removed things, bug fixes. This list drives every later step, so do not build it from the existing changelog entries alone. They can be missing things or describe work that was later reverted.

Never `git stash` here. The user keeps a deliberate staged/unstaged split and stash flattens it.

## 2. Choose the version

Classify the unreleased changes against the last released version `X.Y.Z`:

| Bump | When |
|---|---|
| Patch `X.Y.Z+1` | Bug fixes only. Nothing a user could newly do, nothing they must change. |
| Minor `X.Y+1.0` | Any new user-facing feature (command, flag, config key, connector behaviour, policy capability), with or without fixes. Small breaking changes fit here too: a changed default, a renamed option, a prompt that now asks where it did not. |
| Major `X+1.0.0` | A new architecture or large refactor, a removed runtime or subsystem, or breaking changes that force users to redo their setup. |

The highest category present wins. One `added` entry among ten fixes is a minor release.

If the top `CHANGELOG.md` heading is a version that was never released, it is the working heading for this release: rename it in place to the version you chose instead of adding another heading. If unreleased work was split across two headings above the last release, merge them into one.

If `$ARGUMENTS` names a version, use it, but say so plainly if the changes call for a different bump.

## 3. Tidy the changelog

Rewrite the section for this release, and leave released sections alone.

- One bullet per change, `- **added|fixed|changed|removed**: …`, 1 to 2 sentences, no sub-bullets or headings.
- Aim for 3 to 6 bullets. Merge related items (five approval-prompt fixes become one bullet) and drop the minor ones.
- Keep only what a user would notice: features, behaviour changes, fixes for bugs that shipped in an earlier release.
- Delete any fix for a bug that was introduced and fixed inside this same unreleased version. No user ever saw it, so it folds into the feature's bullet or disappears.
- Say what the user gets, in plain words. Cut helper names, constants, file names, refactors, forensics and the story of how the bug was found.
- Order: `removed` and breaking `changed` first on a major release, otherwise `added`, `changed`, `fixed`.
- Set the heading date to today: `## [X.Y.Z] - YYYY-MM-DD`.

To tell a shipped bug from an unreleased one, check whether the broken code exists at the last release commit: `git show <last-release>:path/to/file | grep …`.

## 4. Bring the docs up to date

Everything in `README.md`, `docs/*.md` and `specs/app/` (gitignored, local only, skip it if absent) must be true of the code being released. The source wins over every document: `leashd --help` and `leashd <subcommand> --help`, `leashd/core/config.py`, `leashd/policies/*.yaml`, `plugins/registry.py`, `agents/registry.py`.

1. For each change from step 1, grep the docs for the feature, command, config key or default it touched, and fix every mention. Add documentation for new features in the doc that owns that subsystem (`docs/index.md` lists them).
2. Then check the other direction, because docs rot without anyone touching them. Verify against the source, not from memory:
   - every CLI command and flag in the docs exists in `--help`, and every command in `--help` is documented in `docs/cli.md`
   - every slash command in the README's Commands table is registered, and none is missing
   - every config key and default quoted in the docs matches `leashd/core/config.py`
   - every file path, module name and policy file named in the docs exists
   - version requirements (Python, Claude Code minimum version) match `pyproject.toml` and the runtime's check
   - nothing describes deleted code (old runtimes, old task orchestrators, `/test`, `AutoApprover`)
3. Grep for stale version strings: `grep -rn "<old version>" README.md docs/`.

For a large release, fan the per-file verification out to subagents (one per doc or group of docs), each told to report only claims it checked against source and found wrong.

### README

The README is the product page, so on a minor or major release it has to show what the release adds, not just avoid being wrong.

- A headline feature gets a place where a new reader will see it: in "Why leashd" if it is a reason to pick leashd, and in the section where it is used, with a short runnable example.
- Lead with what the user can do and how, then the detail. Show the real command and what happens after it.
- A major release means rereading the whole README top to bottom as a newcomer: the pitch, install and quick start must describe the product as it is now.
- A patch release normally needs no README change beyond corrections.

### Writing style

Write for a person reading it once. Short sentences, plain words, active voice, concrete examples over adjectives. No em dashes, no marketing filler ("powerful", "seamless", "robust"), no "it's not X, it's Y" constructions. Keep the existing structure and tone of each file, and do not pad a doc to make a change look bigger.

## 5. Sync the version

The version lives in exactly three places and all must match the changelog heading:

- `pyproject.toml`: `version = "X.Y.Z"`
- `leashd/__init__.py`: `__version__ = "X.Y.Z"`
- `uv.lock`: the `leashd` package entry. Never edit it by hand, run `uv lock` after changing `pyproject.toml`.

Verify:

```bash
grep -m1 '^## \[' CHANGELOG.md
grep '^version' pyproject.toml
grep '__version__' leashd/__init__.py
grep -A1 '^name = "leashd"$' uv.lock
uv lock --check
```

All four must print the same version and `uv lock --check` must exit 0. `uv lock` should change only the leashd version line in `uv.lock`. If it changes anything else, say so in the report.

## 6. Verify

The release must pass the GitHub pipeline, so run what CI runs, not just `make check`. Read `.github/workflows/ci.yml` and `publish.yml` first and treat them as the source of truth: if a command or threshold there differs from what is written below, follow the workflow file.

`make check` is weaker than CI in four ways: it reformats instead of checking the format, it swallows mypy errors (`|| true`), it does not measure coverage, and it skips E2E. So for a release, run the CI commands directly, each with its output sent to a file in the scratchpad:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy leashd/
uv run pytest tests/ -m "not e2e" --cov=leashd --cov-fail-under=89 --cov-report=term-missing
uv run pytest tests/ -m e2e -v
uv build --out-dir <scratchpad>/dist && uvx twine check <scratchpad>/dist/*
```

- **Coverage is a gate.** CI fails the test job below the `--cov-fail-under` value (89%, also `fail_under` in `pyproject.toml`). Read the `TOTAL` line and report the percentage. If it is under the threshold, or within about half a point of it, use the `term-missing` output to find the uncovered lines in the code this release added and write tests for them. Never lower the threshold or add `# pragma: no cover` to get through.
- The coverage run is the full unit suite, so it replaces `make check`'s test run. Run it once and grep the log (`passed|failed`, `TOTAL`).
- mypy must be clean. CI has no `|| true`.
- E2E always runs in CI, so run it here whatever the release touched (`uv run playwright install chromium` once if Chromium is missing).
- CI runs the unit tests on Python 3.10, 3.11, 3.12 and 3.13, while the local environment is 3.13. Check the release diff for 3.11+ APIs (`asyncio.timeout`, `asyncio.TaskGroup`, `except*`, `tomllib`, `typing.Self`, `StrEnum`, bare `TimeoutError` where `asyncio.TimeoutError` is raised).
- The build and `twine check` mirror the publish workflow, which runs only after the GitHub release is published, when a failure is most awkward.

Then look at the pipeline itself:

```bash
gh run list --branch main --limit 5
gh run view <id> --log-failed
```

If the latest run on `main` is red, find out why and fix it as part of this prep, since the release commit will inherit the failure. The unreleased work may not be pushed yet, so a green run on `main` does not prove the release passes. The local commands above do.

Fix what fails before reporting.

## 7. Report

End with a short report:

- the version chosen and the one-line reason for that bump
- the final changelog section, quoted
- what changed in the docs, and anything found wrong in them that was not caused by this release
- the version check output, and the result of each CI command with the coverage percentage against the threshold and the state of the latest run on `main`
- anything left for the user to decide
- a suggested commit subject in the repo's style: `Release version X.Y.Z: <one sentence summary>`

Then stop. Committing, tagging `vX.Y.Z`, pushing and publishing the GitHub release are the user's calls.
