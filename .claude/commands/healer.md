Use the playwright test healer agent to verify that the latest tests are working and repair them otherwise.

When healing tests:
1. Run the failing test suite with `npx playwright test` to identify failures
2. For each failing test, analyze the Playwright trace file
3. Use browser_snapshot to inspect the current UI state
4. Compare expected vs actual state and determine root cause
5. Fix the test code (update selectors, assertions, or waits)
6. Re-run to confirm the fix
