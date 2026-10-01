"""Live multi-conversation scenarios against the tmux+Telegram harness.

Replays what a person actually does with ``/session`` — open a second
conversation, walk away from a running one, come back mid-answer, answer a
question raised by the one they are not looking at — and asserts on the exact
outbound Bot API timeline the harness records. Everything here runs against a
real Engine, a real ``claude`` pane and the real connector; nothing is mocked.

Start the harness first (see the ``telegram-harness`` skill), then:

    uv run python scripts/_harness/multisession_suite.py           # all
    uv run python scripts/_harness/multisession_suite.py s2 s5     # a subset
    uv run python scripts/_harness/multisession_suite.py --list

Exit status is the number of failed scenarios.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

TG = f"http://127.0.0.1:{os.environ.get('TG_PORT', '8091')}"
CHAT = os.environ.get("CHAT_ID", "284184690")
DB = Path(os.environ.get("HARNESS_DIR", "/tmp/leashd_tmux_harness")) / "messages.db"
APP_LOG = Path(
    os.environ.get(
        "APP_LOG", f"{os.environ.get('APPROVED_DIR', '')}/.leashd/logs/app.log"
    )
)

CURSOR = "▍"
TURN_TIMEOUT = float(os.environ.get("TURN_TIMEOUT", "240"))

LINES = 24
AWAY_SECONDS = float(os.environ.get("AWAY_SECONDS", "12"))
SHORT_AWAY_SECONDS = float(os.environ.get("SHORT_AWAY_SECONDS", "5"))
FOOTER_SLACK = 40
STEPPED = (
    "You must produce THREE SEPARATE assistant messages, not one. "
    "Message 1: eight numbered lines on why {topic}, one full sentence each. "
    "Then call Glob once for '*.md'. "
    "Message 2: eight more numbered lines, continuing 9-16. "
    "Then call Glob once for '*'. "
    "Message 3: eight more numbered lines, 17-24. "
    "Never combine the three messages."
)
ASK = (
    "Use the AskUserQuestion tool right now, before anything else, to ask me "
    "one question: 'Tabs or spaces?' with the two options 'Tabs' and 'Spaces'. "
    "Then stop and report what I chose."
)
ANSWER_MARKER = "INDENTATION VERDICT"
ASK_THEN_WORK = (
    "Do this in TWO separate assistant messages. Message 1: six numbered "
    "lines, one full sentence each, on why house style should be agreed "
    "before writing code. Then call Glob once for '*.md'. Message 2: use the "
    "AskUserQuestion tool in ONE call "
    "carrying THREE questions: 'Tabs or spaces?' (options 'Tabs', 'Spaces'), "
    "'Line length?' (options '80', '100') and 'Quotes?' (options 'Single', "
    "'Double'). Do nothing else until I answer all three. After I answer, call "
    f"Glob once for '*.md', then reply with exactly one line: "
    f"'{ANSWER_MARKER}: <what I chose for indentation>'."
)
FREE_ANSWER = "use whatever the repo already does"
IDLE_GRACE = float(os.environ.get("LEASHD_TMUX_COMPLETION_IDLE_GRACE_SECONDS", "45"))
HELD_SECONDS = float(os.environ.get("HELD_SECONDS", str(IDLE_GRACE + 10)))
REPLAY_CEILING = 3000
LONG = (
    "Write 30 numbered lines on why code review catches what tests cannot. "
    "Every line must be a full sentence of at least 180 characters. "
    "Output the lines and nothing else — no preamble, no closing paragraph."
)
SHORT = (
    "Reply with exactly four numbered lines on why small pull requests are "
    "easier to review, one full sentence each, and nothing else."
)


# --- harness control plane -------------------------------------------------


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        TG + path,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def _get(path: str) -> dict:
    return json.loads(urllib.request.urlopen(TG + path, timeout=30).read())


def msg(text: str) -> None:
    _post("/control/inject_message", {"text": text})


def cmd(command: str, args: str = "") -> None:
    _post("/control/inject_command", {"command": command, "args": args})


def tap(message_id: int, data: str) -> None:
    _post("/control/tap", {"message_id": message_id, "data": data})


def calls(since: int = 0) -> list[dict]:
    return _get(f"/control/calls?since={since}")["calls"]


def call_count() -> int:
    return _get("/control/state")["total_calls"]


def sessions() -> dict:
    return _get("/control/sessions")


def texts(since: int = 0, methods: tuple[str, ...] = ("sendMessage",)) -> list[str]:
    return [
        c["data"].get("text", "")
        for c in calls(since)
        if c["method"] in methods and c["data"].get("text")
    ]


def streamed(since: int = 0) -> list[dict]:
    """Every call that put a live, still-being-written message on screen."""
    return [
        c
        for c in calls(since)
        if c["method"] in ("sendMessage", "editMessageText")
        and CURSOR in c["data"].get("text", "")
    ]


def index_of_text(since: int, needle: str) -> int | None:
    for c in calls(since):
        if needle in c["data"].get("text", ""):
            return c["seq"]
    return None


def numbered_lines(text: str) -> int:
    return sum(1 for n in range(1, LINES + 1) if f"{n}." in text or f"{n})" in text)


def buttoned(since: int = 0) -> list[tuple[int, str]]:
    """(message_id, text) for every prompt the chat was given buttons for."""
    return [
        (c["message_id"], c["data"].get("text", ""))
        for c in calls(since)
        if c["method"] == "sendMessage" and c["data"].get("reply_markup")
    ]


def button_data(message_id: int) -> list[str]:
    rows = _get(f"/control/buttons?message_id={message_id}").get("buttons", [])
    return [b["callback_data"] for b in rows]


def deleted(since: int = 0) -> set[int]:
    return {
        int(c["data"]["message_id"])
        for c in calls(since)
        if c["method"] == "deleteMessage"
    }


# --- waiting ---------------------------------------------------------------


def _log_lines() -> list[dict]:
    if not APP_LOG.exists():
        return []
    out = []
    for line in APP_LOG.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def log_events(since_iso: str, *names: str) -> list[dict]:
    return [
        o
        for o in _log_lines()
        if o.get("timestamp", "") >= since_iso and o.get("event") in names
    ]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def wait_for(predicate, timeout: float, what: str, poll: float = 0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


def wait_streaming(since: int, timeout: float = 180) -> dict:
    """Block until a live stream (cursor) is visibly on screen."""
    return wait_for(
        lambda: next(iter(streamed(since)), None),
        timeout,
        "the stream to appear on screen",
    )


def wait_foreground(chat_id: str, timeout: float = 30) -> None:
    wait_for(
        lambda: sessions().get("foreground") == chat_id,
        timeout,
        f"the chat to attach to {chat_id}",
    )


def wait_turn_done(since_iso: str, chat_id: str, timeout: float = TURN_TIMEOUT) -> dict:
    def _done():
        for o in log_events(since_iso, "request_completed"):
            if o.get("chat_id") == chat_id:
                return o
        return None

    return wait_for(_done, timeout, f"the turn in {chat_id} to finish")


def wait_question(since: int, timeout: float = TURN_TIMEOUT) -> tuple[int, str]:
    return wait_for(
        lambda: next(iter(buttoned(since)), None),
        timeout,
        "a question with buttons",
    )


def agent_reply_length(since_iso: str, chat_id: str) -> int:
    """What the agent actually produced, straight from the turn's own record.

    The completeness checks compare this against what leashd stored, so they
    test the thing that broke — a stored fragment of a longer reply — rather than
    how many lines the model felt like writing this time.
    """
    done = [
        o
        for o in log_events(since_iso, "request_completed")
        if o.get("chat_id") == chat_id
    ]
    return int(done[-1].get("response_length", 0)) if done else -1


def check_reply_intact(since_iso: str, chat_id: str, label: str) -> None:
    """The stored reply must be the whole thing the agent wrote.

    The tmux runtime appends a ``🧰 Glob x2`` tool footer to the reply it
    returns but not to the text it streams, and the engine stores the streamed
    text — so the stored copy is legitimately shorter by that footer and no
    more. The bug this guards against lost 40% of a reply, so the slack is
    nowhere near wide enough to hide one.
    """
    produced = agent_reply_length(since_iso, chat_id)
    stored = len(stored_reply(chat_id))
    missing = produced - stored
    check(
        produced > 0 and 0 <= missing <= FOOTER_SLACK,
        f"{label} kept every word the agent wrote ({stored}/{produced} chars)",
    )


def reply_count(chat_id: str) -> int:
    with sqlite3.connect(DB) as db:
        row = db.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ? AND role = 'assistant'",
            (chat_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def stored_reply(chat_id: str) -> str:
    with sqlite3.connect(DB) as db:
        row = db.execute(
            "SELECT content FROM messages WHERE chat_id = ? AND role = 'assistant' "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    return row[0] if row else ""


def slot(index: int) -> str:
    return CHAT if index == 1 else f"{CHAT}:s{index}"


def here() -> str:
    """The directory name of the conversation on screen.

    ``/session new <name>`` takes the same names the directory picker offers,
    and naming one is how a scenario opens a conversation without the picker
    landing in the chat.
    """
    roster = sessions()
    for row in roster["slots"]:
        if row["chat_id"] == roster["foreground"]:
            return str(row["directory"])
    return str(roster["slots"][0]["directory"])


# --- assertions ------------------------------------------------------------


class CheckError(AssertionError):
    pass


def check(ok: bool, label: str) -> None:
    print(f"    {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        raise CheckError(label)


# --- scenarios -------------------------------------------------------------

SCENARIOS: dict[str, tuple[str, object]] = {}


def scenario(key: str, title: str):
    def wrap(fn):
        SCENARIOS[key] = (title, fn)
        return fn

    return wrap


def setup(*, on: int, slots: int = 2) -> None:
    """Put the chat in a known state: *slots* conversations, showing *on*.

    Every scenario declares the state it needs, so one can be re-run on its own
    and a failure earlier in the run cannot quietly change what the next one is
    actually testing.
    """
    roster = wait_for(lambda: sessions().get("ready"), 30, "the engine")
    roster = sessions()
    if not roster["slots"]:
        msg("Reply with exactly one word: ready")
        wait_for(lambda: sessions()["slots"], 90, "the first conversation")
    for row in sorted(sessions()["slots"], key=lambda r: -r["index"]):
        if row["index"] > slots:
            cmd("session", f"kill {row['index']}")
            wait_for(
                lambda i=row["index"]: all(
                    r["index"] != i for r in sessions()["slots"]
                ),
                30,
                f"#{row['index']} to go",
            )
    while len(sessions()["slots"]) < slots:
        want = len(sessions()["slots"]) + 1
        cmd("session", "new")
        wait_for(
            lambda w=want: any(r["index"] == w for r in sessions()["slots"]),
            30,
            f"#{want} to exist",
        )
    if sessions()["foreground"] != slot(on):
        cmd("session", str(on))
        wait_foreground(slot(on))
    wait_for(
        lambda: not any(r["busy"] for r in sessions()["slots"]),
        TURN_TIMEOUT,
        "every conversation to go idle before the scenario starts",
    )


@scenario("s1", "opening a second conversation")
def s1() -> None:
    setup(on=1, slots=1)
    start = call_count()
    cmd("session", "new")
    wait_foreground(slot(2))
    roster = wait_for(
        lambda: sessions() if len(sessions().get("slots", [])) == 2 else None,
        20,
        "a two-conversation roster",
    )
    check(roster["foreground"] == slot(2), "the chat lands on the new conversation")
    check(
        any("#2" in t for t in texts(start)),
        "the chat is told which conversation it is on",
    )


@scenario("s2", "walking away from a running conversation")
def s2() -> None:
    setup(on=2)
    since_iso = now_iso()
    start = call_count()
    msg(STEPPED.format(topic="code review matters"))
    first = wait_streaming(start)

    cmd("session", "1")
    wait_foreground(slot(1))
    landed = wait_for(
        lambda: index_of_text(start, "\u25b8 #1 \u00b7"), 30, "the #1 banner"
    )

    check(
        first["message_id"] in deleted(start),
        "the half-written answer is taken off screen, not left frozen",
    )
    wait_turn_done(since_iso, slot(2))
    time.sleep(4)

    check(
        not streamed(landed + 1),
        "nothing from the background conversation streams into the chat",
    )
    notice = next(
        ((m, t) for m, t in buttoned(landed) if "#2 replied" in t),
        None,
    )
    check(notice is not None, "its finished answer arrives as a tagged notice")
    check(
        any(d.endswith("sw:2") for d in button_data(notice[0])),
        "the notice offers a way back into that conversation",
    )


@scenario("s3", "the reply of a conversation nobody watched is kept whole")
def s3() -> None:
    setup(on=2)
    since_iso = now_iso()
    start = call_count()
    msg(STEPPED.format(topic="small commits help"))
    wait_streaming(start)
    cmd("session", "1")
    wait_foreground(slot(1))
    wait_turn_done(since_iso, slot(2))
    time.sleep(3)

    check_reply_intact(since_iso, slot(2), "the conversation nobody watched")
    check(
        len(stored_reply(slot(2))) > 300,
        f"and it is a real answer, not a stub ({len(stored_reply(slot(2)))} chars)",
    )


@scenario("s4", "coming back to a conversation that finished while away")
def s4() -> None:
    setup(on=1)
    start = call_count()
    cmd("session", "2")
    wait_foreground(slot(2))
    wait_for(
        lambda: next((t for t in texts(start) if "#2 \u00b7" in t), None),
        20,
        "the conversation banner",
    )
    stored = " ".join(stored_reply(slot(2)).split())
    replay = wait_for(
        lambda: next(
            (
                t
                for t in texts(start)
                if stored[:60] and stored[:60] in " ".join(t.split())
            ),
            None,
        ),
        20,
        "the replay of where that conversation left off",
    )
    check(
        not replay.startswith("\u25b8 #2"),
        "the replay is its own message, not folded into the banner",
    )
    check(
        len(" ".join(replay.split())) >= min(len(stored), 3000),
        f"and it is the whole answer, not a slice ({len(stored)} chars stored)",
    )


def _opening(text: str, width: int = 60) -> str:
    return " ".join(text.split())[:width]


def _posted(since: int, needle: str) -> list[str]:
    """Replies posted as messages of their own, never the live stream's edits."""
    return [t for t in texts(since) if needle and needle in " ".join(t.split())]


