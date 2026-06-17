# Real-host validation batteries (vmlease)

These are **[vmlease](https://github.com/zerotrust-ai/vmlease) probe batteries** that validate sandbox-ai
end-to-end on **fresh, real cloud hosts** (ubuntu / debian-13 / fedora / arch) — the "ratified ≠ validated"
gate that unit/integration tests can't cover (the privilege boundary, rootless docker, gVisor, the full
`sandbox` command surface, and host-level shared-namespace allocation across *multiple* operators).

They are **not** collected by `pytest` (`pytest.testpaths = ["tests/unit"]`); they're run by hand against
billable Hetzner VMs. They live here (tracked) so the canonical set isn't lost — the live working area,
plus the raw run outputs, stay **untracked** under `openspec/explorations/ongoing/…/probes/` (gitignored).

Each battery is a **vmlease TOML bundle directory**: a `battery.toml` manifest plus a co-located `prep/`
directory of shell scripts it references. `vmlease lint` shellchecks every probe *and* every prep script
with real `file:line:col`; `vmlease plan` loads + resolves the bundle with zero provider calls. (vmlease's
battery format is TOML-only — there is no JSON loader.)

## The four bundles

| Bundle | Mode / arc | Probes | What it pins |
|---|---|--:|---|
| `baseline-op-rootless/` | operator-rootless, run as the operator | 15 | the no-privilege-crossing `sandbox` lifecycle (apt/CI mode) |
| `baseline-separate-user-sudo/` | separate-user + `sudo systemd-run --pipe` crossing | 17 | the hardened crossing lifecycle (+ the C-009/C-010 verification probes) |
| `multi-operator-oprootless-first/` | two+ operators, op-rootless as op1 | 9 | F-070 (per-operator `sb-ws` bridge gid) + F-071 (per-operator subid ranges + overlap detection) |
| `multi-operator-separate-user-first/` | two+ operators, separate-user as op1 | 8 | F-070 + F-071 with the separate-user operator provisioning first |

### The single-operator baselines (per-command lifecycle)

One probe **per `sandbox` command**, run as a lifecycle so each command maps to one matrix cell. Lifecycle
order: `setup → init → doctor → status → workspace list/add/rename → start → status → attach → stop →
workspace remove/restore → destroy`. Workspace mutations run while stopped. The two bundles differ only in
how `setup` provisions the host (`--docker-execution-mode operator-rootless` vs `separate-user`).

The `separate-user-sudo` bundle additionally carries two verification probes folded in from C-009/C-010:
`core-running` (reads docker via the reliable NON-PTY `sudo -u … env XDG_RUNTIME_DIR DOCKER_HOST docker`
crossing — not the `machinectl` PTY, which empty-crosses in-session on apt, F-018/F-055) and
`preflight-crossing-count` (asserts the C-009 8→2 read-only-crossing burst-collapse).

### The multi-operator bundles (C-013 F-070 / F-071)

The single-operator baselines run as ONE operator (the vmlease `probe` user); they cannot surface a
collision that only manifests when a **second** operator provisions on the same host. These two bundles
create separate per-bug OS operators (`op2`, `op3f`, `op4`) — useradd + sudoers + linger + a per-operator
`uv sync` — via the `mk-operators` prep step, then drive each operator's own `sandbox setup`. The key
probes:

- `op2-bridge-gid-assert` — **F-070**: a second operator's `sb-ws-<op>` bridge gid lands *within that
  operator's* `/etc/subgid` range (per-operator, not host-wide).
- `op3f-loud-assert` — an entry-less operator's op-rootless setup fails **loud** (`NoSubgidRangeError`),
  not silently.
- `mixed-subid-overlap-and-guard` — **F-071**: a fourth operator (`op4`) gets a subid range that does NOT
  silently overlap op1's, and the L2 bridge-gid guard still holds. (`op4`'s `setup` stays an ordered probe
  — it reads `/tmp/op1.subuid` written by `op1-setup`; hoisting it would invert the F-071 coverage.)

## Bundle schema (vmlease `requires` / `[prep]`)

Every bundle declares its host prerequisites declaratively — docker is **default-off** in vmlease, so each
bundle opts in:

