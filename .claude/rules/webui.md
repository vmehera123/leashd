---
paths:
  - "leashd/data/webui/**"
  - "leashd/web/**"
---

# Web UI

- `make check` does not exercise the browser. After changing the Web UI, also run `uv run pytest -m e2e -v` (one-time setup: `uv run playwright install chromium`).
- Approvals, questions and reconnection state are shared with the Telegram connector through the Engine. Check that a Web UI change doesn't assume a single connector.
