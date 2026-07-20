# Gate-config landing — validation evidence

Change: `ci: add no-mistakes gate config and treehouse worktree pool`
Commit `2bba9b7` on `fm/gate-configs-sandbox-ai` (base `a09a690`).

## Scope: config-only, exactly three files

```
$ git diff --name-status a09a690..2bba9b7
M	.gitignore
A	.no-mistakes.yaml
A	treehouse.toml
```

No source or behavior files touched — the non-config filter returns nothing:

```
$ git diff --name-only a09a690..2bba9b7 | grep -vE '^(\.gitignore|\.no-mistakes\.yaml|treehouse\.toml)$'
(empty) -> Only the 3 config files changed
```

## (1) .gitignore — single `.nm-home/` entry under Tooling

```
20:# Tooling
21:# Containerized-gate HOME (uv caches/toolchains inside no-mistakes runs)
22:.nm-home/
23:.antigravityignore
24:.agent/
```

Diff adds exactly two lines (comment + `.nm-home/`), nothing else.

## (2) .no-mistakes.yaml — well-formed gate config

```
YAML parsed OK. Top-level keys: ['commands', 'test']
  commands keys: ['lint', 'test']
  lint cmd starts: hardened-run --profile fetch --image pyt
  test cmd starts: hardened-run --profile fetch --image pyt
  test.evidence: {'evidence': {'store_in_repo': True}}
```

Both commands invoke `hardened-run --profile fetch` (rootless podman + gVisor,
egress for dependency installs) with `HOME` redirected into the gitignored
`.nm-home/` so the read-only rootfs can persist uv caches/toolchains.

## (3) treehouse.toml — worktree pool, max_trees = 4

```
TOML parsed OK: {'max_trees': 4, 'root': ''}
max_trees == 4 OK
```

## Commit conventions

```
$ git show -s --format="%s" 2bba9b7
ci: add no-mistakes gate config and treehouse worktree pool

$ git show -s --format="%b" 2bba9b7 | grep -i co-author
(none) -> NO co-author line
```

Conventional-commit `ci:` prefix; no co-author trailer, per repo/global convention.

## Note on the containerized gate commands

The lint/test commands defined in the new `.no-mistakes.yaml` intentionally do
NOT execute on this PR: the daemon reads `.no-mistakes.yaml` only from the
trusted default branch, so this first push validates under no-mistakes' DEFAULT
commands. That is expected and documented in the change intent, not a
misconfiguration. This validation therefore confirms the configs are
byte-correct and well-formed; it does not (and should not) run the containerized
gate here.