- `requires = ["docker"]` — installs docker-ce + rootless extras into the host's cloud-init (and is part of
  the image cache key, so a `requires=["docker"]` cached image is distinct from a docker-less one).
- `[prep.packages]` — extra OS packages (`util-linux`/`bsdutils` for the PTY tooling `attach` drives).
- `[[prep.setup]]` — ordered setup steps run once after readiness, before the probe loop:
  - `uv-install` → `uv-sync` (extracts the **uploaded source tarball** and `uv sync`s the `sandbox` CLI),
  - `install-helpers` (the shared `/tmp/sbai-helpers.sh` — bridge-group resolver + forensic dump +
    teardown sentinel),
  - `mk-operators` (multi-operator bundles only — provisions op2/op3f/op4),
  - `tlog` (`required = false` — a SOFT step: tlog-rec is a hard product prereq, but a soft prep step lets
    the harness OBSERVE L0's refusal as a probe verdict rather than tearing the host down with zero probes
    on a distro whose tlog build is fragile).

Probes are authored inline (`run = '''…'''`), tagged `mutating:host-root` (the single rank → authoring
order is execution order), and gate with **`exit $rc`** over `*_OK` / `*_FAIL` tokens plus observational
`_review` / `_info` tokens. `vmlease summarize` computes the verdict: a `*_FAIL` token or non-zero exit →
`FAIL`; a zero exit with an `*_OK` token → `PASS`; a zero exit with neither → `PASS_NO_ASSERTIONS` (the
provisioning/teardown probes).

> **Stopgap helpers (F-073).** `prep/install-helpers.sh` (→ `/tmp/sbai-helpers.sh`) and the multi-bundle
> suite orchestration are **battery-level stopgaps** for harness mechanisms vmlease does not yet own
> (failure-forensics capture + teardown gating, a `_skip` verdict, a multi-battery suite runner). They are
> filed as vmlease backlog items carrying the string "F-073"; once they land in vmlease, the helper shrinks
> to just the product-domain resolver. Do not mistake the stopgaps for the permanent design.

## The reference-baseline model (living, not frozen-forever)

A baseline bundle reflects the **last durable (shipped) state** of the product on the matrix. It is *not* a
permanent freeze:

- **During an in-flight change's validation it is held fixed** so cell-flips are measured against a stable
  reference — and **git `HEAD` provides that freeze for free**. You edit the bundle in your working tree
  (that working copy is your "acceptance" battery), run it, and read the flips by comparing against the
  committed matrix. There is **no derive-a-copy builder** — the freeze is the commit, not a separate file.
- **When the change reaches a durable state (archives/ships), refresh the bundle and the matrix** in the
  same change so the tracked baseline tracks reality. Prior state stays recoverable via git history + the
  archived run summaries. A baseline left stale is worse than none.

So authoring is direct: edit `battery.toml` (and its `prep/*.sh`), keep `exit $rc` gating, `vmlease lint`
it, `vmlease plan` it. No builder scripts, no JSON.

## `baseline-matrix.md`

The tracked, publish-safe **baseline validation matrix** — the command × distro × mode verdict snapshot
(verdicts only — no host IPs or transcripts), generated from the per-run `*.summary.json` companions via
`vmlease summarize`, citing each mode's source run + date. Re-run a bundle after a durable change and
regenerate the matrix so it stays honest.

## Running

A real-host `run` is billable and out-of-band; routine changes gate on `lint` + `plan` only. **Always
pass absolute battery paths** — the commands below run vmlease via `uv run --directory ../vmlease`, which
changes the CWD, so a relative `tests/vmlease/…` path would not resolve.

```sh
SBAI=/home/dev/projects/sandbox-ai            # adjust to your checkout
VML=../vmlease                                 # the vmlease repo
RESULTS=$SBAI/openspec/explorations/ongoing/<change>/probes/results

# 0. build a source tarball at the revision under test (uv-sync extracts it on the host)
git -C "$SBAI" archive --format=tar.gz HEAD -o /tmp/sandbox-ai-src.tar.gz

# 1. lint + plan first (zero spend) — gate before ever leasing a VM
uv run --directory "$VML" vmlease lint --battery "$SBAI/tests/vmlease/<bundle>/battery.toml" --require-shellcheck
uv run --directory "$VML" vmlease plan --battery "$SBAI/tests/vmlease/<bundle>/battery.toml" \
        --distros ubuntu,debian,fedora,arch --upload /tmp/sandbox-ai-src.tar.gz --run-token <slug>
```

