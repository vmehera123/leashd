"""Adversarial soak for the /model bridge: pick → verify → repeat.

Requires a running tmux_harness. Each iteration runs /model, taps a random
option, then asks claude which model it is on and checks the answer names
the picked model. A third of iterations race the probe message right behind
the tap. Any hang, unanswered probe, or wrong-model answer is a failure and
dumps the pane. Exit code = number of failures.

  uv run python scripts/_harness/soak_model.py [iterations]
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
import urllib.request

TG = f"http://127.0.0.1:{os.environ.get('TG_PORT', '8091')}"
SOCK = os.environ.get("HARNESS_DIR", "/tmp/leashd_tmux_harness") + "/tmux/tmux.sock"

EXPECTED = {
    "Default": "Opus",
    "Opus": "Opus",
    "Fable": "Fable",
    "Sonnet": "Sonnet",
    "Haiku": "Haiku",
}
EXPECTED_MODEL_ID = {
    "Default": "opus",
    "Opus": "opus",
    "Fable": "fable",
    "Sonnet": "sonnet",
    "Haiku": "haiku",
}
APPROVED_DIR = os.environ.get("APPROVED_DIR", "/Users/vmehera/projects/trend-explorer")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects/") + APPROVED_DIR.replace(
    "/", "-"
)


def actual_model() -> str | None:
    import glob

    files = sorted(
        glob.glob(PROJECTS_DIR + "/*.jsonl"), key=os.path.getmtime, reverse=True
    )
    for path in files[:1]:
        model = None
        for line in open(path):
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") == "assistant":
                model = o.get("message", {}).get("model") or model
        return model
    return None


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        TG + path,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def state() -> dict:
    return json.loads(urllib.request.urlopen(TG + "/control/state", timeout=10).read())


def pane_dump() -> str:
    try:
        name = subprocess.run(
            ["tmux", "-S", SOCK, "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.split()[0]
        return subprocess.run(
            ["tmux", "-S", SOCK, "capture-pane", "-p", "-t", name],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception as exc:
        return f"(pane capture failed: {exc})"


def wait_for(predicate, timeout: float, poll: float = 1.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll)
    return None


def newest_buttons(after_mid: int) -> tuple[int, list[dict]] | None:
    d = state()
    fresh = [int(k) for k in d["buttons"] if int(k) > after_mid]
    if not fresh:
        return None
    mid = max(fresh)
    return mid, d["buttons"][str(mid)]


def bot_reply_after(mid: int, needles: list[str]) -> str | None:
    d = state()
    for k in sorted(d["messages"], key=int):
        if int(k) > mid:
            text = d["messages"][k]
            if text.startswith(("🖥", "**", "⏳", "/")):
                continue
            if any(n.lower() in text.lower() for n in needles):
                return text
    return None


def run_iteration(i: int, race: bool) -> str | None:
    found = None
    for _ in range(2):
        top = max([int(k) for k in state()["messages"]] or [0])
        post("/control/inject_command", {"command": "model", "args": ""})
        found = wait_for(lambda t=top: newest_buttons(t), 90)
        if found:
            break
        busy = any("busy" in v for k, v in state()["messages"].items() if int(k) > top)
        if not busy:
            return "no buttons within 90s"
        time.sleep(30)
    if not found:
        return "no buttons after busy retry"
    qmid, buttons = found

    idx = random.randrange(len(buttons))
    label = buttons[idx]["text"].split()[0]
    post("/control/tap", {"message_id": qmid, "data": buttons[idx]["callback_data"]})

    if not race:
        time.sleep(8)
    probe = post(
        "/control/inject_message",
        {"text": "Which model are you right now? Reply with just the model name."},
    )
    probe_mid = probe["message_id"]

    answer = wait_for(lambda: bot_reply_after(probe_mid, list(EXPECTED.values())), 120)
    if answer is None:
        return f"probe unanswered in 120s (picked {label})"
    time.sleep(3)
    model = actual_model()
    want = EXPECTED_MODEL_ID.get(label, label.lower())
    if not model or want not in model:
        return (
            f"picked {label} but ground-truth model is {model!r} "
            f"(claude's own answer: {answer.strip()[:60]!r})"
        )
    print(
        f"  [{i}] picked {label:8} race={race} -> model={model} "
        f"(claude says {answer.strip()[:40]!r})"
    )
    return None


def main() -> int:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    random.seed(int(os.environ.get("SOAK_SEED", "7")))
    failures = 0
    for i in range(1, iterations + 1):
        race = i % 3 == 0
        try:
            problem = run_iteration(i, race)
        except Exception as exc:
            problem = f"driver exception: {exc}"
        if problem:
            failures += 1
            print(f"  [{i}] FAIL: {problem}")
            print("  --- pane at failure ---")
            print("\n".join(pane_dump().splitlines()[-25:]))
        time.sleep(4)
    print(f"soak done: {iterations - failures}/{iterations} ok, {failures} failures")
    return failures


if __name__ == "__main__":
    sys.exit(main())
