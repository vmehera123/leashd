---
paths:
  - "tests/**/*.py"
---

# Tests

- `tests/conftest.py` has an autouse `_isolate_env` fixture: it disables `.env` loading, strips every `LEASHD_*` variable and pins `LEASHD_TMUX_SOCKET_DIR` to `tmp_path`. Never bypass it or build a config that points at `~/.leashd/`, since that is the running daemon's tmux socket and database, and tests have killed live agent panes that way.
- Use `MockConnector` from `tests/conftest.py` rather than a new fake connector.
- Browser tests live in `tests/e2e/`, are marked `e2e`, and are deselected by default; run them with `uv run pytest -m e2e -v`.
- Python 3.10 must keep working: catch `asyncio.TimeoutError` (not the 3.11 `TimeoutError` alias) and don't use `asyncio.timeout()`.
