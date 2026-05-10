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
