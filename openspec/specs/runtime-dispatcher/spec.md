# runtime-dispatcher Specification

## Purpose
TBD - created by archiving change runtime-dispatcher. Update Purpose after archive.
## Requirements
### Requirement: Dispatcher Binary Path and Ownership

The system SHALL install the dispatcher binary at the absolute reserved path `/usr/local/libexec/sandbox-ai/dispatch`. The binary SHALL be owned by `root:root` and have file mode `0755`. The binary SHALL be statically linked (no dynamic library dependencies) so that it can be invoked across the privilege boundary into the sandbox user's session without requiring matching shared libraries in that session's filesystem view.

#### Scenario: Installed dispatcher present after `sandbox setup` runs
- **WHEN** an operator has run `sudo sandbox setup` on the host
- **THEN** the file `/usr/local/libexec/sandbox-ai/dispatch` exists, is owned by `root:root`, has mode `0755`, and is statically linked (`file /usr/local/libexec/sandbox-ai/dispatch` reports "statically linked")

#### Scenario: Reserved namespace ownership
- **WHEN** the system creates the parent directory `/usr/local/libexec/sandbox-ai/`
- **THEN** the directory is owned by `root:root`, has mode `0755`, and is documented as a sandbox-ai-managed namespace; the orchestrator's installer never writes outside this directory under `/usr/local/libexec/`

### Requirement: Typed Op Surface

The dispatcher SHALL accept exactly the following **twelve** ops as the first positional argument (`argv[1]`). The op surface is derived from — and byte-faithful to — the existing `machinectl_cmd(...)` callsites it replaces (the "op surface = enumeration of existing behavior" non-goal); each op below cites its originating callsite. (`preflight` is the one op NOT a faithful single-callsite enumeration — it is a read-only *bundle* of the `start` privilege-boundary preflight queries, added by C-009 to collapse the preflight crossing burst; see the "preflight read-only op" requirement. `fwd` is the one **streaming** op — the attach ProxyCommand payload, added by C-010 `attach-fwd-dispatch-op` to close F-060; see the "Streaming Op Class" requirement.)

1. `auth-probe` — no further args. (Source: `doctor/checks/privilege_boundary.py:108`, `cli/main.py:2188`.)
2. `compose-up` — one arg: `<instance-name>`. (Source: `actions/compose.py:42` via `_compose_up_cmd_plan`.)
3. `compose-down` — one arg `<instance-name>`, OPTIONAL second arg the literal `--volumes`. (Source: `cli/main.py:1738`; the source builder takes `volumes: bool = False` — `sandbox stop` omits it, `sandbox destroy` passes it.)
4. `compose-ps` — one arg: `<instance-name>`. (Source: `cli/main.py:311`.)
5. `compose-ls` — no further args. (Source: `doctor/checks/privilege_boundary.py:397`.)
6. `docker-version` — no further args. (Source: `doctor/checks/privilege_boundary.py:155` — `docker version`, a distinct subcommand from `docker info`; it cannot be a `docker-info` preset.)
7. `docker-info` — one arg: `<format-preset>` ∈ {`security-options`, `runtimes`}. (Source: `doctor/checks/privilege_boundary.py:186` → `security-options`; `:219`, `:260`, `:323` → `runtimes`. There is no `default` preset in source.)
8. `docker-manifest-inspect` — one arg: an image reference that MUST be a member of the set `{pin.pinned} ∪ {pin.tagged}` over `IMAGE_REGISTRY` (i.e. either a digest ref `<ref>@sha256:<64-hex>` OR the tag ref `<ref>:<tag>` of some registry entry). (Source: `doctor/checks/supply_chain.py:check_image_digests` — the caller loops `IMAGE_REGISTRY` and runs `docker manifest inspect` BOTH on `pin.pinned` (stale-digest detection) AND on `pin.tagged` (best-effort tag-drift detection); the op itself takes exactly one ref. Q7: an earlier draft modelled only the `.pinned` call and a digest-only regex, which left the `.tagged` call unable to route through the op — see design "Resolved Design Questions" Q7.)
9. `helper-chown-files` — `<parent-path>` `<mode-octal>` `<uid>` `<gid>` `<file-name...>` (one or more file names). (Source: `helper_container.py:108`.)
10. `helper-mkdir-chown-dirs` — `<parent-path>` `<uid>` `<gid>` `<leaf-name...>` (one or more leaf names). (Source: `helper_container.py:146`.)
11. `preflight` — no further args. Read-only. Its target argv bundles the DISTINCT read-only health queries backing `sandbox start`'s privilege-boundary preflight — `echo ok`, `docker version`, `docker info` (security-options), `docker info` (runtimes — one query, deduped), `docker compose ls` — `;`-sequenced, with each query's output individually attributable orchestrator-side. (Source: C-009 — collapses the `start` `run_check_subset(["Privilege Boundary"])` crossing burst; see the "preflight read-only op" requirement for the full contract.)
12. `fwd` — one arg: `<instance-name>`, expanded operator-side to the named-flag wire `<inst> --project <P> --ip <IP>` (see the "fwd Op Wire Expansion" requirement). **Streaming** (frameless) — the separate-user attach ProxyCommand payload: the dispatcher execs `docker exec -i <P>-admin-1 /fwd <IP>:9999` and carries the raw SSH byte stream. (Source: `cli/main.py:_build_attach_argv` — the previously raw `pipe_cmd + docker exec … /fwd …` ProxyCommand, routed through the dispatcher by C-010 so the SUDO-mode per-op sudoers `Cmnd_Spec` can authorize it instance-agnostically — F-060.)

> **Renamed from an earlier draft:** op 8 was `docker-inspect-image <digest...>` mapping to `docker inspect <digest...>`. That was a content error — the only callsite (`supply_chain.py:27`) runs `docker **manifest** inspect <pinned-ref>` (a registry-manifest query, not a local-object inspect) once per `IMAGE_REGISTRY` pin. The op is renamed `docker-manifest-inspect`, takes a single image ref (not bare `sha256:` digests), and `docker-version` was added (op 6) because `privilege_boundary.py:155` runs `docker version`, which no prior op covered. That brought the count to 10; C-009 added `preflight` (op 11) for the burst-collapse, and C-010 added the streaming `fwd` (op 12) for the attach ProxyCommand, so the surface is now **twelve** ops.

Any other op MUST be rejected by the dispatcher with exit code non-zero and a clear error message naming the invalid op.

Every op SHALL additionally accept a single `--check` flag as its lone argument: when present, the dispatcher SHALL validate the op name, log the invocation to journald (with `check=1`), and exit 0 WITHOUT performing the op's side effect. The `--check` flag enables the sister change `sandbox-setup`'s L3a per-op probe (validates each op in `SANDBOX_OPS` resolves to MATCH at the sudoers layer without actually running compose-up / docker / helper-chown / the preflight bundle). This applies uniformly to the streaming op: `dispatch fwd --check` (lone `--check`) rides the **framed** path like every other op — only `fwd`'s real stream invocation (op + wire args) is frameless, per the "Streaming Op Class" requirement — so the L3a/L8 probe protocol stays uniform across all twelve ops.

**Sister-change carry-forward (C-002 `sandbox-setup` L3a — F-004 / F-018 silent-footgun class).** When the sister change's L3a per-op probe (and the L8 fresh-session re-probe, and `core.dispatch.invoke()`/`probe()` at runtime **in separate-user mode**) crosses the operator's privilege-boundary rule to run `dispatch <op> [--check]`, it MUST recover the inner exit via the **dispatcher-emitted begin/exit framing** (`core.executor.Executor(...).run(..., framed=True)`), per the "Dispatcher-Emitted Exit Framing" requirement — NOT via an orchestrator-injected `sentinel=True` wrap, and NOT via the `systemd-run --pipe` native exit (unreliable — F-064). In operator-rootless mode `invoke()`/`probe()` take the local path with native exit recovery (no crossing, no framing), per the "Operator-Rootless Local Invocation Mode" and "Native Exit Recovery for Operator-Rootless invoke/probe" requirements. The wrap injected `{ <cmd>; }; echo __SANDBOX_EXIT_<tok>_$?` INTO the crossed payload, which no per-op `Cmnd_Spec` could match, so it silently broke the probe (and the runtime grant) for every SUDO-mode password-operator while NOPASSWD-blanket operators masked it (F-018). `framed=True` keeps the crossed payload the bare `dispatch <op>` the rule matches and recovers the exit from the dispatcher's nonce-bound trailer. `sudo systemd-run --pipe` fails to faithfully propagate the inner `/bin/bash -c` exit (native exit unreliable, F-064), so without this framing a dispatcher *reject* (op-name validation failure → non-zero exit) would be masked as a sudoers *MATCH* — the probe would report a misconfigured rule as healthy. L3a MUST branch on the **recovered inner exit code**, never on journald presence: journald (the `check=1` structured entry) is the **audit channel**, not a control-flow signal — a journald entry is written for both the short-circuit-success path and is independent of the process exit, so presence/absence of a journal line says nothing about whether the rule resolved to MATCH. (The root setup-phase crossings L5/L6/L7 run as root with no rule to match and so keep the orchestrator-injected `sentinel=True` wrap, now token-validated per the `orchestrator-executor` capability.)

