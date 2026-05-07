# Deferred work log

Items deferred during ongoing OpenSpec changes. Each entry: source change,
what was deferred, why, and when to revisit. Mark items done when picked
up — do NOT remove them.

---

## instance-workspace-model

### COPY_DEFAULT_EXCLUDES drift from spec list — RESOLVED

- **Source:** task 5 (`--copy` recipe) and task 8 (`workspace_backups`)
- **Originally deferred:** the constant `core.workspace_copy.COPY_DEFAULT_EXCLUDES`
  diverged from `cli-workspace`'s "Copy Default-Excludes List".
- **Resolution (during task 8):** spec amended to add `.mypy_cache/`,
  `.ruff_cache/`, `.turbo/` (modern caches not in the original list); code
  conformed to the amended spec — `.git`, `.cargo/registry` dropped, missing
  spec entries (`env`, `bin`, `out`, `.nuxt`, `.tsbuildinfo`, `vendor/bundle`,
  `cmake-build-*`, `*.swp`, `*.log`, `coverage`, `.nyc_output`) added.
