# Security Policy

`sandbox-ai` is a security tool. Its job is to put a privilege boundary between
you and an autonomous agent running on your host. This document states what that
boundary covers, what it deliberately does **not**, and how to report a problem.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for an
unfixed vulnerability.

- Use GitHub's **"Report a vulnerability"** flow (the repository's **Security**
  tab → *Report a vulnerability*), which opens a private security advisory.

Include the version / commit, your host OS and configuration, and a minimal
reproduction. We aim to acknowledge a report within a few days and will
coordinate a fix and disclosure timeline with you. There is no bug-bounty
program at this stage.

## Supported versions

`sandbox-ai` is **pre-release**. Until the first tagged release, only the
`main` branch is supported — security fixes land there. Versioned support
policy will be defined at the first release.

## What the boundary defends

The threat model is summarized in the [README](README.md#threat-model) and
detailed — with diagrams — in [`docs/security-model.md`](docs/security-model.md).
In short, `sandbox-ai` assumes a single-tenant Linux host with a trusted operator
and an **untrusted agent**, and it defends against the agent:

- reading or exfiltrating the operator's credentials, keys, cloud config, or
  unrelated project files;
- escalating to host root through the daemon (the Docker daemon is **rootless in
  both execution modes**, so an escape that reaches it lands on an unprivileged
  uid; in the opt-in `separate-user` mode the orchestrator additionally never runs
  Docker as the operator — the only path to the daemon is a dedicated, dead-end
  boundary user);
- making uncontrolled network egress (egress is deny-by-default — an
  allowlisting proxy plus a filtering DNS resolver);
- contaminating other projects (per-instance isolation of subnets, state, and
  workspaces).

## Known limitations (out of scope)

These are **deliberate, documented gaps**, not oversights. Each is a boundary of
the current design; where there is a realistic path to closing it, a
*Revisit when* condition is noted below.

- **Host kernel / container-runtime 0-day.** Isolation here is shared-kernel
  containers, not a hypervisor. A container-escape via a kernel or runtime
  vulnerability is out of scope. If you need hardware-level isolation, use a
  microVM (Firecracker/Kata) or a dedicated host. *Revisit when:* a microVM
  backend is added.
- **Sudoer daemon owner in `operator-rootless` mode.** The default mode runs the
  rootless daemon as the operator's own user; if that account is a **sudoer**, a
  (rare, gVisor-fronted) escape that reaches the daemon owner could `sudo` → root.
  This is an **informed-tradeoff WARN, never a failure** — `sandbox doctor` flags
  it with two named remedies: run as a dedicated **non-sudo** operator account, or
  switch to the **`separate-user`** mode (whose daemon user is a dead-end account
  with no sudo path). It is specific to `operator-rootless`; `separate-user` has no
  equivalent exposure.
- **A trustworthy *view* of the sandbox while attached.** `sandbox attach`
  guarantees **non-escalation** (a session cannot be used to reach back into the
  operator's host) — but **not** a trustworthy view of the sandbox, which is
  unattainable at this layer because the ssh endpoint lives inside the plane the
  agent controls. **Terminal escape sequences**, the **tlog-replay hazard**, and
  **typed-secret visibility** (a secret typed during an attach session is visible
  to the plane) are accepted, documented residuals — see
  [`docs/security-model.md`](docs/security-model.md) § "Attach & the streaming
  `fwd` op".
- **Dependency / supply-chain compromise.** A poisoned package (PyPI / npm / Go
  modules), a backdoored base image, or a malicious OS package that the agent or
  the build installs *inside* the sandbox is not something the boundary inspects.
  Automated dependency scanning is deferred. *Revisit when:* a dependency-scanning
  workstream is picked up.
- **A compromised or malicious operator / host.** The operator is trusted by
  assumption. A host that is already compromised before `sudo sandbox setup`, or
  an operator acting in bad faith, is outside the model.
- **Maintainer / account compromise of the project itself.** Long-game social
  engineering of a maintainer (the "xz" pattern), or compromise of the project's
  source-forge account, is a supply-chain risk on *us*, mitigated by process
  (review, history hygiene, account security) rather than by the runtime
  boundary.
- **Release-pipeline compromise.** Signed tags, provenance/attestation
  (e.g. cosign), and reproducible builds are **not yet** in place — the release
  pipeline is a separate trust surface from the runtime. *Revisit when:* the
  release-engineering workstream lands (see Roadmap).
- **Unenforced gVisor resource limits under the rootless daemon.** A sandbox's
  configured `cpus` / `mem_limit` are **render-time-only for gVisor containers, not
  runtime-enforced**: under both `operator-rootless` and `separate-user` the Docker
  daemon runs rootless, and gVisor's `runsc` cannot create its per-container cgroup
  scope rootless (it reaches the *system* systemd bus and is auth-denied), so it is
  run with `--ignore-cgroups`. A container can therefore exceed its configured
  CPU/memory and apply resource pressure on its (single-tenant) host — a local DoS /
  noisy-neighbour effect, **not** a sandbox escape or a cross-tenant issue.
  `sandbox doctor` surfaces this with an advisory over-commit WARN when an instance's
  summed `mem_limit` exceeds host RAM. *Revisit when:* gVisor gains rootless
  systemd-cgroup support (a `runsc` release where rootless scope creation succeeds) —
  see Roadmap.
- **Cost / abuse amplification.** Beyond the per-instance gVisor limit above,
  general resource exhaustion or abuse — and, for the project's own CI, mass-spawn
  amplification — is bounded by configuration and host sizing, not eliminated.
- **Telemetry side-channels.** The project avoids third-party coverage/telemetry
  services to keep the trust surface small; this is a posture choice, not a
  defended boundary.

The planned CI pipeline (see Roadmap) will extend this posture to the project's
own builds — per-job ephemeral VMs, no credentials on workers, and outbound
polling instead of inbound webhooks — with an in-scope/out-of-scope split that
mirrors the items above.

## Roadmap (acknowledged gaps)

The following are known and intended, but not yet shipped:

- **Public CI pipeline** — a zero-trust CI that runs the gate on a fresh
  ephemeral VM per change, with no credentials on the worker.
- **Release engineering** — signed tags, build provenance/attestation, and
  reproducible builds; packaged distribution.
- **Dependency scanning** — automated detection of vulnerable or malicious
  dependencies pulled into the sandbox or the build.
- **Hardware-isolation backend** — an optional microVM isolation primitive for
  threat models that include a hostile kernel.
- **Enforced gVisor resource limits under rootless** — runtime enforcement of
  per-instance CPU/memory caps for gVisor containers (currently render-time-only;
  see Known limitations), gated on upstream gVisor rootless systemd-cgroup support.

Items here are tracked as deferred work and will be moved into scope as the
project matures.