def wait_new_reply(chat_id: str, before: int, timeout: float = TURN_TIMEOUT) -> str:
    """This turn's stored reply, once the conversation has one more than before.

    ``request_completed`` is matched on a whole-second timestamp, so a turn
    started inside the same second as the one that set the chat up can be
    satisfied by that earlier turn's record and read a stale reply back.
    Counting rows rather than comparing text also survives a model that
    answers the same prompt with the same words twice.
    """
    wait_for(
        lambda: reply_count(chat_id) > before,
        timeout,
        f"a new reply stored for {chat_id}",
    )
    return stored_reply(chat_id)


@scenario("s20", "an answer nothing wrote over is not repeated on the way back")
def s20() -> None:
    """The stutter half of the replay rule.

    A conversation you leave for a silent one is still the last voice in the
    chat when you come back, so its answer sits directly above the landing
    banner and posting it again there is noise. The visit goes to a brand new
    conversation precisely because it has nothing of its own to say.

    The new conversation is opened with its directory named, because leaving
    the directory to the picker is not silence — the picker is a message of
    its own and it buries the answer like anything else would, which is s23.
    """
    setup(on=1, slots=1)
    before = reply_count(slot(1))
    since_iso = now_iso()
    start = call_count()
    msg(SHORT)
    wait_turn_done(since_iso, slot(1))

    reply = _opening(wait_new_reply(slot(1), before))
    check(len(reply) > 40, f"the conversation on screen answered ({reply!r})")
    onscreen = [
        c for c in calls(start) if reply in " ".join(c["data"].get("text", "").split())
    ]
    check(bool(onscreen), "and the chat read that answer live")

    cmd("session", f"new {here()}")
    wait_foreground(slot(2))
    back_at = call_count()
    cmd("session", "1")
    wait_foreground(slot(1))
    wait_for(
        lambda: [t for t in texts(back_at) if t.startswith("\u25b8 #1")],
        30,
        "the landing banner for #1",
    )
    time.sleep(3)

    check(
        not _posted(back_at, reply),
        "coming back to it does not post the same answer a second time",
    )


