# Deferred Work

Items the user explicitly deferred during an OpenSpec flow. Format:

> **Source change** — what was deferred — why — when to revisit. Mark done when picked up; do not remove.

## 2026-05-10

- **refactor-plan-tuples-to-actions §5.4-5.6** — live-host dry-run byte-equivalence
  comparison (pre- vs post-refactor `uv run sandbox start <inst> --dry-run` on a
  real machine, both `sudo` and `polkit` auth modes) — out of scope for the
  implementation subagent (no live-host access, `uv run sandbox start` is not
  permitted in the agent sandbox) — revisit during Phase 6 user-interactive
  validation, before merging the change to main. The PRIMARY automated
  fixture-diff gate (5.1-5.3) is in place at
  `tests/unit/cli/test_dry_run_fixture.py` and runs in CI on every commit;
  the SECONDARY live-host gate is a recommended additional confidence check.
  - **DONE 2026-05-10** — executed against test instance `phase6-test` (2 workspaces:
    `main` + `scratch`; `db_postgres` enabled; `mcp_firecrawl` enabled). Captured
    `uv run sandbox start phase6-test --dry-run` from base commit `085eef8` (pre
    either refactor) vs polished tip `ee6ba95` for both auth modes. Result:
    `diff -u` empty for `polkit` (272 lines each side) and `sudo` (272 lines each
    side). Typed `Action.describe()` outputs are byte-identical to the legacy
    tuple-printing across both auth-mode branches. SECONDARY gate confirms the
    PRIMARY synthetic fixture matches real-host output.
