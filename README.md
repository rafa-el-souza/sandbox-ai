# sandbox-ai

**Zero-trust sandboxing for AI coding agents.**

You hand a coding agent a task and walk away. While you're gone it runs with
*your* privileges on *your* machine: it can read `~/.ssh`, `~/.aws`, and every
`.env` you've ever left lying around; `git push` anywhere your token reaches;
`curl | sh` whatever it decides it needs; and `rm -rf` anything your user owns.
The blast radius of one bad tool-call, one prompt-injected README, one
hallucinated `sudo`, is your entire account.

The usual answer is "review every command" — which defeats the point of an
agent — or "run it in Docker," which just means the agent now drives a Docker
daemon that *is* root on your host. Most setups have **no boundary at all**.

*Jump to: [Why not just…?](#why-not-just) · [Threat model](#threat-model) · [Requirements](#requirements) · [Quick start](#quick-start) · [Two execution modes](#two-execution-modes) · [Architecture](#architecture)*

## Why not just…?

|                                   | Dev Containers | DevPod          | e2b.dev                    | Unsandboxed (your shell) | **sandbox-ai**                        |
|-----------------------------------|----------------|-----------------|----------------------------|--------------------------|---------------------------------------|
| **Built for**                     | humans in an editor | humans, any provider | running AI-generated code | —                        | **autonomous AI coding agents**       |
| **Runs on your own host**         | yes            | yes / remote    | cloud-first (self-host available) | yes               | **yes**                               |
| **Isolation primitive**           | container      | container / VM  | Firecracker microVM        | none                     | **rootless-daemon container + gVisor + userns** |
| **Docker daemon runs as root**    | yes (typical)  | yes (typical)   | n/a (remote service)       | yes (typical)            | **no — rootless in both modes**       |
| **Orchestrator runs Docker as you** | yes (you drive Docker) | yes | n/a (remote service) | yes (it's just your shell) | **default: yes (as your *rootless* user) · hardened: no (crosses into a dead-end user)** |
| **Network egress controlled by default** | no       | no              | yes (remote)               | no                       | **yes (deny-by-default: allowlisting proxy + filtering DNS)** |
| **License**                       | MIT            | MPL-2.0         | Apache-2.0                 | —                        | **AGPL-3.0**                          |

The honest framing: Dev Containers and DevPod are **developer** environment
managers — they were built to make *your* editor reproducible, not to contain an
untrusted actor that has your credentials. e2b is a **cloud** runtime for
executing AI-generated code in an ephemeral microVM, but it lives off your
machine and off your existing local workflow. And the most common setup of all —
an agent in your plain shell — has no boundary whatsoever. `sandbox-ai` is for
the case in between: an autonomous agent doing real, persistent work **on your
own host**, where you want isolation *and* a privilege boundary without shipping
your code to someone else's cloud.

It gives each project a disposable, isolated agent sandbox built on a **rootless
Docker daemon** — so a container escape that reaches the daemon lands on an
*unprivileged* uid, never host root. On that foundation every agent container
runs under the **gVisor (`runsc`) runtime**, behind **deny-by-default network
egress** (an allowlisting proxy + a filtering DNS resolver) and per-instance
isolated subnets, with a hardened container baseline and ACL-gated host
filesystem access. That isolation is load-bearing, and it holds the same way in
both execution modes: the **default `operator-rootless`** (rootless daemon as
your own user, no boundary crossing) and an **opt-in hardened `separate-user`**
for adversarial-agent and multi-tenant threat models ([details ↓](#two-execution-modes)).

> **Status: pre-release.** The core orchestrator works end-to-end and is
> well-tested (100% coverage on `core/` and `cli/`). Non-interactive setup, the
> public CI pipeline, and a packaged release are still landing. Expect sharp
> edges in setup until then.

<details>
<summary>Demos</summary>

Coming soon — asciinema casts (CLI-native, copy-pasteable, no video):

- **Zero to agent (≈30s)** — `init` → `start` → an agent working inside the boundary.
- **The boundary in action (≈60s)** — denied operations failing at the edge: the sandbox reaching for a host path, a secret, or network egress it isn't allowed.
- **Workspace sharing (≈60s)** — bind a host checkout into the sandbox without handing over ownership.
</details>

## Threat model

A security tool is only as good as the boundary it draws. Here is ours.

**Assumes:**

- A single-tenant Linux host with systemd and Docker, administered by a trusted
  operator who runs the one-time `sudo sandbox setup`.
- The host kernel, systemd, and container runtime are not already compromised.
- The operator's account and the host itself are trusted; the *agent* is not.

**Defends against:**

- An agent reading or exfiltrating the operator's host credentials, SSH keys,
  cloud config, or unrelated project files — the daemon owner has no access to
  the operator's home or keys, and the instance tree is ACL-gated.
- An agent escalating to host root through the daemon: the Docker daemon is
  **rootless in both modes**, so an escape that reaches it lands on an
  unprivileged uid, not root. In `separate-user` mode the orchestrator
  additionally never executes Docker as the operator — the only path to the
  daemon is the dedicated, dead-end boundary user.
- Uncontrolled network egress: egress is deny-by-default — each instance routes
  through an allowlisting proxy and a filtering DNS resolver, not the open
  internet.
- Cross-project contamination: instances are isolated from one another (separate
  subnets, separate state, separate workspaces).

**Does NOT defend against** (known limitations — see [`SECURITY.md`](SECURITY.md)):

- A container-escape via a host **kernel or container-runtime 0-day** — shared-kernel
  containers (even under gVisor) are the isolation primitive here, not a hypervisor.
- In `operator-rootless` mode, a **sudoer daemon owner** reaching root after a
  (rare, gVisor-fronted) escape — an informed-tradeoff **WARN**, not a failure;
  remedied by a non-sudo operator or `separate-user` mode.
- A trustworthy **view** of the sandbox while attached — `sandbox attach`
  guarantees only that the session can't escalate back to your host, not that the
  view is un-tampered (terminal-escape, tlog-replay, and typed-secret visibility
  are documented residuals).
- A **malicious or compromised operator**, or a host that is already compromised
  before setup.
- **Dependency / supply-chain** compromise of what the agent installs *inside*
  the sandbox (a poisoned PyPI/npm package, a backdoored base image).
- Resource exhaustion / **abuse amplification** beyond the configured limits.

If your threat model includes a hostile kernel or you need hardware-level
isolation, you want a microVM (Firecracker/Kata) or a separate physical host —
not this.

**Full security model** — the privilege boundary, deny-by-default network
isolation, container hardening, the ACL model, and secrets handling, with
diagrams: [`docs/security-model.md`](docs/security-model.md).

## Requirements

- A Linux host with **systemd** and **rootless Docker** (the opt-in
  `separate-user` mode additionally requires `machinectl`/`systemd-run` for its
  privilege crossing).
- [`uv`](https://docs.astral.sh/uv/) for the Python toolchain. The project pins
  Python 3.14 via `.python-version`; always use `uv run` or the `make` targets.
- One-time host preparation via `sudo sandbox setup`, which provisions rootless
  Docker, the gVisor runtime, and — for `separate-user` mode — the dedicated
  boundary user and privilege-crossing configuration. See
  [`docs/setup-guide.md`](docs/setup-guide.md) for the operator runbook and
  [`docs/setup.md`](docs/setup.md) for what it does under the hood.

Specific distro support is being validated; follow the setup guide for the
currently exercised configuration rather than assuming your distro is covered.

## Quick start

```bash
# 1. One-time host prep (rootless Docker + gVisor; the dedicated boundary user
#    is added only in the opt-in separate-user mode).
sudo sandbox setup

# 2. Scaffold an isolated sandbox for a project.
sandbox init myproject

# 3. Launch it. The agent runs inside, behind the privilege boundary.
sandbox start myproject

# 4. Work with it.
sandbox attach myproject     # reconnect to the running sandbox
sandbox status myproject     # health + diagnostics
sandbox stop myproject       # graceful stop
sandbox destroy myproject    # remove the instance entirely
```

**Trying it on a throwaway dev box?** Take the default — you don't choose or
configure a mode: `sudo sandbox setup` provisions `operator-rootless` and you're
ready to `init`. Run `sandbox doctor` at any time for host-readiness diagnostics.

## Two execution modes

Rootless by default; dead-end user when you need it. The isolation foundation
above — rootless daemon, gVisor, userns, deny-by-default egress, container
hardening, ACL-gated filesystem — is **the same in both modes**. What differs is
*who owns the Docker daemon* and whether ops cross a boundary:

- **`operator-rootless` (default).** Rootless Docker runs as **your own user**; ops
  are local subprocesses with no crossing. Best for single-operator dev boxes.
  Caveat: if your operator account is a **sudoer**, a (rare, gVisor-fronted) escape
  reaching the daemon owner could reach root — `sandbox doctor` flags this as an
  informed-tradeoff **WARN (never a failure)**, with two remedies: run as a
  dedicated non-sudo operator, or switch to `separate-user`.
- **`separate-user` (opt-in hardened).** Rootless Docker runs as a **dedicated,
  dead-end unprivileged user**; the orchestrator holds no Docker access and crosses
  a privilege boundary (a root-owned dispatcher, per-op sudoers authorization) for
  every op. For adversarial-agent and multi-tenant threat models.

The mode is chosen at `sudo sandbox setup` time. Full detail:
[`docs/privilege-boundary.md`](docs/privilege-boundary.md).

## Architecture

The orchestrator is small, deterministic, and built around one load-bearing
idea — the privilege boundary. The documentation is split by concern:

- [docs/architecture.md](docs/architecture.md) — project overview and the
  `src/core/` module map.
- [docs/privilege-boundary.md](docs/privilege-boundary.md) — the two execution
  modes and the privilege boundary itself: rootless-by-default, the opt-in
  `separate-user` crossing (`sudo systemd-run --pipe` → root-owned dispatcher),
  the 12-op dispatcher surface, and the PTY/PAM consequences.
- [docs/dispatcher.md](docs/dispatcher.md) — the dispatcher op reference and how
  to add a new orchestrator-to-sandbox operation.
- [docs/configuration.md](docs/configuration.md) — the per-host and per-instance
  configuration scopes.
- [docs/acl-model.md](docs/acl-model.md) — the ACL / ownership model.
- [docs/locking.md](docs/locking.md) — orchestrator state layout and lock
  topology.
- [docs/setup.md](docs/setup.md) / [docs/setup-guide.md](docs/setup-guide.md) —
  `sudo sandbox setup` internals and the operator runbook.
- [docs/testing.md](docs/testing.md) — test, coverage, lint, and typecheck
  conventions.

## Contributing

Contributions are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
development workflow and the test/coverage gate. Contributions are accepted
under the project's AGPL-3.0-or-later license (inbound = outbound) — there is
**no CLA to sign**. Security reports: see [`SECURITY.md`](SECURITY.md).

## License

`sandbox-ai` is licensed under the **GNU Affero General Public License v3.0 or
later** (AGPL-3.0-or-later). See [`LICENSE`](LICENSE).

---

![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue)
![Python 3.14](https://img.shields.io/badge/python-3.14-blue)
![Status: pre-release](https://img.shields.io/badge/status-pre--release-orange)
