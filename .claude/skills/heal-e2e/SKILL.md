---
name: heal-e2e
description: Run the Playwright E2E suite against the Web UI and repair failing tests. Use after Web UI changes break tests/e2e/.
disable-model-invocation: true
argument-hint: "[test file or -k expression]"
---

Run the Web UI E2E suite and fix what fails: $ARGUMENTS

1. Run `uv run pytest -m e2e -v $ARGUMENTS`. If Chromium is missing, run `uv run playwright install chromium` once first.
2. For each failure, decide whether the test or the app is wrong. Read the test, `tests/e2e/conftest.py` (one uvicorn server and one browser per module) and the Web UI code it exercises in `leashd/data/webui/` and `leashd/web/`.
3. If the app changed on purpose, update the test's selectors, assertions or waits. If the app regressed, fix the app and say so. Never loosen an assertion only to make it pass.
4. Re-run the failing tests until they pass, then run the whole E2E tier once more and `make check`.
