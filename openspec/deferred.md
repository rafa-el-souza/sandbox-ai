# Deferred Work

Items that were explicitly deferred during an OpenSpec change. Mark items
done when picked up — do not remove them.

## acl-ownership-recipes

- **`secrets_hydrated_restrictively` and `pre_existing_instance_layout` doctor
  checks are no-ops in wheel installs.** Both delegate to
  `core.doctor._scan_instance_dirs`, which derives the orchestrator's
  `sandboxes/` root by walking `__file__` parents 3 levels — only correct in
  dev checkouts. After `uv tool install`, `__file__` points into
  `site-packages/`, where no `sandboxes/` tree exists. Today the checks
  surface `skip` with a wheel-install diagnostic; full coverage requires
  retiring `_resolve_sandbox_ai_home`'s `__file__`-walking pattern, which is
  the scope of the deferred change 5 (per `acl-ownership-recipes/proposal.md`
  Out of Scope: "Removing `_resolve_sandbox_ai_home()` (still needed for
  legacy `sandboxes/` resolution; change 5)"). Revisit when change 5 lands.

- **`make verify-acl-recipes` operator-runnable target.** Six of the seven
  E2E integration tests (cache leaf chowned, ro file chowned, workspace
  shared-group applied, named-ACL re-applied on next start, drift detection)
  remain `pytest.skip` because they require a fully-configured host
  (rootless docker for the daemon user + runsc registered + bridge group
  set up). The host-side mutation logic is pinned by unit tests and the
  workspace named-ACL round-trip integration test; the missing layer is
  "all the pieces line up on a real host." Adding a Makefile target that
  scripts the verification commands from the proposal's Migration Plan
  (`sandbox init` → `sandbox start` → `getfacl`/`stat`/`id` assertions →
  `sandbox stop` → re-assertions) gives release engineers a one-command
  pre-cut gate without inventing CI prereqs that won't exist there. Defer
  until the first release where this matters.