@scenario("s23", "an answer a command wrote over is put back too")
def s23() -> None:
    """Burying is not something only a rival conversation can do.

    The skip fired on nothing but another conversation's reply, so anything
    else the chat received — the roster, a directory picker, a command's
    output — scrolled the answer away while the guard still called it the last
    thing in the chat, and the return landed on a bare banner. Each half here
    buries the answer a different way and expects it back.
    """
    setup(on=1, slots=1)
    before = reply_count(slot(1))
    since_iso = now_iso()
    msg(SHORT)
    wait_turn_done(since_iso, slot(1))
    reply = _opening(wait_new_reply(slot(1), before))
    check(len(reply) > 40, f"the conversation on screen answered ({reply!r})")

    cmd("session", f"new {here()}")
    wait_foreground(slot(2))
    cmd("status")
    buried = call_count()
    wait_for(lambda: texts(buried - 3), 20, "the command output to reach the chat")

    cmd("session", "1")
    wait_foreground(slot(1))
    wait_for(
        lambda: _posted(buried, reply),
        30,
        "the answer a command's output wrote over",
    )
    check(True, "a command's output counts as writing over it")

    cmd("session", "2")
    wait_foreground(slot(2))
    cmd("session")
    rostered = call_count()
    wait_for(
        lambda: [t for t in texts(rostered - 3) if "Conversations in this chat" in t],
        20,
        "the roster to reach the chat",
    )

    cmd("session", "1")
    wait_foreground(slot(1))
    wait_for(
        lambda: _posted(rostered, reply),
        30,
        "the answer the roster wrote over",
    )
    check(True, "and so does the roster")


