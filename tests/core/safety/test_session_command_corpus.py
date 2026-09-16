import json
import re
from pathlib import Path

import pytest

from leashd.core.safety.analyzer import command_units, strip_benign_prefixes
from leashd.core.safety.policy import PolicyEngine

POLICIES = Path(__file__).parent.parent.parent.parent / "leashd" / "policies"
CORPUS = json.loads(
    (Path(__file__).parent / "data" / "session_command_corpus.json").read_text()
)

DESTRUCTIVE_HEADS = frozenset(
    {
        "rm",
        "rmdir",
        "mv",
        "dd",
        "shred",
        "truncate",
        "chmod",
        "chown",
        "kill",
        "pkill",
        "killall",
        "sudo",
        "mkfs",
        "bash",
        "sh",
        "zsh",
        "eval",
        "source",
        ".",
    }
)
GIT_WRITES = frozenset(
    {"push", "reset", "clean", "checkout", "restore", "rebase", "merge", "commit"}
)
DOCKER_WRITES = frozenset(
    {"rm", "rmi", "run", "stop", "restart", "kill", "prune", "down", "up", "build"}
)


@pytest.fixture(scope="module")
def engine():
    return PolicyEngine([POLICIES / "default.yaml", POLICIES / "dev-tools.yaml"])


def _destructive_units(command: str) -> list[str]:
    found = []
    for text, kind in command_units(command):
        if kind == "pipeline":
            continue
        tokens = strip_benign_prefixes(text).lstrip("({ ").split()
        if not tokens:
            continue
        head, rest = tokens[0], set(tokens[1:4])
        if (
            head in DESTRUCTIVE_HEADS
            or (head == "git" and GIT_WRITES & rest)
            or (head in ("docker", "podman") and DOCKER_WRITES & rest)
        ):
            found.append(text)
    return found


def test_the_corpus_covers_every_kind_of_change():
    transitions = {(entry["before"], entry["verdict"]) for entry in CORPUS}
    assert ("require_approval", "allow") in transitions
    assert ("allow", "require_approval") in transitions
    assert ("allow", "allow") in transitions
    assert ("require_approval", "require_approval") in transitions


def test_the_corpus_carries_no_local_setup():
    for entry in CORPUS:
        command = entry["command"]
        assert command.isascii(), command
        assert not re.search(r"/Users/|/home/(?!dev\b)|/private/", command), command
        assert not re.search(r"\b(?!0+\d{0,2}\b)[0-9a-f]{12,}\b", command), command
        for address in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", command):
            assert re.fullmatch(r"127\.0\.0\.1|0\.0\.0\.0|203\.0\.113\.\d+", address), (
                command
            )


@pytest.mark.parametrize("entry", CORPUS, ids=[f"cmd{i}" for i in range(len(CORPUS))])
def test_command_keeps_its_reviewed_verdict(engine, entry):
    classification = engine.classify_compound("Bash", {"command": entry["command"]})
    assert (engine.evaluate(classification).value, classification.category) == (
        entry["verdict"],
        entry["category"],
    ), entry["command"]


@pytest.mark.parametrize(
    "entry",
    [entry for entry in CORPUS if entry["verdict"] == "allow"],
    ids=lambda entry: entry["command"][:40],
)
def test_nothing_allowed_runs_a_destructive_command(entry):
    assert _destructive_units(entry["command"]) == [], entry["command"]
