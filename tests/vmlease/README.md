# Real-host validation batteries (vmlease)

These are **[vmlease](https://github.com/zerotrust-ai/vmlease) probe batteries** that validate sandbox-ai
end-to-end on **fresh, real cloud hosts** (ubuntu / debian-13 / fedora / arch) — the "ratified ≠ validated"
gate that unit/integration tests can't cover (the privilege boundary, rootless docker, gVisor, the full
`sandbox` command surface).

They are **not** collected by `pytest` (`pytest.testpaths = ["tests/unit"]`); they're run by hand against
billable Hetzner VMs. They live here (tracked) so the canonical set isn't lost — the live working area,
plus the raw run outputs, stay **untracked** under `openspec/explorations/ongoing/…/probes/` (gitignored).

## Canonical baseline batteries (per-command × distro × mode)

One probe **per `sandbox` command**, run as a lifecycle, so each command maps to one matrix cell. The three
batteries differ only in how `setup` provisions the host:

| File | Mode | Probes |
|---|---|--:|
| `baseline-op-rootless.json` | operator-rootless (`setup --docker-execution-mode operator-rootless`, run as the operator) | 16 |
| `baseline-separate-user-sudo.json` | separate-user + sudo (`--docker-execution-mode separate-user --machinectl-auth sudo`) | 16 |
| `baseline-separate-user-polkit.json` | separate-user + polkit — tier 1 asserts `setup` *refuses* polkit; tier 2 installs the genuine `render_polkit_rule` + flips the toml, then runs the lifecycle | 18 |

Lifecycle order (after `PREP`): `setup → init → doctor → status → workspace list/add/rename → start →
status → attach → stop → workspace remove/restore → destroy`. Workspace mutations run while stopped.
Each probe emits `<CMD>_*_OK` / `_FAIL` gating tokens plus observational `_review`/`_info` tokens
(e.g. the F-063 crossing-class capture on `start`/`attach`). Gate is `exit $rc`; `PREP` is the only
`mutating:operator-space` probe, the rest are `mutating:host-root` (single rank → authoring order is
execution order).

`vmlease-compose-plugin-check.json` is a separate, read-only check that a freshly-provisioned host has
vmlease's docker prerequisites (compose v2 plugin, buildx, `script(1)`); no source upload.

## `baseline-matrix.md`

The tracked, publish-safe **command × distro × mode verdict snapshot** (verdicts only — no host IPs or
transcripts). It is the progress yardstick: re-run the batteries after a fix and regenerate the matrix to
see cells flip. The current snapshot is the **pre-fix baseline** (code `acbd55f`, the clean E-005 unit).

## Running

```sh
# build a source tarball at the revision under test
git -C <checkout> archive --format=tar.gz <rev> -o /tmp/sandbox-ai-src.tar.gz

# one run per battery (serialize runs — Hetzner Primary-IP cap; --parallel 4 covers the 4 distros)
uv run --directory ../vmlease vmlease run \
  --battery tests/vmlease/baseline-<mode>.json \
  --distros ubuntu,debian,fedora,arch --server-type cpx22 \
  --upload /tmp/sandbox-ai-src.tar.gz --operator probe --firewall <fw> \
  --run-token <slug> --results-dir <results-dir> --timestamp <YYYYMMDDtHHMMSSz> --parallel 4 \
  --ssh-key <registered-name> --ssh-key-path <local-priv> --yes

# summarize each raw result -> a versioned .summary.json companion
#   (verdict computed from the *_OK/*_FAIL tokens; process exit = overall verdict)
uv run --directory ../vmlease vmlease summarize <raw>.json --battery tests/vmlease/baseline-<mode>.json
```

Raw `vmlease-*.json` outputs + their `*.summary.json` companions are written to the run's `--results-dir`
(kept under the gitignored exploration tree — they carry host IPs / full transcripts).

## Expected pre-fix pattern (the yardstick)

- **op-rootless** — green on every command × distro (no privilege crossing; the apt/CI mode).
- **separate-user · sudo** — green except: `start`/`doctor` fail on **apt (ubuntu/debian)** (F-063 `machinectl`
  PTY empty-crossing — `user-early` vs `user-light`), and `attach` fails on **all 4** (F-060 — the
  unprivileged `systemd-run` ProxyCommand needs `manage-units` polkit auth, headless-blocked). `setup`
  converges on all 4, including apt (refines F-055: the apt failure is runtime-only, not a setup wall).
- **separate-user · polkit** — `setup` refuses polkit; even with the genuine rule + polkitd up, every crossing
  op is headless-blocked (*interactive authentication required*) on all distros (F-060).

When the `sudo systemd-run --pipe` fix lands, the sudo-mode `start`/`doctor` apt cells should flip to green.