@scenario("s21", "an answer the other conversation buried is put back, every time")
def s21() -> None:
    """The reported bug: coming back landed on a bare banner.

    One chat stream carries every conversation in it, so an answer stops
    being readable the moment another one writes under it. Replaying it once
    and never again left every later return with no context at all — this
    walks out and back twice to prove the second return is served too.
    """
    setup(on=2)
    before = reply_count(slot(2))
    since_iso = now_iso()
    start = call_count()
    msg(STEPPED.format(topic="short functions read better"))
    wait_streaming(start)
    cmd("session", "1")
    wait_foreground(slot(1))
    wait_turn_done(since_iso, slot(2))

    reply = _opening(wait_new_reply(slot(2), before))
    check(len(reply) > 40, "the conversation nobody watched answered")

    first_visit = call_count()
    cmd("session", "2")
    wait_foreground(slot(2))
    wait_for(
        lambda: _posted(first_visit, reply),
        30,
        "the replay of the answer the chat never received",
    )

    for visit in (1, 2):
        cmd("session", "1")
        wait_foreground(slot(1))
        one_before = reply_count(slot(1))
        one_iso = now_iso()
        msg(SHORT)
        wait_turn_done(one_iso, slot(1))
        buried = _opening(wait_new_reply(slot(1), one_before))
        check(len(buried) > 40, f"#1 wrote over it (return {visit})")

        again = call_count()
        cmd("session", "2")
        wait_foreground(slot(2))
        wait_for(
            lambda at=again: _posted(at, reply),
            30,
            f"the buried answer put back on return {visit}",
        )


@scenario("s24", "terminating the one on screen, then picking #1, shows #1's answer")
def s24() -> None:
    """The reported incident: #1 answered while the chat was on #3, #3 was
    terminated with #2 still open, and tapping #1 in the roster that followed
    showed nothing.

    The terminate lands on #1 without a banner because the roster is about to
    ask where to go, so #1 is already on screen when it is tapped — and that
    tap answered with a bare header, never the answer #1 gave while away.
    """
    setup(on=1, slots=3)
    before = reply_count(slot(1))
    since_iso = now_iso()
    start = call_count()
    msg(STEPPED.format(topic="naming things is hard"))
    wait_streaming(start)
    cmd("session", "3")
    wait_foreground(slot(3))
    wait_turn_done(since_iso, slot(1))
    reply = _opening(wait_new_reply(slot(1), before))
    check(len(reply) > 40, f"#1 answered while the chat was away ({reply!r})")

    killed = call_count()
    cmd("session", "kill 3")
    wait_for(lambda: all(r["index"] != 3 for r in sessions()["slots"]), 30, "#3 to go")
    roster = wait_for(
        lambda: next(
            (m for m, t in buttoned(killed) if "Conversations in this chat" in t),
            None,
        ),
        20,
        "the roster offered after the terminate",
    )
    check(sessions()["foreground"] == slot(1), "the terminate lands the chat on #1")
    check(not _posted(killed, reply), "and leaves the choice to the roster")

    tapped = call_count()
    tap(roster, "sess:sw:1")
    wait_for(
        lambda: _posted(tapped, reply),
        30,
        "#1's answer after choosing it from the roster",
    )
    check(True, "choosing #1 puts its answer back")

    again = call_count()
    tap(roster, "sess:sw:1")
    wait_for(
        lambda: [t for t in texts(again) if t.startswith("▸ #1")],
        20,
        "the header for the second tap",
    )
    time.sleep(3)
    check(
        not _posted(again, reply),
        "choosing it again while the answer is still in view does not repeat it",
    )