### The smart way: build an image cache first

The slow, fragile part of every run is OS prep (arch's multi-minute rescue-write; every distro's docker +
package install via cloud-init). Bake it **once** into a per-distro snapshot, then every `run` restores
from the prepped disk in ~30s and re-runs only the battery's own `[prep.setup]` (uv sync, operator
creation) on top. Build the **`requires=["docker"]`** variant — it must match what the batteries declare:

```sh
# build one docker-capable cache image per distro (idempotent; --rebuild to replace)
for d in ubuntu debian fedora arch; do
  uv run --directory "$VML" vmlease build-image --distro "$d" --requires docker \
          --server-type cpx22 --operator probe --firewall <fw> --run-token cache-build \
          ${ARCH_KEYS:+--ssh-key <name> --ssh-key-path <local-priv>} --yes   # ssh-key only needed for arch
done
```

- A `run` then **automatically restores** a matching cached image (same distro + arch + docker recipe) and
  records `restored_image` in the raw results — a cold miss (`null`) just falls back to the slow path, never
  breaks. arch (rescue-write) needs the two SSH keys up front; ubuntu/debian/fedora are native and need none.
- Prune the cache with `vmlease reap-images --distro <d>` / `--superseded` (a bare `reap-images` is refused).

### Run + summarize

```sh
# one run per bundle. --parallel covers the distros within a run; run separate BUNDLES serially
# (Hetzner Primary-IP cap), or launch them as concurrent background processes (≤ a few at once).
uv run --directory "$VML" vmlease run \
  --battery "$SBAI/tests/vmlease/<bundle>/battery.toml" \
  --distros ubuntu,debian,fedora,arch --server-type cpx22 \
  --upload /tmp/sandbox-ai-src.tar.gz --operator probe --firewall <fw> \
  --run-token <slug> --results-dir "$RESULTS" --timestamp <YYYY-MM-DDTHHMM> --parallel 4 \
  ${ARCH:+--ssh-key <name> --ssh-key-path <local-priv>} --yes      # ssh-key only when the matrix includes arch

# summarize each raw result -> a versioned .summary.json companion; ITS exit code is the gate
uv run --directory "$VML" vmlease summarize "$RESULTS/vmlease-<slug>-<ts>.json" \
        --battery "$SBAI/tests/vmlease/<bundle>/battery.toml"; echo $?   # 0 = green

# always confirm no billable host leaked
uv run --directory "$VML" vmlease status --run-token <slug>             # expect 0 live
```

> **Gate on `summarize`, never on `run`.** `run`'s exit reflects only provisioning/teardown mechanics. And
> read `restored_image` + per-host probe **counts**: a host that errors at readiness/restore runs zero
> probes — historically `summarize` could still exit 0 (a hollow green), so assert `probes > 0` per host
> until that gap closes in vmlease (F-075).

Raw `vmlease-*.json` outputs + their `*.summary.json` companions are written to `--results-dir` (kept under
the gitignored exploration tree — they carry host IPs / full transcripts).

## Current expected pattern

- **op-rootless** & **separate-user · sudo** — green on every command × distro (separate-user green as of
  C-009 `sudo systemd-run --pipe` + C-010 headless `attach`).
- **multi-operator** (C-013) — F-070 (`op2-bridge-gid-assert`) + F-071 (`op3f-loud-assert`,
  `mixed-subid-overlap-and-guard`) green; these validate the *allocation mechanisms*. The multi-operator
  **runtime** is a known, tracked failure (**F-074**, `⊘` in the matrix): a second operator's core container
  exits 126 (bind-mounted entrypoint exec-denied, subuid-base-dependent) on **all 4 distros** and **both**
  multi-operator bundles. The `op2-start` probe gates only on F-070 and records the exit-126 observationally
  (vmlease has no `xfail` verdict, F-073) — so **a green `op2-start` cell does NOT mean op2's core runs.**
  Simultaneous multi-operator runtime is a separate follow-up, not a C-013 deliverable.

See `baseline-matrix.md` for the per-cell snapshot + source runs.
