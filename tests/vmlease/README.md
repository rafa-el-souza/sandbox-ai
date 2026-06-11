# Real-host validation batteries (vmlease)

These are **[vmlease](https://github.com/zerotrust-ai/vmlease) probe batteries** that validate sandbox-ai
end-to-end on **fresh, real cloud hosts** (ubuntu / debian-13 / fedora / arch) — the "ratified ≠ validated"
gate that unit/integration tests can't cover (the privilege boundary, rootless docker, gVisor, the full
`sandbox` command surface).

They are **not** collected by `pytest` (`pytest.testpaths = ["tests/unit"]`); they're run by hand against
billable Hetzner VMs. They live here (tracked) so the canonical set isn't lost — the live working area,
plus the raw run outputs, stay **untracked** under `openspec/explorations/ongoing/…/probes/` (gitignored).

Each battery is a **vmlease TOML bundle**: a `battery.toml` manifest whose probes are authored inline
(`run = '''…'''`). `vmlease lint` shellchecks every probe with real `file:line:col`; `vmlease plan` loads
+ resolves the bundle with zero provider calls. (vmlease's battery format is TOML-only — there is no JSON
loader.)

## The reference baselines (per-command × distro × mode)

One probe **per `sandbox` command**, run as a lifecycle, so each command maps to one matrix cell. The three
bundles differ only in how `setup` provisions the host:

| Bundle | Mode | Probes |
|---|---|--:|
| `baseline-op-rootless/` | operator-rootless (`setup --docker-execution-mode operator-rootless`, run as the operator) | 16 |
| `baseline-separate-user-sudo/` | separate-user + sudo (`--docker-execution-mode separate-user --machinectl-auth sudo`) | 18 |
| `baseline-separate-user-polkit/` | separate-user + polkit — tier 1 asserts `setup` *refuses* polkit; tier 2 installs the genuine `render_polkit_rule` + flips the toml, then runs the lifecycle | 18 |

Lifecycle order (after `PREP`): `setup → init → doctor → status → workspace list/add/rename → start →
status → attach → stop → workspace remove/restore → destroy`. Workspace mutations run while stopped.
Each probe emits `<CMD>_*_OK` / `_FAIL` gating tokens plus observational `_review`/`_info` tokens.
Gate is `exit $rc`; `PREP` is the only `mutating:operator-space` probe, the rest are `mutating:host-root`
(single rank → authoring order is execution order).

The separate-user·sudo bundle additionally carries two verification probes folded in from the C-009/C-010
work: `core-running` (reads docker via the reliable NON-PTY `sudo -u … env XDG_RUNTIME_DIR DOCKER_HOST
docker` crossing — not the `machinectl` PTY, which empty-crosses in-session on apt, F-018/F-055/F-097) and
`preflight-crossing-count` (asserts the C-009 8→2 read-only-crossing burst-collapse).

(The vmlease-provisioning smoke that used to sit here now lives in the vmlease repo at
`examples/compose-plugin-check/`, since it validates vmlease's own provisioning, not sandbox-ai.)

## The reference-baseline model (living, not frozen-forever)

A baseline bundle is a **reference baseline**: it reflects the **last durable (shipped) state** of the
product on the matrix. It is *not* a permanent freeze:

- **During an in-flight change's validation it is held fixed** so cell-flips are measured against a stable
  reference — and **git `HEAD` provides that freeze for free**. You edit the bundle in your working tree
  (that working copy is your "acceptance" battery), run it, and read the flips by comparing against the
  committed matrix. There is **no derive-a-copy builder** — the freeze is the commit, not a separate file.
- **When the change reaches a durable state (archives/ships), refresh the bundle and the matrix** in the
  same change so the tracked baseline tracks reality. Prior state stays recoverable via git history + the
  archived run summaries. A baseline left stale is worse than none.

So authoring is direct: edit `battery.toml`, keep `exit $rc` gating (or use `success_when = "<TOKEN>"`
for a single-token verdict), `vmlease lint` it, `vmlease plan` it. No builder scripts, no JSON.

## `baseline-matrix.md`

The tracked, publish-safe **baseline validation matrix** — the command × distro × mode verdict snapshot
(verdicts only — no host IPs or transcripts), generated from the per-run `*.summary.json` companions via
`vmlease summarize`, citing each mode's source run + date. It is the current-state record: re-run a bundle
after a durable change and regenerate the matrix so it stays honest.

## Running

```sh
# build a source tarball at the revision under test
git -C <checkout> archive --format=tar.gz <rev> -o /tmp/sandbox-ai-src.tar.gz

# lint + plan first (zero spend) — gate before ever leasing a VM
uv run --directory ../vmlease vmlease lint --battery tests/vmlease/baseline-<mode>/battery.toml --require-shellcheck
uv run --directory ../vmlease vmlease plan --battery tests/vmlease/baseline-<mode>/battery.toml --distros ubuntu

# one run per bundle (serialize runs — Hetzner Primary-IP cap; --parallel 4 covers the 4 distros)
uv run --directory ../vmlease vmlease run \
  --battery tests/vmlease/baseline-<mode>/battery.toml \
  --distros ubuntu,debian,fedora,arch --server-type cpx22 \
  --upload /tmp/sandbox-ai-src.tar.gz --operator probe --firewall <fw> \
  --run-token <slug> --results-dir <results-dir> --timestamp <YYYYMMDDtHHMMSSz> --parallel 4 \
  --ssh-key <registered-name> --ssh-key-path <local-priv> --yes

# summarize each raw result -> a versioned .summary.json companion
#   (verdict computed from the *_OK/*_FAIL tokens; process exit = overall verdict)
uv run --directory ../vmlease vmlease summarize <raw>.json --battery tests/vmlease/baseline-<mode>/battery.toml
```

Raw `vmlease-*.json` outputs + their `*.summary.json` companions are written to the run's `--results-dir`
(kept under the gitignored exploration tree — they carry host IPs / full transcripts). A real-host `run` is
billable and out-of-band; routine changes gate on `lint` + `plan` only.

## Current expected pattern

- **op-rootless** — green on every command × distro (no privilege crossing; the apt/CI mode).
- **separate-user · sudo** — **green across the board** as of C-009 (`sudo systemd-run --pipe` crossing,
  fixing the apt `start`/`doctor` F-063 empty-crossing) + C-010 (`dispatch fwd` headless `attach`, fixing
  F-060 on all 4).
- **separate-user · polkit** — red is the **durable** state: `setup` refuses to install a polkit rule, and
  even with a genuine rule + polkitd up, every crossing op needs interactive `manage-units` auth and is
  headless-blocked (F-060) — POLKIT-mode crossings require an interactive agent.

See `baseline-matrix.md` for the per-cell snapshot + source runs.