@scenario("s5", "leaving and returning while the answer is still being written")
def s5() -> None:
    """Two outcomes, both asserted — whether the turn is still going on return
    is the model's business, but coming back to nothing is never acceptable.

    A fast model finishes the whole answer during the walk-away, which is not a
    reason to skip the run: that path has its own contract (the finished reply
    is what the landing banner shows), and it is the one the mid-turn snapshot
    race used to break.
    """
    setup(on=2)
    since_iso = now_iso()
    start = call_count()
    msg(STEPPED.format(topic="fast tests matter"))
    first = wait_streaming(start)
    opening = first["data"]["text"][:60]

    cmd("session", "1")
    wait_foreground(slot(1))
    away = call_count()
    time.sleep(AWAY_SECONDS)
    back_at = call_count()
    cmd("session", "2")
    wait_foreground(slot(2))

    try:
        back = wait_for(lambda a=away: streamed(a), 30, "the answer back on screen")
    except AssertionError:
        print("    ....  the turn finished while away; checking that path instead")
        wait_turn_done(since_iso, slot(2))
        wait_for(
            lambda: [t for t in texts(back_at) if t.startswith("▸ #2")],
            30,
            "the landing banner for #2",
        )
        replay = wait_for(
            lambda: next(
                (t for t in texts(back_at) if opening.strip()[:24] in t), None
            ),
            30,
            "the finished reply back on screen",
        )
        check(
            not replay.startswith("▸ #2"),
            "coming back to a finished turn replays the reply beside the banner",
        )
        check_reply_intact(since_iso, slot(2), "the answer it came back to")
        return

    body = max((c["data"]["text"] for c in back), key=len)
    check(
        opening in body,
        f"coming back shows what it had already written ({len(body)} chars)",
    )

    def _grew_or_finished():
        longer = [c for c in streamed(away) if len(c["data"]["text"]) > len(body)]
        if longer:
            return ("streamed", longer)
        for o in log_events(since_iso, "request_completed"):
            if o.get("chat_id") == slot(2):
                return ("finished", o)
        return None

    outcome, _ = wait_for(
        _grew_or_finished,
        90,
        "the answer to keep being written, or the turn to finish",
    )
    if outcome == "streamed":
        check(True, "and it goes on streaming from there")
    else:
        print("    ....  the turn ended right after the switch back")
        check(
            True,
            "the replay is what the turn had written, and finalize closes it off",
        )

    wait_turn_done(since_iso, slot(2))
    time.sleep(4)
    check_reply_intact(since_iso, slot(2), "the answer it came back to")
    last = texts(away, methods=("sendMessage", "editMessageText"))[-1]
    check(CURSOR not in last, "and the stream is closed off cleanly")


@scenario("s22", "walking out and back repeatedly while the answer is written")
def s22() -> None:
    """One return is not the contract — every return is.

    ``s5`` proves a single walk-away and back. Suspending rewrites the
    responder's message ids and display offset, and resuming rewrites them
    again, so a state machine that survives one round trip can still strand
    the second: the roster keeps saying *working* and the chat stays blank.
    This leaves and returns three times inside one turn and asserts that every
    return puts the answer back on screen, still growing.

    Whether the turn is still going on any given return is the model's
    business, so each one is allowed to land on a finished reply instead — but
    never on nothing. "Still going" is read off the stored reply count rather
    than off ``request_completed``, whose whole-second timestamps let the turn
    that set the chat up answer for this one.
    """
    setup(on=2)
    before = reply_count(slot(2))
    since_iso = now_iso()
    start = call_count()
    msg(STEPPED.format(topic="reviewing your own diff first pays off"))
    first = wait_streaming(start)
    opening = first["data"]["text"][:60]

    seen = 0
    for visit in (1, 2, 3):
        cmd("session", "1")
        wait_foreground(slot(1))
        away = call_count()
        time.sleep(SHORT_AWAY_SECONDS)

        if reply_count(slot(2)) > before:
            print(f"    ....  the turn finished before return {visit}")
            break

        back_at = call_count()
        cmd("session", "2")
        wait_foreground(slot(2))
        try:
            back = wait_for(
                lambda a=away: streamed(a), 30, f"the answer back on screen ({visit})"
            )
        except AssertionError:
            print(f"    ....  the turn finished during return {visit}")
            wait_new_reply(slot(2), before)
            wait_for(
                lambda a=back_at: [t for t in texts(a) if opening.strip()[:24] in t],
                30,
                f"the finished reply back on screen (return {visit})",
            )
            check(True, f"return {visit} was served the finished reply")
            break

        body = max((c["data"]["text"] for c in back), key=len)
        check(
            opening in body,
            f"return {visit} shows what it had written ({len(body)} chars)",
        )
        check(
            len(body) >= seen,
            f"and return {visit} is not shorter than what return {visit - 1} showed",
        )
        seen = len(body)

    wait_new_reply(slot(2), before)
    time.sleep(4)
    check_reply_intact(since_iso, slot(2), "the answer walked out on three times")
    last = texts(start, methods=("sendMessage", "editMessageText"))[-1]
    check(CURSOR not in last, "and the stream is closed off cleanly")


@scenario("s6", "two conversations working at once")
def s6() -> None:
    setup(on=1)
    since_iso = now_iso()
    start = call_count()
    msg(STEPPED.format(topic="naming things is hard"))
    wait_streaming(start)

    cmd("session", "2")
    wait_foreground(slot(2))
    both = call_count()
    msg(STEPPED.format(topic="logs beat print statements"))
    wait_streaming(both)

    wait_turn_done(since_iso, slot(1))
    wait_turn_done(since_iso, slot(2))
    time.sleep(4)

    check(bool(streamed(both)), "the conversation on screen streams as usual")
    check(
        any("#1 replied" in t for t in texts(both)),
        "the other one reports in without writing over it",
    )
    for index in (1, 2):
        check_reply_intact(since_iso, slot(index), f"#{index}")


