# E-005 baseline matrix — `sandbox` command × distro × mode

Pre-fix baseline (code `acbd55f`, the clean E-005 unit — no B retry, no C gate). Generated from the per-run `.summary.json` companions via `vmlease summarize`. ✅ PASS · ❌ FAIL.

## op-rootless

| command | ubuntu | debian | fedora | arch |
|---|:--:|:--:|:--:|:--:|
| `setup` | ✅ | ✅ | ✅ | ✅ |
| `doctor` | ✅ | ✅ | ✅ | ✅ |
| `init` | ✅ | ✅ | ✅ | ✅ |
| `status` | ✅ | ✅ | ✅ | ✅ |
| `workspace list` | ✅ | ✅ | ✅ | ✅ |
| `workspace add` | ✅ | ✅ | ✅ | ✅ |
| `workspace rename` | ✅ | ✅ | ✅ | ✅ |
| `start` | ✅ | ✅ | ✅ | ✅ |
| `attach` | ✅ | ✅ | ✅ | ✅ |
| `stop` | ✅ | ✅ | ✅ | ✅ |
| `workspace remove` | ✅ | ✅ | ✅ | ✅ |
| `workspace restore` | ✅ | ✅ | ✅ | ✅ |
| `destroy` | ✅ | ✅ | ✅ | ✅ |

## separate-user · SUDO

| command | ubuntu | debian | fedora | arch |
|---|:--:|:--:|:--:|:--:|
| `setup` | ✅ | ✅ | ✅ | ✅ |
| `doctor` | ❌ | ❌ | ✅ | ✅ |
| `init` | ✅ | ✅ | ✅ | ✅ |
| `status` | ✅ | ✅ | ✅ | ✅ |
| `workspace list` | ✅ | ✅ | ✅ | ✅ |
| `workspace add` | ✅ | ✅ | ✅ | ✅ |
| `workspace rename` | ✅ | ✅ | ✅ | ✅ |
| `start` | ❌ | ❌ | ✅ | ✅ |
| `attach` | ❌ | ❌ | ❌ | ❌ |
| `stop` | ✅ | ✅ | ✅ | ✅ |
| `workspace remove` | ✅ | ✅ | ✅ | ✅ |
| `workspace restore` | ✅ | ✅ | ✅ | ✅ |
| `destroy` | ✅ | ✅ | ✅ | ✅ |

## separate-user · POLKIT

| command | ubuntu | debian | fedora | arch |
|---|:--:|:--:|:--:|:--:|
| `setup` | ✅ | ❌ | ✅ | ✅ |
| `doctor` | ❌ | ❌ | ❌ | ❌ |
| `init` | ❌ | ❌ | ❌ | ❌ |
| `status` | ❌ | ❌ | ❌ | ❌ |
| `workspace list` | ❌ | ❌ | ❌ | ❌ |
| `workspace add` | ❌ | ❌ | ❌ | ❌ |
| `workspace rename` | ❌ | ❌ | ❌ | ❌ |
| `start` | ❌ | ❌ | ❌ | ❌ |
| `attach` | ❌ | ❌ | ❌ | ❌ |
| `stop` | ❌ | ❌ | ❌ | ❌ |
| `workspace remove` | ❌ | ❌ | ❌ | ❌ |
| `workspace restore` | ❌ | ❌ | ❌ | ❌ |
| `destroy` | ❌ | ❌ | ❌ | ❌ |

## Sources + totals

- **op-rootless** — `results/vmlease-e005-base-oprootless-v3-20260609T134818Z.summary.json` — {'FAIL': 0, 'PASS': 64, 'PASS_NO_ASSERTIONS': 0, 'TIMEOUT': 0}
- **separate-user · SUDO** — `results/vmlease-e005-base-su-sudo-v2-20260609T143818Z.summary.json` — {'FAIL': 8, 'PASS': 56, 'PASS_NO_ASSERTIONS': 0, 'TIMEOUT': 0}
- **separate-user · POLKIT** — `results/vmlease-e005-base-su-polkit-20260609T144922Z.summary.json` — {'FAIL': 57, 'PASS': 15, 'PASS_NO_ASSERTIONS': 0, 'TIMEOUT': 0}

## Reading the reds (all confirmed real, not probe artifacts)

- **op-rootless:** 64/64 green — the apt/CI mode, no privilege crossing.
- **separate-user · SUDO:** `start`/`doctor` fail on **apt (ubuntu/debian)** — the F-063 `machinectl` PTY empty-crossing (`user-early` vs `user-light`, captured in the probe `_review` tokens); fedora/arch green. `attach` fails on **all 4** — its unprivileged `systemd-run` ProxyCommand needs `manage-units` polkit auth, **headlessly blocked (F-060)**, independent of the apt bug. `setup` **converges on all 4** (incl. apt) — refines F-055: the apt failure is runtime-only, not a setup wall.
- **separate-user · POLKIT:** `setup` refuses polkit (correct); even with the genuine `machine1.shell` rule + polkitd running, every crossing op fails **headlessly** (*interactive authentication required*) — F-060 confirmed with the `polkit1`/`polkit2` confounds removed. (debian also lacks `polkit.service` on the minimal image.)