#### Scenario: Known op accepted
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch auth-probe`
- **THEN** the dispatcher does not reject the op for being unknown (proceeds to build the target argv and spawn)

#### Scenario: preflight op accepted
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch preflight`
- **THEN** the dispatcher does not reject the op for being unknown (it is the eleventh valid op)

#### Scenario: fwd op accepted
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch fwd myinst --project dev-myinst --ip 10.100.0.7`
- **THEN** the dispatcher does not reject the op for being unknown (it is the twelfth valid op; the invocation proceeds down the streaming path per the "Streaming Op Class" requirement)

#### Scenario: Unknown op rejected
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch hypothetical-not-a-real-op`
- **THEN** the dispatcher exits non-zero with stderr containing `unknown op: hypothetical-not-a-real-op` and the list of valid ops

#### Scenario: --check flag short-circuits to exit 0 without side effect
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch <any-known-op> --check` (e.g., `dispatch compose-up --check`, `dispatch preflight --check`) — exactly one trailing argument, equal to literal `--check`
- **THEN** the dispatcher exits 0; no target argv is built; no `os.execv` is performed; journald records the invocation with `check=1`; the on-host state is unchanged (the begin/exit framing carrying `_0` is still emitted per the "Dispatcher-Emitted Exit Framing" requirement)

#### Scenario: --check in a non-lone position does NOT short-circuit
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch <known-op> <args...> --check` where the op has additional preceding args (e.g., `dispatch helper-chown-files /srv/parent 0644 1000 1000 --check`)
- **THEN** the dispatcher treats `--check` as a regular positional argument to the op (subject to per-op validation in `core.dispatch`); the short-circuit predicate fires ONLY when `argc == 2` (op + lone `--check`); the real `os.execv` path runs and produces the op's normal side effect
- **(empirically validated in V9c-C1C2.C2.4 against a bash mock dispatcher; the canonical predicate is `argc == 2 && argv[2] == "--check"`)**

#### Scenario: Unknown op + --check is rejected
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch <unknown-op> --check`
- **THEN** op-name validation fires BEFORE the `--check` predicate is consulted; the dispatcher exits non-zero with stderr containing `unknown op: <unknown-op>` and the list of valid ops; `--check` does NOT whitewash an invalid op name
- **(empirically validated in V9c-C1C2.C2.6 against a bash mock dispatcher under a permissive sudoers rule)**

### Requirement: Per-Op Argument Validation

The system SHALL validate every op's arguments in `src/core/dispatch.py` before invoking the dispatcher. Validators SHALL reject malformed args with a clear error and return non-zero exit. Validation rules per op:

- `auth-probe`, `compose-ls`, `docker-version`: no args; reject if any args provided.
- `compose-up`, `compose-ps`: exactly one arg; the instance name MUST match the same regex enforced for `sandbox init` (per `instance-workspace-model`'s instance-name validation).
- `compose-down`: one arg `<instance-name>` (same regex), with an OPTIONAL second arg that MUST be exactly the literal `--volumes` (any other second arg is rejected). No third arg.
- `docker-info`: exactly one arg; MUST be one of the preset literals `security-options` or `runtimes`.
- `docker-manifest-inspect`: exactly one arg; the arg MUST be a member of the precomputed set `{pin.pinned for pin in IMAGE_REGISTRY.values()} ∪ {pin.tagged for pin in IMAGE_REGISTRY.values()}`. Validation is by **set membership**, not by regex/grammar: the op's sole purpose is to inspect `IMAGE_REGISTRY` refs, so its legitimate arg domain is exactly that set (a new registry entry auto-extends it; no docker-reference-grammar surface to get subtly wrong). A bare `sha256:…` digest, an arbitrary `name@sha256:…` not in the registry, or any non-registry ref is rejected.
- `helper-chown-files`: at least five args; `<parent-path>` MUST be an absolute path containing no `..` components, no embedded NUL/newline; `<mode-octal>` MUST be a 4-digit octal between `0000` and `7777`; `<uid>` and `<gid>` MUST be decimal non-negative integers; each file name MUST contain no `/`, `..`, NUL, or newline.
- `helper-mkdir-chown-dirs`: at least four args; `<parent-path>`, `<uid>`, `<gid>` same constraints as above; each leaf name MUST contain no `/`, `..`, NUL, or newline.
- `fwd`: exactly one arg; the instance name MUST match the same regex enforced for `sandbox init` (per `instance-workspace-model`'s instance-name validation). The operator-side wire expansion and the dispatcher-side wire validation are owned by the "fwd Op Wire Expansion" requirement.

Note on `compose-down` vs the `--check` predicate: the `--check` short-circuit fires only when the op has exactly one trailing argument equal to literal `--check` (`argc == 2`). `compose-down <inst> --volumes` is `argc == 3` and is never confused with a check probe; `compose-down --check` (op + lone `--check`) short-circuits as for any other op.

The dispatcher binary itself SHALL NOT re-run these validators; it trusts upstream validation in `core.dispatch`.

#### Scenario: Validator rejects path traversal in helper-chown-files
- **WHEN** `core.dispatch.validate_args(op="helper-chown-files", args=["/srv/parent", "0644", "1000", "1000", "../escape.txt"])` is invoked
- **THEN** the validator raises a typed error naming the rejected argument and the rule it violated; the dispatcher is never invoked

#### Scenario: Validator rejects unknown docker-info preset
- **WHEN** `core.dispatch.validate_args(op="docker-info", args=["all"])` is invoked
- **THEN** the validator raises a typed error indicating only `security-options` and `runtimes` are accepted; the dispatcher is never invoked

#### Scenario: Validator accepts compose-down with optional --volumes
- **WHEN** `core.dispatch.validate_args(op="compose-down", args=["myinst", "--volumes"])` is invoked
- **THEN** the validator returns without raising (instance regex matches; the optional second arg is exactly `--volumes`)

#### Scenario: Validator rejects compose-down with a non---volumes second arg
- **WHEN** `core.dispatch.validate_args(op="compose-down", args=["myinst", "-v"])` is invoked
- **THEN** the validator raises a typed error (the only permitted second arg is the literal `--volumes`); the dispatcher is never invoked

#### Scenario: Validator rejects a ref not in IMAGE_REGISTRY (incl. bare digest)
- **WHEN** `core.dispatch.validate_args(op="docker-manifest-inspect", args=["sha256:" + "a"*64])` is invoked (a bare digest), OR an arbitrary `evil/image@sha256:<64-hex>` not present in `IMAGE_REGISTRY`
- **THEN** the validator raises a typed error (the arg is not a member of the `{pin.pinned} ∪ {pin.tagged}` set); the dispatcher is never invoked

#### Scenario: Validator accepts a registry pinned-digest ref
- **WHEN** `core.dispatch.validate_args(op="docker-manifest-inspect", args=[IMAGE_REGISTRY["busybox_musl"].pinned])` is invoked
- **THEN** the validator returns without raising (the arg is a member of the registry pinned set)

#### Scenario: Validator accepts a registry tag ref (tag-drift probe path)
- **WHEN** `core.dispatch.validate_args(op="docker-manifest-inspect", args=[IMAGE_REGISTRY["busybox_musl"].tagged])` is invoked
- **THEN** the validator returns without raising (the arg is a member of the registry tagged set — this is the `supply_chain.py` tag-drift call, which under Q7 now routes through the op rather than a direct `machinectl_cmd`)

#### Scenario: Validator accepts known good args
- **WHEN** `core.dispatch.validate_args(op="docker-info", args=["runtimes"])` is invoked
- **THEN** the validator returns without raising

#### Scenario: Validator accepts fwd with a valid instance name
- **WHEN** `core.dispatch.validate_args(op="fwd", args=["myinst"])` is invoked
- **THEN** the validator returns without raising (the instance regex matches; wire expansion happens downstream)

#### Scenario: Validator rejects fwd with a malformed instance name or extra args
- **WHEN** `core.dispatch.validate_args(op="fwd", args=["../escape"])` is invoked, OR `validate_args(op="fwd", args=["myinst", "10.100.0.7"])` (a second positional arg)
- **THEN** the validator raises a typed error; the dispatcher is never invoked (callers pass only `[<inst>]` — the `--project`/`--ip` flags are produced by the operator-side wire expansion, never accepted from callers)

### Requirement: Compose Op Wire Expansion

The eight deterministic ops (`auth-probe`, `compose-ls`, `docker-version`, `docker-info`, `docker-manifest-inspect`, `helper-chown-files`, `helper-mkdir-chown-dirs`, `preflight`) cross the boundary with their typed args verbatim (`dispatch <op> <typed-args>`; `preflight` and `auth-probe` take no args). The three compose ops (`compose-up`, `compose-down`, `compose-ps`) MUST NOT, because their target argv embeds project name / compose-file paths / env-file path that are **dev-context state the dispatcher cannot re-derive** (the dispatcher executes inside the `[host].docker_unprivileged_user` session: `compose_project_name` resolves via `getpwuid(getuid())` → the wrong user there; the compose-file and `.sandbox.env` absolute paths live under the operator's `sandbox_ai_home()`).

For the compose ops, `core.dispatch.invoke(...)` SHALL, AFTER per-op validation of the typed args, resolve the dev-context state operator-side (`core.dispatch._resolve_compose_state(inst)` → project name via `core.compose.compose_project_name`, the compose-file list, the `<instance_dir>/.sandbox.env` path) and expand the crossed command line to the **wire form**:

```
/usr/local/libexec/sandbox-ai/dispatch <compose-op> <inst> \
    --project <P> --env-file <E> --compose-file <f1> [--compose-file <f2> …] [--volumes]
