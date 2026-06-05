# Real-host validation batteries (vmlease)

These are **[vmlease](https://github.com/zerotrust-ai/vmlease) probe batteries** that validate sandbox-ai
end-to-end on **fresh, real cloud hosts** (ubuntu / debian-13 / fedora / arch) — the "ratified ≠ validated"
gate that unit/integration tests can't cover (privilege boundary, rootless docker, gVisor, the full
`setup → init → start → attach → stop → destroy` lifecycle).

They are **not** collected by `pytest` (`pytest.testpaths = ["tests/unit"]`); they're run by hand against
billable Hetzner VMs. They live here (tracked) so the hard-won per-distro recipes aren't lost — the working
exploration lives, untracked, under `openspec/explorations/ongoing/…/probes/`.

## Batteries

| File | Proves |
|---|---|
| `oprootless-full-chain.json` | The operator-rootless lifecycle on 4 distros: `sandbox setup → init → start (core running) → attach (agent@/workspaces/main) → stop → destroy`. Encodes the per-distro `tlog-rec` prep (incl. the debian-13 from-source build + the arch AUR path), the op-rootless session-env prefix, dummy secret-seeding, the `script(1)`-driven non-interactive `attach`, and the non-interactive `destroy` flags. |
| `vmlease-compose-plugin-check.json` | A freshly-provisioned host has the docker prerequisites vmlease installs: `docker compose` (v2 plugin), `docker buildx`, and `script(1)` (fedora splits it into `util-linux-script`). No source upload needed. |

## Running

vmlease is provider-token-blind — it uses your already-active `hcloud` context.

```bash
# 1. Build the source tarball from the branch under test (the battery uploads + `uv sync`es it).
git -C <this-worktree> archive --format=tar.gz HEAD -o /tmp/sandbox-ai-src.tar.gz

# 2. Run the full lifecycle on all 4 distros (billable — provisions + ALWAYS tears down).
vmlease run \
  --battery tests/vmlease/oprootless-full-chain.json \
  --distros ubuntu,debian,fedora,arch \
  --upload /tmp/sandbox-ai-src.tar.gz \
  --operator probe \
  --results-dir <results-dir> \
  --run-token <slug> --timestamp "$(date -u +%Y%m%dT%H%M%SZ)" \
  --ssh-key <hcloud-key-name> --ssh-key-path <local-private-key> \
  --firewall <firewall-name> --parallel 4 --yes

# Prereq-only check (no upload, read-only):
vmlease run --battery tests/vmlease/vmlease-compose-plugin-check.json --distros ubuntu,debian,fedora,arch \
  --operator probe --results-dir <dir> --run-token prereq --timestamp "$(date -u +%Y%m%dT%H%M%SZ)" \
  --ssh-key <name> --ssh-key-path <path> --firewall <fw> --parallel 4 --yes
```

`vmlease plan …` (same args) is a zero-spend dry run that lints the battery first.

## Reading results

The results JSON is `{hosts: [{distro, probes: [{id, ok, exit_code, stdout, stderr, tag}]}]}`. A green run has
every probe `exit_code == 0` and no `*_FAIL` tokens in `stdout`; assertions are emitted as `…_OK` / `…_FAIL`
tokens per probe (e.g. `START_CORE_RUNNING_OK`, `ATTACH_REACHED_WORKSPACE_OK`, `DESTROY_EXIT0_OK`).

## Notes / known frictions

- The `tlog-rec` + `uv` install repeated in each battery's `PREP` is host-prep that belongs in a vmlease
  *per-distro battery prep* feature — tracked in vmlease's backlog ("Distro-aware, battery-declared host
  prep"). Until that lands, the recipe stays inline here.
- `docker compose` / `buildx` / `script(1)` are provided by **vmlease's** per-distro host prep
  (`docker-compose-plugin`, `docker-buildx-plugin`, `util-linux-script`), not by these batteries.
- Probe `ok` is the command's exit code; assertions thread `rc` and `exit $rc`. Gate on the `*_OK`/`*_FAIL`
  tokens, not just `ok` (see vmlease's README footguns).
