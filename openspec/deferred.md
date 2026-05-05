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