```

`--volumes` appears only for `compose-down` when the destroy path requested it. The typed `invoke()` API (and thus the "Typed Op Surface" and "Per-Op Argument Validation" requirements) is unchanged: callers pass `[<inst>]` (plus `["--volumes"]` for a `compose-down` destroy); the named-flag expansion is internal to `invoke()` and occurs only after the typed args validate. The separate-user crossing carrying this wire (and every other op's bare payload) is `sudo_pipe_cmd(user)` (the privileged byte-pipe, C-009 design D2), not `machinectl_cmd(user)`. The wire form itself is identical across crossings.

The dispatcher binary SHALL, for a compose op, parse the named flags (`--project` once, `--env-file` once, `--compose-file` one-or-more, `--volumes` boolean for `compose-down` only), reject any unrecognized flag or a flag illegal for the op, and construct the target argv with an **op-hardcoded verb** that is NEVER taken from the wire. The dispatcher SHALL NOT re-derive `<P>`, the compose-file paths, or `<E>`.

As a bounded defense-in-depth exception to "the dispatcher trusts upstream validation" (design D4), the dispatcher SHALL apply a STRUCTURAL confinement check to compose path operands. This check has two layers that run at **different points in the pipeline**:

- the **lexical envelope** (absolute, no empty/`.`/`..` component, no NUL/newline, the `instances/<inst>/…` containment, and the `--project` charset + `-<inst>` suffix) is a deterministic string-only computation with **no filesystem access**. It is part of **target-argv construction** and is therefore covered by the Python↔Go byte-parity contract (the shared fixture `target_argv_cases.json` / `main_test.go`'s `TestTargetArgvFixtureParity`, design "Target Argv Construction Per Op" C-e). The Go construction function performs zero `lstat`/`os.Stat`/disk I/O so it produces the fixture-asserted argv deterministically inside the offline compile container where the operand paths do not exist on disk.
- the **`lstat` symlink pass** (below) is a **runtime guard on the dispatch path**, NOT part of the byte-parity-tested construction. The dispatcher SHALL run it for compose ops AFTER the target argv is built and BEFORE process replacement (`os.execv`); on failure no `os.execv` is performed. Its behaviour for a real on-disk tree is identical to a single combined check — only the pipeline layer at which the `lstat` runs is distinct (it has its own scenarios below; it is not exercised by the fixture-parity construction test). The orchestrator-side Python builder (`core.dispatch._build_compose_wire_argv`) is correspondingly pure (it performs no confinement at all — the operand set it emits is already trusted dev-context state), so the construction byte-parity holds.

The STRUCTURAL confinement check (both layers) SHALL NOT enumerate legal filenames — that remains the operator-side resolver's responsibility:

- Every `--compose-file` value and the `--env-file` value MUST be an absolute path, MUST contain no empty / `.` / `..` path component, and MUST contain no NUL or newline byte.
- Every `--compose-file` value and the `--env-file` value MUST contain the consecutive path components `instances` then `<inst>` (the argv[2] instance token), with at least one further component below `<inst>` (i.e., the operand provably lives under some `…/instances/<inst>/` tree).
- `--project` MUST match `^[a-z0-9][a-z0-9_-]*$` and MUST end with `-<inst>`.
- For every `--compose-file` and the `--env-file`, the dispatcher SHALL `lstat()` each path component **from the `instances/<inst>` boundary downward to and including the operand file** and MUST reject the invocation if any such component is a symbolic link. An `lstat` error on any such component (e.g. EACCES) SHALL also be a reject (fail-closed); the stderr SHALL name the un-stattable component and indicate the likely cause is a missing sandbox-user ACL traverse grant on its parent directory (actionable: run `sandbox doctor`) — note an EACCES here necessarily implies the real `docker compose -f <path>` run would also fail (lstat needs a subset of the traversal `docker compose` already requires), so this is fail-fast on an already-broken setup, never a false reject of a working one. Components ABOVE the `instances` boundary (operator-home ancestors) SHALL NOT be symlink-checked (avoids namespace-fragile false rejects on legitimate distro/systemd home indirection). This pass performs NO path resolution (namespace-stable, drift-free).

Any failure SHALL cause a non-zero exit with stderr naming the offending operand and no `os.execv`; a lexical-layer failure additionally means no target argv is constructed, while a symlink-layer (runtime-guard) failure means the argv was constructed but the process image is never replaced. This check is an *envelope* (it defeats the direct-invocation `--compose-file /tmp/evil.yml` bypass, and its symlinked-in-tree variant, available to an actor holding the sudoers grant) and is drift-free (adding a component compose file later requires no dispatcher change). It is deliberately NOT TOCTOU-complete: it cannot close the race between this check and `docker compose`'s subsequent open of `-f <path>`. The only actor able to win that race is one with write access to the operator-owned `…/instances/<inst>/` tree — i.e. the operator, who already holds passwordless arbitrary command execution via the F-003-unclosable sudoers grant — so the residual is documented honestly (it grants no power that actor lacks) rather than masked by a path resolution that would false-reject legitimate setups and still not close the race.

#### Scenario: dispatcher rejects a compose-file outside the instance tree
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch compose-up myinst --project u-myinst --env-file /home/op/.sandbox-ai/instances/myinst/.sandbox.env --compose-file /tmp/evil.yml`
- **THEN** the dispatcher exits non-zero with stderr naming `/tmp/evil.yml` as outside the `instances/myinst/` envelope; no `docker compose` argv is constructed and no `os.execv` is performed

#### Scenario: dispatcher rejects a compose path operand containing ..
- **WHEN** a `--compose-file` or `--env-file` value contains a `..` component (e.g. `/home/op/.sandbox-ai/instances/myinst/../other/compose.yml`)
- **THEN** the dispatcher exits non-zero naming the offending operand; no `os.execv` is performed

#### Scenario: dispatcher accepts in-envelope compose path operands
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch compose-up myinst --project u-myinst --env-file /home/op/.sandbox-ai/instances/myinst/.sandbox.env --compose-file /home/op/.sandbox-ai/instances/myinst/docker/compose.yml`
- **THEN** the structural check passes (all operands are absolute, `..`-free, and under `…/instances/myinst/`; `--project` matches the charset and ends with `-myinst`) and the dispatcher proceeds to construct the op-hardcoded `up -d --build --wait` target argv

#### Scenario: dispatcher rejects a --project not ending with -<inst>
- **WHEN** the dispatcher is invoked with `compose-up myinst --project totally-unrelated …`
- **THEN** the dispatcher exits non-zero (the `--project` operand does not end with `-myinst`); no `os.execv` is performed

#### Scenario: dispatcher rejects a symlinked component inside the instance tree
- **WHEN** a `--compose-file` operand is `…/instances/myinst/docker/compose.yml` but `…/instances/myinst/docker` is a symbolic link (resolving outside the tree)
- **THEN** the dispatcher's per-component `lstat` from the `instances/myinst` boundary downward detects the symlinked `docker` component and exits non-zero naming it; no `os.execv` is performed

#### Scenario: dispatcher does not symlink-check operator-home ancestors
- **WHEN** every component from the `instances/<inst>` boundary downward is a real directory/file, but an ancestor ABOVE `instances` (e.g. `/home` → `/var/home` on the host) is a symlink
- **THEN** the structural + symlink checks pass (ancestors above the `instances` boundary are intentionally not symlink-checked) and the dispatcher proceeds to construct the op-hardcoded target argv

#### Scenario: invoke() expands compose-up to the named-flag wire form
- **WHEN** `core.dispatch.invoke("compose-up", ["myinst"], host_config)` is called for a registered instance under separate-user + SUDO auth
- **THEN** the command crossed via `sudo_pipe_cmd(user)` is `[*sudo_pipe_cmd(user), "/bin/bash", "-c", "/usr/local/libexec/sandbox-ai/dispatch compose-up myinst --project <P> --env-file <E> --compose-file <f1> …"]` where `<P>`, `<E>`, and each `<f>` are the operator-side-resolved project name, `.sandbox.env` path, and compose-file paths for `myinst`

#### Scenario: compose-down destroy carries --volumes in the wire form
- **WHEN** `core.dispatch.invoke("compose-down", ["myinst", "--volumes"], host_config)` is called
- **THEN** the expanded wire form ends with `--volumes`, and the dispatcher's op-hardcoded verb for that invocation is `down -v` (not `down`)

#### Scenario: dispatcher rejects an unknown flag on a compose op
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch compose-up myinst --project p --env-file e --compose-file f --runtime evil`
- **THEN** the dispatcher exits non-zero with stderr naming the unrecognized flag; no `docker compose` argv is constructed and no `os.execv` is performed

