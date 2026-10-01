---
paths:
  - "leashd/policies/*.yaml"
  - "leashd/core/safety/**"
---

# Safety pipeline and policies

- `deny` means the call must never run. `require_approval` means it needs a second look. Anything a human could reasonably wave through (for example `rm -rf`) belongs in `require_approval`, not `deny`.
- Rules match first-match-wins, so deny rules sit at the top of each policy file. Each standalone policy (every file except the `dev-tools.yaml` overlay) carries its own deny rules, so a change to the deny floor usually has to be made in every file.
- Compound Bash is split into segments and evaluated deny-wins, and patterns match what the shell will execute (including `bash -c` / `eval` payloads), not quoted text. Add a case to `tests/core/safety/test_policy.py` or `test_policy_compound.py` for every rule you change.