@scenario("s7", "a question from off screen waits until you go back to it")
def s7() -> None:
    setup(on=2)
    since_iso = now_iso()
    start = call_count()
    msg(ASK)
    cmd("session", "1")
    wait_foreground(slot(1))

    notice_id, notice = wait_question(start)
    check(
        "#2" in notice and "Tabs or spaces" not in notice,
        f"the chat is told #2 is waiting, not given its question: {notice[:48]!r}",
    )
    check(
        bool(log_events(since_iso, "telegram_prompt_deferred")),
        "and the question itself is held back",
    )
    check(
        not [d for d in button_data(notice_id) if d.startswith("interact:")],
        "so there is nothing to answer from the conversation on screen",
    )

    opened = call_count()
    tap(notice_id, f"sess:sw:{2}")
    wait_foreground(slot(2))

    prompt_id, prompt = wait_for(
        lambda: next(
            (
                (mid, text)
                for mid, text in buttoned(opened)
                if any(d.startswith("interact:") for d in button_data(mid))
            ),
            None,
        ),
        30,
        "the held question to appear once #2 is on screen",
    )
    check("Tabs" in prompt or "spaces" in prompt.lower(), "it is the real question")
    check(
        bool(log_events(since_iso, "telegram_deferred_flushed")),
        "released by the switch, not by a timer",
    )

    choice = next(d for d in button_data(prompt_id) if d.startswith("interact:"))
    tap(prompt_id, choice)

    answered = wait_for(
        lambda: next(
            (
                o
                for o in log_events(since_iso, "question_completed")
                if o.get("chat_id") == slot(2)
            ),
            None,
        ),
        60,
        "the question to be answered",
    )
    check(bool(answered), "the answer goes back to the conversation that asked")
    wait_turn_done(since_iso, slot(2))


@scenario("s8", "typing while the conversation off screen is waiting on something")
def s8() -> None:
    """What you type belongs to the conversation you are looking at.

    The held question is not in the chat — it is a notice with an Open button —
    so reading a typed message as its answer swallows an instruction meant for
    the agent on screen and leaves it silent.
    """
    setup(on=2)
    since_iso = now_iso()
    start = call_count()
    msg(ASK)
    cmd("session", "1")
    wait_foreground(slot(1))

    _notice_id, notice = wait_question(start)
    check("#2" in notice, "the chat is told #2 is waiting on something")
    check(
        "Tabs or spaces" not in notice,
        "without pasting its question under the conversation on screen",
    )

    typed = now_iso()
    msg("Reply with exactly: ON SCREEN")
    started = wait_for(
        lambda: next(
            (
                o
                for o in log_events(typed, "request_started")
                if o.get("chat_id") == slot(1)
            ),
            None,
        ),
        60,
        "a turn to start in the conversation on screen",
    )
    check(bool(started), "the message starts a turn in the conversation on screen")
    check(
        not [
            o
            for o in log_events(typed, "question_completed")
            if o.get("chat_id") == slot(2)
        ],
        "and is not swallowed as the answer to the question off screen",
    )
    check(
        bool(
            [
                o
                for o in log_events(since_iso, "telegram_prompt_deferred")
                if o.get("chat_id") == slot(2)
            ]
        ),
        "which is still held, waiting for #2 to be opened",
    )
    wait_turn_done(typed, slot(1))

    opened = call_count()
    cmd("session", "2")
    wait_foreground(slot(2))
    prompt_id, _ = wait_for(
        lambda: next(
            (
                (mid, text)
                for mid, text in buttoned(opened)
                if any(d.startswith("interact:") for d in button_data(mid))
            ),
            None,
        ),
        30,
        "the held question, released now that #2 is on screen",
    )
    tap(prompt_id, next(d for d in button_data(prompt_id) if d.startswith("interact:")))
    wait_turn_done(since_iso, slot(2))


@scenario("s16", "a question you walk away from is taken back down with you")
def s16() -> None:
    setup(on=2)
    since_iso = now_iso()
    start = call_count()
    msg(ASK)

    prompt_id, prompt = wait_question(start)
    check("Tabs" in prompt or "spaces" in prompt.lower(), "the question renders on #2")

    left = call_count()
    cmd("session", "1")
    wait_foreground(slot(1))
    wait_for(
        lambda: prompt_id in deleted(start),
        30,
        "the question to be taken off screen",
    )
    check(
        bool(log_events(since_iso, "telegram_prompts_withdrawn")),
        "leaving withdraws it instead of leaving it under #1",
    )
    notice = wait_for(
        lambda: next(
            ((m, t) for m, t in buttoned(left) if "#2 is waiting on you" in t),
            None,
        ),
        30,
        "the notice that #2 still needs an answer",
    )
    check(
        not [d for d in button_data(notice[0]) if d.startswith("interact:")],
        "and it cannot be answered from the conversation on screen",
    )

    back = call_count()
    tap(notice[0], "sess:sw:2")
    wait_foreground(slot(2))
    again = wait_for(
        lambda: next(
            (
                (mid, text)
                for mid, text in buttoned(back)
                if any(d.startswith("interact:") for d in button_data(mid))
            ),
            None,
        ),
        30,
        "the question to come back under #2",
    )
    check(again[0] != prompt_id, "as a fresh message, not the one that was deleted")
    check(
        "Tabs" in again[1] or "spaces" in again[1].lower(),
        "carrying the same question",
    )

    choice = next(d for d in button_data(again[0]) if d.startswith("interact:"))
    tap(again[0], choice)
    answered = wait_for(
        lambda: next(
            (
                o
                for o in log_events(since_iso, "question_completed")
                if o.get("chat_id") == slot(2)
            ),
            None,
        ),
        60,
        "the answer to land",
    )
    check(bool(answered), "and it is still answerable after the round trip")
    wait_turn_done(since_iso, slot(2))


@scenario("s9", "a question from the conversation on screen is untouched")
def s9() -> None:
    setup(on=1)
    since_iso = now_iso()
    start = call_count()
    msg(ASK)

    _prompt_id, prompt = wait_question(start)
    check(not prompt.startswith("[#"), "the question carries no slot tag")
    check("reply with a message" in prompt.lower(), "and it invites a typed answer")
    typed = call_count()
    msg("Tabs")

    answered = wait_for(
        lambda: next(
            (
                o
                for o in log_events(since_iso, "question_completed")
                if o.get("chat_id") == slot(1)
            ),
            None,
        ),
        60,
        "the answer",
    )
    check(bool(answered), "it is answered in place")
    check(
        not any("Sent to #" in t for t in texts(typed)),
        "and nothing is said about redirecting it",
    )
    wait_turn_done(since_iso, slot(1))


