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

`sandbox-ai` gives each project a disposable, isolated agent sandbox and puts a
real privilege boundary between you and it. The `sandbox` CLI wraps Docker
Compose lifecycles, but it **never executes a Docker call as you** — every
Docker operation is dispatched across a boundary into an *unprivileged systemd
user* via `machinectl shell`. The orchestrator you run holds no Docker access
of its own; the thing that can talk to the daemon is a user that can't touch
your home directory, your keys, or your repos.

```bash
sandbox init myproject          # scaffold an isolated, per-project sandbox
sandbox start myproject         # launch it — agent runs inside, behind the boundary
sandbox attach myproject        # drop into the running agent's environment
sandbox stop myproject          # tear it down
```

> **Status: pre-release.** The orchestrator is feature-complete and tested; the
> public CI pipeline and packaged release are still landing. Expect sharp edges
> in setup until then.

## Demos

Asciinema casts (CLI-native, copy-pasteable, no video):

- **Zero to agent (≈30s)** — `init` → `start` → an agent working inside the boundary. _(asciinema link — pending)_
- **The boundary in action (≈60s)** — watch denied operations fail at the edge: the sandbox trying to reach a host path, a secret, the network egress it isn't allowed. _(asciinema link — pending)_
- **Workspace sharing (≈60s, optional)** — bind a host checkout into the sandbox without handing over ownership. _(asciinema link — pending)_

## Why not just…?

|                                   | Dev Containers | DevPod          | e2b.dev                    | Unsandboxed (your shell) | **sandbox-ai**                        |
|-----------------------------------|----------------|-----------------|----------------------------|--------------------------|---------------------------------------|
| **Built for**                     | humans in an editor | humans, any provider | running AI-generated code | —                        | **autonomous AI coding agents**       |
| **Runs on your own host**         | yes            | yes / remote    | cloud-first (self-host available) | yes               | **yes**                               |
| **Isolation primitive**           | container      | container / VM  | Firecracker microVM        | none                     | **container behind an unprivileged-user boundary** |
| **Orchestrator holds host-root / Docker access** | yes (you drive Docker) | yes | n/a (remote service) | yes (it's just your shell) | **no — Docker calls cross into an unprivileged user** |
| **Network egress controlled by default** | no       | no              | yes (remote)               | no                       | **yes (per-instance egress policy)**  |
| **License**                       | MIT            | MPL-2.0         | Apache-2.0                 | —                        | **AGPL-3.0**                          |

The honest framing: Dev Containers and DevPod are excellent **developer**
environment managers — they were built to make *your* editor reproducible, not
to contain an untrusted actor that has your credentials. e2b is a strong
**cloud** runtime for executing AI-generated code in an ephemeral microVM, but
it lives off your machine and off your existing local workflow. And the most
common setup of all — an agent in your plain shell — has no boundary
whatsoever. `sandbox-ai` is for the case in between: an autonomous agent doing
real, persistent work **on your own host**, where you want isolation *and* a
privilege boundary without shipping your code to someone else's cloud.

## Threat model

A security tool is only as good as the boundary it draws explicitly. Here is
ours.

**Assumes:**

- A single-tenant Linux host with systemd and Docker, administered by a trusted
  operator who runs the one-time `sudo sandbox setup`.
- The host kernel, systemd, and container runtime are not already compromised.
- The operator's account and the host itself are trusted; the *agent* is not.

**Defends against:**

- An agent reading or exfiltrating the operator's host credentials, SSH keys,
  cloud config, or unrelated project files — the sandbox runs as an
  unprivileged user with no access to the operator's home or keys.
- An agent escalating to host root through the orchestrator: the orchestrator
  never executes Docker as the operator; the only path to the daemon is the
  unprivileged boundary user.
- Uncontrolled network egress: each instance gets an explicit egress policy
  rather than the open internet by default.
- Cross-project contamination: instances are isolated from one another (separate
  subnets, separate state, separate workspaces).

**Does NOT defend against** (known limitations — see [`SECURITY.md`](SECURITY.md)):

- A container-escape via a host **kernel or container-runtime 0-day** — shared-kernel
  containers are the isolation primitive here, not a hypervisor.
- A **malicious or compromised operator**, or a host that is already compromised
  before setup.
- **Dependency / supply-chain** compromise of what the agent installs *inside*
  the sandbox (a poisoned PyPI/npm package, a backdoored base image).
- Resource exhaustion / **abuse amplification** beyond the configured limits.

If your threat model includes a hostile kernel or you need hardware-level
isolation, you want a microVM (Firecracker/Kata) or a separate physical host —
not this.

## Requirements

- A Linux host with **systemd** (`machinectl` must be available) and **Docker**.
- [`uv`](https://docs.astral.sh/uv/) for the Python toolchain. The project pins
  Python 3.14 via `.python-version`; always use `uv run` or the `make` targets.
- One-time host preparation via `sudo sandbox setup`, which provisions the
  unprivileged boundary user and the privilege-crossing configuration. See
  [`docs/setup-guide.md`](docs/setup-guide.md) for the operator runbook and
  [`docs/setup.md`](docs/setup.md) for what it does under the hood.

Specific distro support is being validated; follow the setup guide for the
currently exercised configuration rather than assuming your distro is covered.

## Quick start

```bash
# 1. One-time host prep (provisions the unprivileged boundary user).
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

Run `sandbox doctor` at any time for host-readiness diagnostics.

## Architecture

The orchestrator is small, deterministic, and built around one load-bearing
idea — the privilege boundary. The documentation is split by concern:

- [docs/architecture.md](docs/architecture.md) — project overview and the
  `src/core/` module map.
- [docs/privilege-boundary.md](docs/privilege-boundary.md) — the load-bearing
  boundary: how `machinectl shell` crossings work and their PTY/PAM consequences.
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