#### Scenario: dispatcher does not take the compose verb from the wire
- **WHEN** the dispatcher handles any `compose-up` invocation
- **THEN** the constructed target argv's compose verb is exactly the hardcoded `up -d --build --wait`; there is no wire flag by which a caller can substitute a different `docker compose` subcommand for a given compose op

### Requirement: Target Argv Construction Per Op

The dispatcher SHALL translate each op's validated args into a target argv per a fixed mapping defined in `src/core/dispatch.py`. The Go dispatcher binary's argv construction SHALL match the Python builder's output byte-for-byte.

**Verification scoping (honest, per phase-3 review C-e; compose-op refinement per Q6).** The Python `core.dispatch` target-argv builders are the source of truth and are verified in the **standard CI gate** (`make test`/`make coverage`) against the shared fixture `src/templates/dispatch/fixtures/target_argv_cases.json`. The Go binary's matching output is verified against the **same fixture** by `src/templates/dispatch/main_test.go` via `go test ./...` inside the pinned `golang:1.23-alpine` image — there is **no host Go toolchain, so this is NOT part of the standard `make test`/`make coverage` gate**.

The fixture is keyed on each op's **wire form** (what crosses the boundary): for the eight deterministic ops that is the typed args; for the three compose ops that is the Q6 named-flag expansion (`<inst> --project <P> --env-file <E> --compose-file <f>…`); for `fwd` it is its named-flag expansion (`<inst> --project <P> --ip <IP>` — see the "fwd Op Wire Expansion" requirement). Keyed this way, every op's target argv — including compose and `fwd` — is a pure function of its wire inputs, so all twelve ops live in the one shared fixture and the Python↔Go lockstep covers them. The operator-side resolution of `<P>/<E>/<files>` from `<inst>` (`_resolve_compose_state`, and `fwd`'s project/IP resolution) depends on a seeded registered instance, not a static fixture, and is therefore covered by the standard gate's dynamic Python tests rather than by `target_argv_cases.json`.

To make "byte-for-byte" an actually-enforced invariant rather than an unenforced convention, `core.dispatch.compile_dispatcher` SHALL run `go test ./...` inside the pinned image **before** `go build`, in the same `docker run --network none` invocation, and SHALL fail the compile (and therefore `sandbox-setup`'s L6.5 dispatcher-install phase) if the Go fixture test fails. A Python↔Go argv drift is then caught deterministically at dispatcher-compile time — the only place a Go toolchain is available — and a drifted dispatcher binary is never installed. The guarantee is precisely: *Python side — standard-gate-enforced; Go side — compile-time-enforced (compile fails on fixture mismatch); the two stay in lockstep because both consume the one fixture file*. It is a compile-time invariant, not something the orchestrator's pure-Python `make test` gate proves end-to-end (it cannot — no Go toolchain there).

The mappings (with `<...>` denoting substituted args):

Every compose op's inner string carries the exact env prefix the source builders use — `TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME=<proj>` and `--ansi never` — because dropping any of it changes observable behavior (BuildKit progress format, ANSI in captured output, the compose project name). For the compose ops, `<proj>` / `<compose-files>` / `<env>` are the operator-side-resolved operands delivered to the dispatcher via the Q6 named flags (`--project` / `--compose-file`… / `--env-file`); the dispatcher substitutes them into the template below but the compose **verb** (`up -d --build --wait` / `down[ -v]` / `ps --format json`) is op-hardcoded in the dispatcher and never sourced from the wire. The eight deterministic ops below take no such expansion.

- `auth-probe` → `["/bin/bash", "-c", "echo ok"]`
- `compose-up <inst>` → `["/bin/bash", "-c", "TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME=<proj> docker compose <compose-files> --ansi never --env-file <env> up -d --build --wait"]` (byte-faithful to `cli/main.py:_compose_up_cmd_plan`)
- `compose-down <inst> [--volumes]` → `["/bin/bash", "-c", "TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME=<proj> docker compose <compose-files> --ansi never --env-file <env> down<vol>"]` where `<vol>` is ` -v` iff the `--volumes` arg was supplied, else the empty string (byte-faithful to `cli/main.py:1731-1733`, whose `v_flag = " -v" if volumes else ""`). `sandbox stop` → no `--volumes` → `down`; `sandbox destroy` → `--volumes` → `down -v`.
- `compose-ps <inst>` → `["/bin/bash", "-c", "TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME=<proj> docker compose <compose-files> --env-file <env> --ansi never ps --format json"]` (byte-faithful to `cli/main.py:311`)
- `compose-ls` → `["/bin/bash", "-c", "docker compose ls --format json --all"]`
- `docker-version` → `["/bin/bash", "-c", "docker version --format '{{.Server.Version}}'"]` (byte-faithful to `privilege_boundary.py:155`)
- `docker-info <preset>` → `["/bin/bash", "-c", "docker info --format '<fmt>'"]` where `<fmt>` is `{{.SecurityOptions}}` for preset `security-options` (source `:186`) and `{{json .Runtimes}}` for preset `runtimes` (source `:219`/`:260`/`:323`). These are the only two format strings used in source.
- `docker-manifest-inspect <ref>` → `["/bin/bash", "-c", "docker manifest inspect <ref>"]` (byte-faithful to `supply_chain.py:27`; the caller loops over `IMAGE_REGISTRY` and invokes the op once per pinned ref)
- `helper-chown-files <parent> <mode> <uid> <gid> <files...>` → the hardened-`docker run` invocation per `core/helper_container.py` with the chown inner script
- `helper-mkdir-chown-dirs <parent> <uid> <gid> <leaves...>` → the hardened-`docker run` invocation per `core/helper_container.py` with the mkdir+chown inner script
- `preflight` → `["/bin/bash", "-c", "<bundle>"]` where `<bundle>` is the `;`-sequenced read-only health bundle backing `sandbox start`'s privilege-boundary preflight (C-009 D6): per DISTINCT query a `echo __PREFLIGHT_Q_${__PFNONCE}_<name>__; <query> 2>&1; echo __PREFLIGHT_RC_${__PFNONCE}_<name>_$?__` segment (the `${__PFNONCE}` token binds every marker to the per-crossing nonce — see the ADDED "preflight read-only op" requirement's forge-rejection contract) — for the queries `echo ok`, `docker version`, `docker info` security-options, `docker info` runtimes (one query, deduped — feeds the runsc/runsc-runtimeArgs/host-uds checks), and `docker compose ls`. The query inners are SSOT-shared with the individual read-only op builders (so they cannot drift); the segments are `;`-joined (NOT `&&`/`set -e`) so one query's failure neither aborts the others nor forges their success (F-065). A `preflight` fixture row + Go case were added in C-009 so the bundle is covered by the Python↔Go byte-parity contract.
- `fwd <inst> --project <P> --ip <IP>` → `["/usr/bin/docker", "exec", "-i", "<P>-admin-1", "/fwd", "<IP>:9999"]` — the ONE op whose target argv is **NOT** a `/bin/bash -c` wrapper: the dispatcher execs docker directly (stream hygiene — no interpreter between the dispatcher and the byte stream; the framed path's sentinel wrap never applies because the stream invocation bypasses it entirely, per the "Streaming Op Class" requirement). The admin container name is derived dispatcher-side from `--project` (`<P>-admin-1`); the `exec -i` verb, the `/fwd` binary path, and port `9999` are op-hardcoded and never read from the wire. Byte-faithful to the previously raw ProxyCommand payload in `cli/main.py:_build_attach_argv` (`/usr/bin/docker exec -i <project>-admin-1 /fwd <core_ipc_ip>:9999`). A `fwd` fixture row + Go case are added in this change.

#### Scenario: auth-probe constructs canonical echo ok argv
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch auth-probe`
- **THEN** the target argv constructed before process replacement is exactly `["/bin/bash", "-c", "echo ok"]`

#### Scenario: docker-info preset 'runtimes' constructs the runtimes-format argv
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch docker-info runtimes`
- **THEN** the target argv constructed before process replacement is `["/bin/bash", "-c", "docker info --format '{{json .Runtimes}}'"]`

#### Scenario: preflight constructs the sequenced read-only health bundle
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch preflight`
- **THEN** the target argv constructed before process replacement is `["/bin/bash", "-c", "<bundle>"]` where `<bundle>` is the `;`-sequenced per-query (`__PREFLIGHT_Q_${__PFNONCE}_<name>__` / query `2>&1` / `__PREFLIGHT_RC_${__PFNONCE}_<name>_$?__`) form over the deduped read-only queries (`echo ok`, `docker version`, `docker info` security-options, `docker info` runtimes, `docker compose ls`); the `preflight` fixture row in `target_argv_cases.json` captures this nonce-bound marker form (with the literal `${__PFNONCE}` placeholder) for the Python↔Go parity test

#### Scenario: fwd constructs the direct docker-exec argv (no bash -c)
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch fwd myinst --project dev-myinst --ip 10.100.0.7`
- **THEN** the target argv constructed before process replacement is exactly `["/usr/bin/docker", "exec", "-i", "dev-myinst-admin-1", "/fwd", "10.100.0.7:9999"]` (no `/bin/bash`, no `-c`); the `fwd` fixture row in `target_argv_cases.json` captures this form for the Python↔Go parity test

#### Scenario: helper-chown-files target argv is byte-faithful to the existing hardened helper
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch helper-chown-files /srv/cache 0644 1000 1000 a.log b.log`
- **THEN** the target argv is `["/bin/bash", "-c", <cmd>]` where `<cmd>` is byte-identical to what `core.helper_container.hardened_docker_run(IMAGE_REGISTRY["busybox_musl"].pinned, "/srv/cache", <chown-inner>)` produces today — i.e. the dispatcher's builder reuses/mirrors that exact function rather than re-deriving flags. Concretely the hardened prefix is the **space-separated** source form `docker run --rm --runtime=runc --network=none --read-only --tmpfs /tmp --user 0:0 --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true -v /srv/cache:/p <busybox-musl-pinned> sh -c <inner>` (NOT the `--cap-drop=ALL`/`--security-opt=no-new-privileges` `=`-joined forms — those do not match source and would fail a faithful build), and `<inner>` is the existing `cp /p/"$f" /tmp/"$f" && unlink && cp back && chmod {mode} && chown {uid}:{gid}` inode-stability loop from `helper_container.py` (NOT a plain `chmod && chown`). The shared fixture `target_argv_cases.json` MUST capture this exact string so the Python builder and `main_test.go` both assert against the real source form.

### Requirement: Structured Journald Logging

The dispatcher SHALL log every invocation to systemd-journald with structured fields before the process-replacement spawn. Fields:

- `MESSAGE`: human-readable summary in the form `dispatch <op> <args-summary>`
- `SANDBOX_AI_OP`: the op name
- `SANDBOX_AI_ARGS_SUMMARY`: comma-joined args, truncated to 256 chars
- `SANDBOX_AI_TARGET_ARGV_SUMMARY`: space-joined target argv, truncated to 256 chars
- `SANDBOX_AI_INSTANCE`: instance name if the op carries one (compose-up, compose-down, compose-ps, fwd); empty otherwise

Logs SHALL be written with priority `INFO` (priority value 6).

#### Scenario: Successful op invocation produces one journald entry
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch compose-up myinst` and the spawn succeeds
- **THEN** `journalctl --user -u user@<sandbox-uid>` shows one line with `SANDBOX_AI_OP=compose-up`, `SANDBOX_AI_INSTANCE=myinst`, and `PRIORITY=6`

### Requirement: System-Binary Error Translation

When the dispatcher's process-replacement spawn fails with EACCES, EIO, or ENOENT, the dispatcher SHALL exit non-zero with stderr containing both the raw error code AND a hint naming the most likely cause:

- `EACCES` → `"process replacement refused by kernel (EACCES); likely IMA-appraise or fapolicyd is enforcing on <target-binary>. Check system integrity tool state via 'sandbox doctor'."`
- `EIO` → `"I/O error during process replacement (EIO); likely dm-verity reports block-level corruption on <target-binary>'s partition. Check dmesg for verity events."`
- `ENOENT` → `"target binary not found at <target-binary>; the package providing it may be uninstalled. Reinstall the package and re-run 'sudo sandbox setup'."`

#### Scenario: EACCES surfaces integrity-layer hint
- **WHEN** the dispatcher attempts process replacement on `/usr/bin/docker` and the spawn returns EACCES (e.g., fapolicyd refuses)
- **THEN** the dispatcher exits non-zero with stderr containing both `EACCES` and `IMA-appraise or fapolicyd`; journald records the failure with the same hint

#### Scenario: ENOENT surfaces missing-package hint
- **WHEN** the dispatcher attempts process replacement on `/usr/bin/docker` and the spawn returns ENOENT
- **THEN** the dispatcher exits non-zero with stderr containing `target binary not found` and the reinstall hint

### Requirement: Offline Reproducible Compile Recipe

The dispatcher binary SHALL be compiled offline using `docker run --network none` against the pinned image `IMAGE_REGISTRY["golang_alpine"]`. Inside that single container invocation the recipe SHALL first run `go test ./...` (the `main_test.go` fixture parity test — Python↔Go target-argv lockstep, spec "Target Argv Construction Per Op" C-e enforcement) and SHALL proceed to the build only if it passes; the build command SHALL be `go build -trimpath -ldflags '-s -w' -o <output> .` with vendored deps (`GOFLAGS=-mod=vendor`). A fixture-parity failure SHALL fail the recipe and produce no binary. Two *successful* compiles of the same source against the same pinned image SHALL produce byte-identical output (the preceding `go test` does not write to `<output>` and does not affect build reproducibility).

The dispatcher source lives at `src/templates/dispatch/{main.go, go.mod, go.sum, vendor/}` and is shipped in the wheel.

`compile_dispatcher(output_path, host_config)` SHALL take **no build-directory parameter**. The orchestrator SHALL embed the dispatcher source (a deterministic `gzip -9 | base64 -w0` tar of the shipped source entries) in the single `bash -c` payload crossed via `pipe_cmd` (NOT `machinectl_cmd`). The recipe carries a multi-MB **binary frame** out (the built dispatcher base64'd on stdout), and `machinectl_cmd` allocates a PTY where `stdout ≡ stderr` (the `1>&2` chatter-separation would be a no-op — go/docker text would interleave with the binary) and whose `onlcr` line discipline would corrupt the frame, so the byte-pipe-for-binary-frames doctrine mandates `pipe_cmd` (`systemd-run -q --pipe --uid=<user>`: a real byte pipe with distinct stdout/stderr and no PTY). `pipe_cmd` is auth-mode-independent (the per-host `machinectl_authentication` is unused on this path; the parameter is retained for signature symmetry with `invoke`); the PAM-skip trade-off is acceptable for this fixed, audited, session-bounded one-shot build per the boundary-primitive doctrine. Running as the unprivileged docker user, that payload SHALL derive `RD="/run/user/$(id -u)"`, fail-closed-guard it with `[ -d "$RD" ]` (an absent runtime dir SHALL exit non-zero with a diagnostic), and create the build directory as a per-call `mktemp -d "$RD/sandbox-ai-build-XXXXXX"` (a tmpfs directory under the lingering daemon user's per-user runtime dir, created by `systemd-logind` independent of any login session so it is reachable under the PAM-skipping `pipe_cmd` crossing where `$XDG_RUNTIME_DIR` is unset — linger is therefore an architectural prerequisite, sister-change `sandbox-setup` L5; owner-only, whose ancestors are world-traversable so **no operator-tree ACL grant is ever required**; build-dir reachability is structural via the daemon's own runtime dir), and SHALL arm `trap 'rm -rf "$DIR"' EXIT` before any work so the build directory **self-cleans on success AND on every failure path** (no source/binary/ACL residue). All docker/go output SHALL be redirected to a genuinely distinct stderr (`1>&2`, real under the byte pipe); on success the payload SHALL emit only the `base64 -w0` of the built binary on stdout. Because `systemd-run --pipe` propagates the inner `/bin/bash -c` exit, the orchestrator SHALL run the crossing through `Executor().run(cmd)` with the **default `sentinel=False`** (no sentinel echo): the Executor's `check=True` SHALL raise on any non-zero inner exit (absent `/run/user/$(id -u)` caught by the `[ -d "$RD" ]` guard, `go test` fixture drift, `go build` failure, container start failure, timeout). ONLY after a clean (exit 0) return SHALL the orchestrator decode the stripped captured stdout and write `output_path` with mode `0o755`; any failure SHALL raise before that write so `output_path` is untouched on every failure path. Reproducibility SHALL remain location-neutral: the container always bind-mounts the ephemeral directory at the fixed `/build` and `-trimpath` strips module paths, so two compiles into two distinct `mktemp` directories are byte-identical.

#### Scenario: Reproducible build across two invocations
- **WHEN** the compile recipe runs twice with identical source and identical pinned image
- **THEN** the sha512 of the two output binaries matches

#### Scenario: Compile uses pinned golang image
- **WHEN** the compile recipe is invoked
- **THEN** the docker invocation uses the digest-pinned `golang:1.23-alpine@sha256:<digest>` ref from `IMAGE_REGISTRY["golang_alpine"].pinned`, not a tag-mutable reference

#### Scenario: Compile runs offline
- **WHEN** the compile recipe runs with `--network none` against vendored deps
- **THEN** `go build` succeeds; no `go mod download` or network fetch occurs

### Requirement: File Immutability After Install

After installation, the dispatcher binary SHALL carry the immutable file attribute (`chattr +i` set). Setup applies this attribute at install time (sister change `sandbox-setup` L6.5). Setup re-applies the attribute after any `--update-runsc`-style refresh that replaces the binary.

#### Scenario: Installed dispatcher has immutable attribute
- **WHEN** an operator runs `lsattr /usr/local/libexec/sandbox-ai/dispatch`
- **THEN** the output line begins with characters that include `i` (immutable attribute set)

#### Scenario: chattr -i required before replace
- **WHEN** maintenance attempts to overwrite `/usr/local/libexec/sandbox-ai/dispatch` while the immutable bit is set
- **THEN** the write fails with `Operation not permitted`; the operator must run `sudo chattr -i /usr/local/libexec/sandbox-ai/dispatch` first (or use `sandbox setup --refresh` which handles the toggle)

### Requirement: Dispatcher-Emitted Exit Framing

`machinectl shell` does NOT propagate the inner `/bin/bash -c` exit code (it exits 0 even when the payload fails), and the native `systemd-run --pipe` exit is likewise unreliable (F-064), so the inner exit must be recovered out-of-band. The recovery framing SHALL be emitted **by the dispatcher itself**, AFTER sudo has authorized the bare `dispatch <op>` crossing — NOT injected into the crossed payload by the orchestrator. This keeps the crossed (authorized) command the bare `dispatch <op> [args]` that the per-op `Cmnd_Spec` matches; an orchestrator-injected wrap (`{ <cmd>; }; echo __SANDBOX_EXIT_…`) made the authorized command unmatchable and silently broke every op for a SUDO-mode password-operator (F-018).

The dispatcher SHALL:

1. Generate a per-invocation nonce with `crypto/rand` (a hex string matching `[0-9a-f]+`). A nonce-generation failure SHALL emit NO begin line and exit non-zero, so the orchestrator fails closed rather than trusting an unframed exit.
2. Emit `__SANDBOX_BEGIN_<nonce>` on stdout BEFORE running the op (so it precedes all op output).
3. Emit `__SANDBOX_EXIT_<nonce>_<code>` on stdout AFTER the op, on EVERY exit path — the success path (the dispatcher execs `/bin/bash -c '{ <inner>; }; echo __SANDBOX_EXIT_<nonce>_$?'`, so the wrapped bash emits it), the `--check` short-circuit (`_0`), and every early-exit path (usage / unknown-op / validation / symlink-guard reject — emitted by the dispatcher before it returns the code). The success path SHALL preserve process-image replacement via `syscall.Exec` (it execs the sentinel-wrapped bash; the wrap is added post-authorization, as the sandbox user).

The nonce binds the trailer: untrusted op output (a malicious image, `docker-manifest-inspect` registry JSON, compose logs) cannot forge a matching `__SANDBOX_EXIT_` line because it cannot read the dispatcher's prior stdout to learn the nonce, and it cannot precede the dispatcher's begin line. This does NOT defend against a fully-compromised sandbox UID (the dispatcher runs as that UID); that is out of reach of any in-band scheme and is bounded by OS isolation + the immutable root-owned binary.

**Streaming carve-out (C-010).** The framing contract above applies to **framed** invocations only — every op except the streaming op's real invocation. A `fwd <inst> --project … --ip …` stream invocation emits NO nonce, NO begin line, and NO exit trailer (a `__SANDBOX_BEGIN_` line would corrupt the raw SSH byte stream the op exists to carry); its contract is owned by the ADDED "Streaming Op Class" requirement. The lone `dispatch fwd --check` probe form is NOT a stream invocation and emits the full framing like every op, so "every exit path" in item 3 reads: every exit path of a framed invocation. The forgery-resistance property is preserved, not weakened: the orchestrator never parses a stream invocation's output at all (the bytes go to the operator's ssh client, which authenticates core's sshd end-to-end), so there is no verdict for untrusted output to forge — control decisions ride framed ops exclusively, per the "Streaming ProxyCommand Entrypoint" requirement.

#### Scenario: Success path frames the recovered exit
- **WHEN** the dispatcher runs a known op that succeeds
- **THEN** stdout carries `__SANDBOX_BEGIN_<nonce>` before the op's output and `__SANDBOX_EXIT_<nonce>_0` after it, bound to the same nonce

#### Scenario: --check path frames exit 0
- **WHEN** the dispatcher is invoked with a lone `--check`
- **THEN** it emits `__SANDBOX_BEGIN_<nonce>` and `__SANDBOX_EXIT_<nonce>_0` (no op side effect), so the operator-rule probe recovers exit 0 = MATCH

#### Scenario: Early-exit path frames the reject code
- **WHEN** the dispatcher rejects an unknown op (or a validation/symlink-guard failure)
- **THEN** it emits `__SANDBOX_BEGIN_<nonce>` and `__SANDBOX_EXIT_<nonce>_<non-zero>` so the orchestrator recovers the reject code rather than a masked success

### Requirement: Operator-Rootless Local Invocation Mode

When `host_config.host.docker_execution_mode == operator-rootless`, `core.dispatch.build_invocation(op, args, host_config)` SHALL return the op's target-argv **directly** — the same `["/bin/bash", "-c", "<inner>"]` (or hardened `docker run`) produced by the existing per-op target-argv builder — with **no `machinectl_cmd(...)` prefix** and **no `<dispatch-binary> <op>` indirection**. The Go dispatcher binary SHALL NOT be invoked in this mode. The twelve-op surface, per-op argument validators, and per-op target-argv builders SHALL be reused unchanged across both modes. The operator-rootless path SHALL still perform the same upstream steps before the builder as the `separate-user` path — per-op argument validation AND, for the compose ops, the Q6 operator-side wire-expansion (`_expand_compose_wire`, which resolves dev-context project/compose-file/env-file state the pure builder cannot re-derive) — so that **only the crossing prefix is dropped**, not the validation/expansion pipeline. (The streaming op `fwd` is not reachable through `build_invocation`/`invoke()`/`probe()` in either mode; its operator-rootless form — the bare target argv with no dispatcher indirection — is owned by the "Streaming ProxyCommand Entrypoint" requirement and mirrors exactly this requirement's no-indirection local shape.)

In `separate-user` mode `build_invocation` behavior SHALL keep the bare `dispatch <op>` payload, crossed via `sudo_pipe_cmd(user)` (the privileged byte-pipe, C-009 design D2).

#### Scenario: build_invocation emits bare argv in operator-rootless mode

- **WHEN** `build_invocation(Op.COMPOSE_UP, ["inst"], host_config)` is called with `docker_execution_mode == operator-rootless`
- **THEN** the returned argv begins with `/bin/bash`, `-c` (no `sudo`/`machinectl`/`systemd-run` prefix and no `/usr/local/libexec/sandbox-ai/dispatch` token), and the `<inner>` string is byte-identical to the op's target-argv builder output

#### Scenario: separate-user SUDO mode crosses via sudo_pipe_cmd

- **WHEN** `build_invocation(Op.COMPOSE_UP, ["inst"], host_config)` is called with `docker_execution_mode == separate-user`
- **THEN** the returned argv is `[*sudo_pipe_cmd(user), "/bin/bash", "-c", "<dispatch> compose-up …"]` (the bare `dispatch <op>` payload over the privileged byte-pipe — no `machinectl` prefix)

### Requirement: Native Exit Recovery for Operator-Rootless invoke/probe

In `operator-rootless` mode, `core.dispatch.invoke()` SHALL execute the bare argv via `Executor.run(..., framed=False)` and recover the result from the local process's native exit code (no `__SANDBOX_BEGIN/EXIT` nonce framing). `invoke()` SHALL preserve its raise-on-failure contract (raise `SandboxExecutionError` on non-zero exit or timeout). `core.dispatch.probe()` SHALL preserve its branch-on-outcome contract, returning a `ProbeOutcome` with `ok`/`timed_out`/`stdout` derived from the local subprocess result.

Typed argument validation SHALL run before execution in both modes (the local path does not skip validation).

The captured stdout returned to callers SHALL be normalized identically in both modes: `core.dispatch` SHALL pass the operator-rootless local result's stdout through the **same** normalization applied during `separate-user` framing recovery (ANSI-escape stripping, carriage-return removal, collapse of 3+ consecutive newlines). This normalization SHALL be a single shared helper used by both paths (no duplicated logic), and SHALL be applied at the `core.dispatch` layer — NOT in `Executor.run`'s default `framed=False` path (which other callers rely on for raw output).

#### Scenario: Operator-rootless stdout normalized identically to separate-user

- **WHEN** an op's local output in `operator-rootless` mode contains carriage returns, ANSI escape sequences, or 3+ consecutive newlines
- **THEN** the stdout returned by `invoke()`/`probe()` is normalized identically to the `separate-user` framed path (no `\r`, no ANSI, ≤2 consecutive newlines)

#### Scenario: Executor's default framed=False path is not globally altered

- **WHEN** a non-dispatch caller uses `Executor.run(..., framed=False)` (e.g. a setup plain crossing or `compile_dispatcher`)
- **THEN** its raw stdout is returned unchanged — the normalization lives in `core.dispatch`, not in the executor default

#### Scenario: Non-zero local exit raises

- **WHEN** an op run in `operator-rootless` mode exits non-zero locally
- **THEN** `invoke()` raises `SandboxExecutionError` and `probe()` returns `ProbeOutcome(ok=False, …)`

#### Scenario: Local timeout discriminated by probe

- **WHEN** an op run in `operator-rootless` mode exceeds its timeout
- **THEN** `probe()` returns `ProbeOutcome(ok=False, timed_out=True, …)`

#### Scenario: Validation still runs before local execution

- **WHEN** `invoke()` is called in `operator-rootless` mode with malformed args for the op
- **THEN** validation raises before any local subprocess is spawned

### Requirement: Operator-Rootless Op Audit Trail

In `operator-rootless` mode, before executing each op locally, the orchestrator SHALL emit a structured journald record carrying at least the op name, an args summary, and the instance token (when applicable), mirroring the structured audit entry the Go dispatcher emits in `separate-user` mode, so the per-op audit trail is preserved despite bypassing the dispatcher.

#### Scenario: Audit record emitted before local op

- **WHEN** an op is invoked in `operator-rootless` mode
- **THEN** a journald record identifying the op (and instance token when applicable) is emitted before the local subprocess runs

### Requirement: Lifecycle Call Sites Thread Execution Mode

Every orchestrator call site that invokes a dispatcher op for a lifecycle command SHALL pass a `host_config` carrying the resolved `docker_execution_mode`, so that in `operator-rootless` mode ALL such ops run as local subprocesses (no `machinectl` crossing). This covers `start`'s `compose-up` and helper-container ops (`helper-chown-files`, `helper-mkdir-chown-dirs`), `stop`/`destroy`'s `compose-down`, and the status/warm-check `compose-ps`. (The local `setfacl` ACL operations do not cross the dispatcher and are unaffected.)

#### Scenario: stop and destroy run compose-down locally in operator-rootless
- **WHEN** `sandbox stop <inst>` or `sandbox destroy <inst>` runs with `docker_execution_mode == operator-rootless`
- **THEN** the `compose-down` op runs as a local `docker compose down` subprocess with no `machinectl` crossing

#### Scenario: helper-container ops run locally in operator-rootless
- **WHEN** `sandbox start <inst>`'s helper-recipe ops (`helper-chown-files`, `helper-mkdir-chown-dirs`) run with `docker_execution_mode == operator-rootless`
- **THEN** each helper op runs as a local hardened `docker run` subprocess with no `machinectl` crossing

#### Scenario: status and warm-check compose-ps run locally in operator-rootless
- **WHEN** a `sandbox status` query or a lifecycle warm-check runs `compose-ps` with `docker_execution_mode == operator-rootless`
- **THEN** the `compose-ps` op runs as a local `docker compose ps` subprocess with no `machinectl` crossing

### Requirement: `build_invocation` routes SUDO separate-user ops onto the privileged pipe

For separate-user, `core.dispatch.build_invocation` SHALL assemble the crossing as
`[*sudo_pipe_cmd(user), "/bin/bash", "-c", "<dispatch-binary> <op> <wire-args>"]` — the SAME bare
`dispatch <op>` payload it crosses today (so the per-op `Cmnd_Spec` matches), crossed via
`sudo_pipe_cmd(...)`. The validation + compose-wire-expansion pipeline (Q6) and
the operator-rootless branch SHALL be unchanged. No `--unit`/`--description` is added.

#### Scenario: SUDO separate-user prefix swap
- **WHEN** `build_invocation(op, args, host_config)` runs with separate-user
- **THEN** the argv is `["sudo","systemd-run","-q","--pipe","--uid=<user>","/bin/bash","-c","<dispatch> <op> <wire>"]`
- **AND** the inner `<dispatch> <op> <wire>` string is byte-identical to the machinectl-path payload it replaced

#### Scenario: other modes unchanged
- **WHEN** the host is operator-rootless
- **THEN** `build_invocation` returns the local target-argv (no crossing prefix), as today

### Requirement: Frame-based exit recovery on the pipe path (`framed=True`)

On the `sudo_pipe_cmd` path the inner op's exit SHALL be recovered from the dispatcher's
`__SANDBOX_EXIT_<nonce>_<rc>` frame (`framed=True`), exactly as on the `machinectl` path — NOT from
`systemd-run --pipe`'s native exit. F-064 ground-truthed that the native `systemd-run --pipe` exit is
**unreliable** (a failing op returned native `0` while its dispatcher frame correctly showed `_1`); the frame
is authoritative, so a native-exit recovery path would be a correctness bug. This needs **zero
executor/dispatcher change**: the Go dispatcher frames unconditionally (`main.go`), the frame rides the byte
pipe (stdout forwarded faithfully), and the existing `_recover_inner_exit` strips + recovers it. The op's
stdout SHALL be returned free of any `__SANDBOX_BEGIN_`/`__SANDBOX_EXIT_` framing markers (the existing strip).

#### Scenario: non-zero op exit surfaces from the frame
- **WHEN** a dispatcher op exits non-zero across the `sudo_pipe_cmd` path
- **THEN** the orchestrator recovers that non-zero exit from the `__SANDBOX_EXIT_<nonce>_<rc>` frame (raise for
  `invoke`, `ok=False` for `probe`), independent of the possibly-`0` native `systemd-run` exit
- **AND** the returned stdout contains no `__SANDBOX_BEGIN_`/`__SANDBOX_EXIT_` marker text

#### Scenario: probe-style callers still branch correctly
- **WHEN** `dispatch.probe("auth-probe", …)` runs over the pipe path on a working host
- **THEN** it returns `ok=True`; on a failing crossing it returns `ok=False` (never an unhandled raise)

### Requirement: `preflight` read-only op bundles the `start` preflight health queries

`core.dispatch` SHALL define an 11th op, `preflight` (no-arg, read-only), whose target-argv runs the
**distinct** read-only health queries that back `sandbox start`'s privilege-boundary preflight — `echo ok`,
`docker version`, `docker info` (security-options), `docker info` (runtimes — **a single query** feeding the
runsc/runsc-runtimeArgs/host-uds checks), and `docker compose ls` — in **one** crossing, emitting each
query's output in an orchestrator-parseable, individually-attributable form. Each query SHALL run under `;`
sequencing (NOT `&&`/`set -e`) so one query's failure neither aborts the others nor forges their success. The
op SHALL NOT take per-instance state (it is instance-agnostic; `compose-ps` stays a separate crossing). All
check *interpretation* + per-check diagnostics remain orchestrator-side; the op only carries the raw query
outputs. The op crosses via the same path as every other op (`build_invocation`: `sudo_pipe_cmd` under
separate-user, local under operator-rootless) and recovers exit via the frame (`framed=True`).

Each query SHALL be attributed by a begin marker, its **stderr merged** (`2>&1`), and a trailing per-query
**exit marker** (`__PREFLIGHT_RC_<…>_<rc>__`), so the orchestrator reconstructs a per-query outcome
(stdout + recovered exit), not just the whole-op exit. The per-query markers SHALL be **bound to a
per-crossing nonce** (unguessable, dispatcher-minted on the framed path and reused from the F-018 frame;
Python-minted on the operator-rootless local path) so untrusted query output cannot forge a marker and flip a
verdict (the F-018 bar). The orchestrator parser SHALL be **fail-closed**: a missing/garbled segment, an
absent/mismatched nonce, or a duplicated marker SHALL yield a not-ok outcome for that query, never a forged
pass.

#### Scenario: one crossing carries every health query
- **WHEN** `dispatch.probe("preflight", [], host_config)` runs on a separate-user host
- **THEN** it performs exactly one boundary crossing
- **AND** its stdout carries each distinct health query's output (stderr-merged) with a recoverable per-query
  exit code, individually attributable to its check

#### Scenario: forged marker cannot flip a verdict
- **WHEN** an untrusted query's output contains a byte-perfect copy of another query's marker spelled WITHOUT
  the per-crossing nonce
- **THEN** the parser does not match the forged marker (it derives every marker from the crossing's nonce), so
  the real verdict stands and the forgery cannot turn a FAIL into a PASS
- **AND** if the nonce is absent or mismatched, every query is reported not-ok (fail-closed)

#### Scenario: a failing query is isolated, not masking the rest
- **WHEN** one bundled query fails (e.g. the daemon is unreachable)
- **THEN** that query's failure is recoverable from the output while the other queries' outputs are still
  present (the orchestrator can surface the specific failing check)

#### Scenario: deduped runtimes query
- **WHEN** the `preflight` op runs
- **THEN** `docker info` (runtimes) is queried once, and the runsc / runsc-runtimeArgs / host-uds checks are
  all derived from that single output (no repeated crossing)

### Requirement: Streaming Op Class

The dispatcher SHALL classify every op as **framed** (the default — eleven ops) or **streaming** (exactly one op: `fwd`). A **stream invocation** — the streaming op with its wire args (NOT the lone `--check` form) — SHALL:

1. Emit **no** nonce, **no** `__SANDBOX_BEGIN_` line, and **no** `__SANDBOX_EXIT_` trailer: the op's stdout is a raw byte stream (the operator's SSH session) and any dispatcher-emitted stdout byte would corrupt it.
2. Write ALL diagnostics (usage, unknown-op, wire-validation failures) to **stderr** only, exiting non-zero with **zero bytes written to stdout**.
3. On validation success, replace the process image via `syscall.Exec` with the op's target argv **directly** — no `/bin/bash -c` wrapper, no sentinel wrap — so the dispatcher is structurally incapable of interleaving bytes into the stream after handoff.
4. Still write the structured journald entry (per "Structured Journald Logging") before the exec — the stream invocation keeps its audit record even though it has no framing.

The lone `--check` form of a streaming op is NOT a stream invocation: it rides the framed path (nonce + BEGIN + `_0` trailer) exactly like every other op, keeping the L3a/L8 per-op probe protocol uniform across all twelve ops.

There is no exit recovery for a stream invocation by design: no orchestrator code branches on its outcome (the framed warm-state gate runs *before* the stream opens, per `cli-attach`); a failure surfaces to the operator as the ssh client's connection-closed plus the dispatcher's stderr line in the sudo/systemd journal.

#### Scenario: Stream invocation emits no framing
- **WHEN** the dispatcher is invoked as `/usr/local/libexec/sandbox-ai/dispatch fwd myinst --project dev-myinst --ip 10.100.0.7` and validation passes
- **THEN** stdout carries NO `__SANDBOX_BEGIN_` line and NO `__SANDBOX_EXIT_` trailer; the process image is replaced via `syscall.Exec` with `["/usr/bin/docker", "exec", "-i", "dev-myinst-admin-1", "/fwd", "10.100.0.7:9999"]`; from that point stdout/stdin belong exclusively to the exec'd target

#### Scenario: Stream-invocation validation failure is stderr-only
- **WHEN** the dispatcher is invoked as `dispatch fwd myinst --project evil --ip 10.100.0.7` (project fails the `-<inst>` suffix rule)
- **THEN** the dispatcher exits non-zero with a diagnostic on stderr naming the rejected operand and rule; stdout receives zero bytes (no frames, no partial output)

#### Scenario: Streaming op --check rides the framed path
- **WHEN** the dispatcher is invoked as `dispatch fwd --check`
- **THEN** it emits `__SANDBOX_BEGIN_<nonce>` and `__SANDBOX_EXIT_<nonce>_0` (no side effect, journald `check=1`) exactly like every framed op's `--check`, so L3a/L8 probe `fwd` with the same `framed=True` recovery as the other eleven ops

### Requirement: fwd Op Wire Expansion

Callers pass only `[<instance-name>]`. `core.dispatch` SHALL resolve operator-side state the dispatcher cannot re-derive — the compose project name (`compose_project_name(inst)`) and the instance's core IPC IP (read-only IPAM ledger peek; attach never mutates IPAM state, per `cli-attach`) — and SHALL expand the crossed wire to:

`dispatch fwd <inst> --project <P> --ip <IP>`

The Go dispatcher SHALL validate the wire operands before exec:

- `--project` MUST match `^[a-z0-9][a-z0-9_-]*$` AND end with `-<inst>` (the same project rule the compose ops enforce); the admin container name is derived dispatcher-side as `<project>-admin-1` and is never read from the wire.
- `--ip` MUST be a dotted-quad IPv4 literal inside the sandbox-ai IPAM superblock: first octet `10`, second octet in `100..255` (the allocator's `10.100.0.0`–`10.255.255.0` range). Any other value — hostnames, IPv6, out-of-range addresses, ports — is rejected.
- The `docker exec -i` verb, the in-container `/fwd` path, and port `9999` are op-hardcoded; no wire operand can alter them. Unknown or duplicate flags, missing flags, or extra positionals are rejected.

These checks are defense-in-depth on top of the structural confinement already bounding the op: the exec'd dial happens inside the admin container's network namespace, and the SSH layer end-to-end authenticates core (pinned host key, key-only client auth, per `cli-attach`).

#### Scenario: Operator-side expansion produces the named-flag wire
- **WHEN** the streaming entrypoint is asked for the `fwd` crossing argv for instance `myinst` whose project resolves to `dev-myinst` and whose core IPC IP peeks to `10.100.0.7`
- **THEN** the crossed payload is `<dispatch> fwd myinst --project dev-myinst --ip 10.100.0.7` (named flags, no positional smuggling)

#### Scenario: Project without the instance suffix is rejected
- **WHEN** the dispatcher receives `fwd myinst --project other-inst --ip 10.100.0.7`
- **THEN** it rejects the wire (project does not end `-myinst`) with a stderr diagnostic and non-zero exit; no exec occurs

#### Scenario: IP outside the IPAM superblock is rejected
- **WHEN** the dispatcher receives `fwd myinst --project dev-myinst --ip 192.168.1.50` (or `10.0.0.1`, or `evil.example.com`)
- **THEN** it rejects the wire (operand is not an IPv4 literal within `10.100.0.0`–`10.255.255.0`) with a stderr diagnostic and non-zero exit; no exec occurs

#### Scenario: Verb, target binary, and port are not wire-controllable
- **WHEN** the dispatcher receives any well-formed `fwd` wire
- **THEN** the constructed target argv always takes the shape `["/usr/bin/docker", "exec", "-i", "<project>-admin-1", "/fwd", "<ip>:9999"]` — there exists no wire input that changes the `exec -i` verb, the `/fwd` path, the `-admin-1` container suffix, or the `:9999` port

### Requirement: Streaming ProxyCommand Entrypoint

`core.dispatch` SHALL expose a streaming entrypoint, distinct from `invoke()`/`probe()`, that **constructs and returns** the full crossing argv for a streaming op and **never executes it and never returns captured output**. The returned argv is embedded by the caller (the attach/handover builder) as the ssh `-o ProxyCommand=…` value; the *ssh client* executes it. Per mode:

- separate-user → `[*sudo_pipe_cmd(user), "/bin/bash", "-c", "<dispatch> fwd <wire>"]` — the privileged byte-pipe; the crossed payload is the bare `dispatch fwd <wire>` the per-op sudoers `Cmnd_Spec` matches (headless-capable — the F-060 fix).
- operator-rootless → the op's **target argv directly** (`["/usr/bin/docker", "exec", "-i", "<P>-admin-1", "/fwd", "<IP>:9999"]`) — no crossing, no dispatcher indirection, mirroring the "Operator-Rootless Local Invocation Mode" shape.

`invoke()` and `probe()` SHALL reject a streaming op with a typed error before any boundary crossing — the orchestrator can never capture (and therefore never branch on) a stream invocation's output. The invariant this enforces: **the orchestrator branches only on framed, nonce-bound signals; streaming ops carry zero orchestrator-interpreted content.**

A convention meta-test (the `test_machinectl_cmd_callers_restricted` pattern) SHALL enforce structurally that: (a) no `src/` call site passes a streaming op to `invoke()`/`probe()`, and (b) no `src/` module outside `core.dispatch` constructs a `dispatch fwd` payload or its docker-exec target argv directly — the streaming entrypoint is the single sanctioned producer.

#### Scenario: SUDO-mode streaming argv
- **WHEN** the streaming entrypoint is called for `fwd`/`myinst` under separate-user
- **THEN** it returns `["sudo", "systemd-run", "-q", "--pipe", "--uid=<user>", "/bin/bash", "-c", "/usr/local/libexec/sandbox-ai/dispatch fwd myinst --project <P> --ip <IP>"]` without executing anything

#### Scenario: Operator-rootless streaming argv is the bare target
- **WHEN** the streaming entrypoint is called for `fwd`/`myinst` under operator-rootless
- **THEN** it returns `["/usr/bin/docker", "exec", "-i", "<P>-admin-1", "/fwd", "<IP>:9999"]` — no `systemd-run`, no `sudo`, no `<dispatch>` token

#### Scenario: invoke/probe refuse the streaming op
- **WHEN** `core.dispatch.invoke(Op.FWD, ["myinst"], host_config)` or `core.dispatch.probe(Op.FWD, ["myinst"], host_config)` is called
- **THEN** a typed error is raised before any subprocess or boundary crossing; the error names the streaming entrypoint as the sanctioned path

#### Scenario: Meta-test pins the streaming discipline
- **WHEN** the convention meta-test walks `src/**/*.py`
- **THEN** it fails the gate if any call site passes a streaming op to `invoke()`/`probe()`, or any module outside `core.dispatch` builds a `dispatch fwd` payload or its docker-exec target argv