@scenario("s10", "typing to the conversation on screen while another one works")
def s10() -> None:
    setup(on=2)
    since_iso = now_iso()
    start = call_count()
    msg(STEPPED.format(topic="rollbacks should be boring"))
    wait_streaming(start)
    cmd("session", "1")
    wait_foreground(slot(1))

    msg("Reply with exactly one word: acknowledged")
    started = wait_for(
        lambda: [
            o
            for o in log_events(since_iso, "request_started")
            if o.get("chat_id") == slot(1)
        ],
        45,
        "the message to start a turn in #1",
    )
    check(bool(started), "a plain message still starts a turn where the chat is")
    wait_turn_done(since_iso, slot(1))
    wait_turn_done(since_iso, slot(2))
    check_reply_intact(since_iso, slot(2), "the conversation off screen")


@scenario("s11", "terminating the conversation on screen")
def s11() -> None:
    setup(on=2)
    cmd("session", "kill 2")
    roster = wait_for(
        lambda: sessions() if len(sessions().get("slots", [])) == 1 else None,
        30,
        "the conversation to be removed",
    )
    check(
        roster["foreground"] == slot(1), "the chat falls back to the first conversation"
    )
    check(roster["slots"][0]["index"] == 1, "and only that one is left")


@scenario("s12", "a message after terminating goes to the conversation left behind")
def s12() -> None:
    setup(on=1, slots=1)
    since_iso = now_iso()
    msg("Reply with exactly one word: home")
    started = wait_for(
        lambda: [
            o
            for o in log_events(since_iso, "request_started")
            if o.get("chat_id") == slot(1)
        ],
        45,
        "the turn to start in #1",
    )
    check(bool(started), "the message lands in the conversation that is left")
    wait_turn_done(since_iso, slot(1))


@scenario("s13", "a new conversation asks which project it is for")
def s13() -> None:
    setup(on=1, slots=1)
    start = call_count()
    cmd("session", "new")
    wait_foreground(slot(2))

    picker = wait_for(
        lambda: next(
            (
                (mid, text)
                for mid, text in buttoned(start)
                if "Select directory" in text
            ),
            None,
        ),
        30,
        "the directory picker",
    )
    picker_id, _ = picker
    check(True, "opening a conversation offers the directory picker")
    check(
        all(d.startswith("dir:") for d in button_data(picker_id)),
        "every option switches this conversation's directory",
    )
    ordered = [t for t in texts(start) if "▸ #2" in t or "Select directory" in t]
    check(
        ordered and "▸ #2" in ordered[0],
        "and it arrives under #2, not the conversation it was opened from",
    )


@scenario("s14", "terminating with several left offers the roster")
def s14() -> None:
    setup(on=3, slots=3)
    start = call_count()
    cmd("session", "kill 3")
    roster = wait_for(
        lambda: sessions() if len(sessions().get("slots", [])) == 2 else None,
        30,
        "the conversation to be removed",
    )
    shown = wait_for(
        lambda: next(
            (t for t in texts(start) if "Conversations in this chat" in t), None
        ),
        20,
        "the roster to be offered",
    )
    check(bool(shown), "the chat is asked which conversation to go to")
    check("#3" not in shown, "the terminated one is gone from it")
    check(
        roster["foreground"] == slot(1),
        "and the chat has somewhere to type in the meantime",
    )


@scenario("s15", "terminating with one left jumps straight there")
def s15() -> None:
    setup(on=2, slots=2)
    start = call_count()
    cmd("session", "kill 2")
    roster = wait_for(
        lambda: sessions() if len(sessions().get("slots", [])) == 1 else None,
        30,
        "the conversation to be removed",
    )
    check(roster["foreground"] == slot(1), "the chat lands on the one that is left")
    check(
        not any("Conversations in this chat" in t for t in texts(start)),
        "with nothing to choose, so no roster is offered",
    )


@scenario("s17", "a long reply comes back whole and outlives the banner")
def s17() -> None:
    """The two halves of a switch that used to lose the answer.

    The replay was folded into the landing banner: one message, so anything
    past the ceiling was cut, and the banner is chrome the next thing you type
    deletes — taking the agent's previous message down with it.
    """
    setup(on=2)
    since_iso = now_iso()
    msg(LONG)
    cmd("session", "1")
    wait_foreground(slot(1))
    wait_turn_done(since_iso, slot(2))

    stored = stored_reply(slot(2))
    check(
        len(stored) > REPLAY_CEILING,
        f"the conversation off screen wrote a reply past the old ceiling "
        f"({len(stored)} chars)",
    )

    back = call_count()
    cmd("session", "2")
    wait_foreground(slot(2))
    replayed = wait_for(
        lambda: (
            "".join(t for t in texts(back) if not t.startswith("▸ #2"))
            if len("".join(t for t in texts(back) if not t.startswith("▸ #2")))
            >= len(stored) - FOOTER_SLACK
            else None
        ),
        30,
        "the whole reply back on screen",
    )
    check(
        len(replayed) >= len(stored) - FOOTER_SLACK,
        f"switching back replays all of it, not one message of it "
        f"({len(replayed)}/{len(stored)} chars)",
    )

    typed = call_count()
    followed_up = now_iso()
    replay_ids = {
        c["message_id"]
        for c in calls(back)
        if c["method"] == "sendMessage"
        and not c["data"].get("text", "").startswith("▸")
    }
    msg("Reply with exactly one word: noted")
    wait_turn_done(followed_up, slot(2))
    check(
        not (replay_ids & deleted(typed)),
        "and the next thing you type does not delete it",
    )


