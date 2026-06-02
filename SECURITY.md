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

The threat model is summarized in the [README](README.md#threat-model). In
short, `sandbox-ai` assumes a single-tenant Linux host with a trusted operator
and an **untrusted agent**, and it defends against the agent:

- reading or exfiltrating the operator's credentials, keys, cloud config, or
  unrelated project files;
- escalating to host root through the orchestrator (the orchestrator never runs
  Docker as the operator — the only path to the daemon is the unprivileged
  boundary user);
- making uncontrolled network egress (each instance has an explicit egress
  policy);
- contaminating other projects (per-instance isolation of subnets, state, and
  workspaces).

## Known limitations (out of scope)

These are **deliberate, documented gaps**, not oversights. Each is a defensible
boundary of the current design, with a "revisit when" condition tracked in the
project's roadmap.

- **Host kernel / container-runtime 0-day.** Isolation here is shared-kernel
  containers, not a hypervisor. A container-escape via a kernel or runtime
  vulnerability is out of scope. If you need hardware-level isolation, use a
  microVM (Firecracker/Kata) or a dedicated host. *Revisit when:* a microVM
  backend is added.
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
- **Cost / abuse amplification.** Resource exhaustion or abuse beyond the
  configured per-instance limits (and, for the project's own CI, mass-spawn
  amplification) is bounded by configuration, not eliminated.
- **Telemetry side-channels.** The project avoids third-party coverage/telemetry
  services to keep the trust surface small; this is a posture choice, not a
  defended boundary.

For the CI pipeline that runs against this repository, the in-scope vs.
out-of-scope split is owned by the CI design and mirrors the items above
(per-job ephemeral VMs, no credentials on workers, polling instead of inbound
webhooks).

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

Items here are tracked as deferred work and will be moved into scope as the
project matures.