@scenario("s18", "the one conversation that cannot be terminated says so")
def s18() -> None:
    """Slot 1 is the chat's own id, so terminating it can only reset it.

    The roster used to offer a ✕ on it anyway, and the confirm and the result
    both said "terminated" — so the row was still there afterwards and the
    chat kept tapping it.
    """
    setup(on=1, slots=2)
    start = call_count()
    cmd("session", "")
    listing = wait_for(
        lambda: next(
            (m for m, t in buttoned(start) if "Conversations in this chat" in t), None
        ),
        20,
        "the roster",
    )
    data = button_data(listing)
    check("sess:k:1" not in data, "the roster offers no ✕ on #1")
    check("sess:k:2" in data, "and still offers one on #2")

    start = call_count()
    cmd("session", "kill 1")
    said = wait_for(
        lambda: next((t for t in texts(start) if "#1" in t), None),
        20,
        "the outcome",
    )
    check("Cleared #1" in said, "killing #1 reports a reset")
    check("Terminated" not in said, "and never claims it was removed")
    check(
        any(row["index"] == 1 for row in sessions()["slots"]),
        "which is what happened — #1 is still there",
    )


@scenario(
    "s19", "a question left open a long time still delivers the work that follows it"
)
def s19() -> None:
    """The reported incident, reproduced end to end.

    A question raised by the conversation off screen is held until you go back
    to it, so the turn sits blocked for minutes. That wait used to age the same
    clock the turn backstops read, so the turn was declared finished on the very
    poll the answer arrived: the chat kept the few lines written before the
    question and lost everything the agent did with the answer, while the pane
    carried on working for another forty minutes.

    This is an end-to-end guard, not the proof. Reproducing the loss needs a
    poll to land in the second or so between the dialog closing and claude's
    first line of new output, before the tailer refreshes the clock — against a
    5s poll that is a coin toss, and a local pane usually wins it. The
    deterministic version lives in
    ``tests/agents/test_tmux_agent.py::test_idle_backstop_does_not_count_time_blocked_on_a_human``.
    """
    setup(on=2)
    cmd("clear")
    time.sleep(2)
    since_iso = now_iso()
    start = call_count()
    msg(ASK_THEN_WORK)
    cmd("session", "1")
    wait_foreground(slot(1))

    notice_id, notice = wait_question(start)
    check("#2" in notice, f"the chat is told #2 is waiting: {notice[:48]!r}")

    opened = call_count()
    tap(notice_id, "sess:sw:2")
    wait_foreground(slot(2))
    _prompt_id, prompt = wait_for(
        lambda: next(
            (
                (mid, text)
                for mid, text in buttoned(opened)
                if any(d.startswith("interact:") for d in button_data(mid))
            ),
            None,
        ),
        30,
        "the held question to appear once #2 is on screen",
    )
    check("Tabs" in prompt or "spaces" in prompt.lower(), "it is the real question")

    print(
        f"    ....  leaving it open for {HELD_SECONDS:.0f}s (grace {IDLE_GRACE:.0f}s)"
    )
    time.sleep(HELD_SECONDS)
    check(
        not [
            o
            for o in log_events(since_iso, "tmux_turn_idle_completed")
            if o.get("chat_id") == slot(2)
        ],
        "a turn parked on the question is not called finished while it waits",
    )

    answered = 0
    deadline = time.time() + 150
    while time.time() < deadline:
        if [
            o
            for o in log_events(since_iso, "question_completed")
            if o.get("chat_id") == slot(2)
        ]:
            break
        asked = len(
            [
                o
                for o in log_events(since_iso, "telegram_question_sent")
                if o.get("chat_id") == slot(2)
            ]
        )
        if asked > answered:
            msg(FREE_ANSWER)
            answered += 1
        time.sleep(1.0)
    check(answered >= 3, f"all three questions were answered ({answered})")
    wait_turn_done(since_iso, slot(2))

    ended_early = log_events(
        since_iso,
        "tmux_turn_idle_completed",
        "tmux_turn_no_progress_finalized_with_text",
        "tmux_turn_no_progress",
    )
    check(
        not [o for o in ended_early if o.get("chat_id") == slot(2)],
        "and the wait is not counted against the turn once it is answered",
    )
    reply = stored_reply(slot(2))
    check(
        ANSWER_MARKER in reply,
        f"the work done after the answer reaches the chat: {reply[-90:]!r}",
    )


ORDER = [
    "s1",
    "s2",
    "s3",
    "s4",
    "s5",
    "s22",
    "s20",
    "s23",
    "s21",
    "s24",
    "s17",
    "s6",
    "s7",
    "s19",
    "s8",
    "s16",
    "s9",
    "s10",
    "s11",
    "s12",
    "s13",
    "s14",
    "s15",
    "s18",
]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--list" in sys.argv:
        for key in ORDER:
            print(f"  {key:4} {SCENARIOS[key][0]}")
        return 0
    if not APP_LOG.exists():
        print(f"app log not found at {APP_LOG} — export APPROVED_DIR or APP_LOG")
        return 1

    keys = args or ORDER
    failed: list[str] = []
    for key in keys:
        title, fn = SCENARIOS[key]
        print(f"\n[{key}] {title}")
        began = time.time()
        try:
            fn()
        except CheckError:
            failed.append(key)
        except Exception as exc:
            print(f"    ERROR {type(exc).__name__}: {exc}")
            failed.append(key)
        print(f"    ({time.time() - began:.0f}s)")

    print("\n" + "=" * 60)
    passed = [k for k in keys if k not in failed]
    print(f"passed {len(passed)}/{len(keys)}")
    if failed:
        print("failed: " + ", ".join(f"{k} ({SCENARIOS[k][0]})" for k in failed))
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
