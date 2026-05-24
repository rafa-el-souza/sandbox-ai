## Purpose

This specification governs the isolated execution boundary inside the Python Orchestrator, guaranteeing that POSIX subprocess logic is strictly bifurcated from core orchestration string factory arrays using deterministic sterile matrices.
## Requirements
### Requirement: The Subprocess Execution Boundary

The system SHALL strictly isolate POSIX level string generation away from execution logic natively. The `run()` method SHALL accept two mutually-exclusive keyword controls for inner-exit recovery over `machinectl shell` (which does not propagate the inner `/bin/bash -c` exit code):

- `sentinel` (the **wrap path**) — the Executor INJECTS its own exit sentinel into the bash payload. Used by crossings the Executor fully controls and that are NOT matched by a per-op privilege-boundary rule (the root setup-phase crossings).
- `framed` (the **framed path**) — the Executor injects NOTHING; the callee (the root-owned dispatcher) emits its own begin/exit framing and the Executor parses it. Used by the operator dispatch crossing and the operator-rule probes (L3a, L8), where the crossed payload MUST stay the bare command so the per-op `Cmnd_Spec` matches it.

`sentinel` and `framed` SHALL be rejected with `ValueError` if both are passed.

#### Scenario: Sentinel parameter scopes exit code recovery
- **WHEN** `Executor.run()` is called with `sentinel=True`
- **THEN** the exit code sentinel protocol is activated for that invocation only (the Executor injects the wrap), and the return value reflects the inner command's exit code rather than `machinectl`'s always-zero exit code

#### Scenario: Framed parameter recovers a callee-emitted exit without injecting
- **WHEN** `Executor.run()` is called with `framed=True`
- **THEN** the Executor does NOT modify the command (the bare payload crosses the boundary), `check` is disabled, and the inner exit is recovered from the callee's begin/exit framing in the captured output

#### Scenario: sentinel and framed are mutually exclusive
- **WHEN** `Executor.run()` is called with both `sentinel=True` and `framed=True`
- **THEN** a `ValueError` is raised before any subprocess is spawned

### Requirement: Exit Code Sentinel Protocol

The system SHALL recover the inner command's exit code from non-interactive `machinectl shell` crossings and SHALL bind the recovered exit to a value untrusted command output cannot forge. There are two mechanisms:

1. **Wrap path (`sentinel=True`).** The Executor SHALL inject a per-invocation nonce sentinel into the bash payload, wrapping it as `( <original_cmd> ); echo __SANDBOX_EXIT_{token}_$?` — a SUBSHELL `( … )`, NOT a brace group `{ … }`, so an inner `exit` in `<original_cmd>` cannot swallow the trailing sentinel echo (the F-023 root cause) — where `token` is `secrets.token_hex(8)`. The recovered exit SHALL be taken from the `__SANDBOX_EXIT_` line whose token EQUALS the injected token — a line carrying any other token SHALL be ignored (so injected/forged output cannot spoof the exit). When the command is not a `bash -c` form (no injection point), no wrap is applied and no recovery is performed.

2. **Framed path (`framed=True`).** The Executor SHALL inject nothing; the dispatcher emits `__SANDBOX_BEGIN_<nonce>` BEFORE the op and `__SANDBOX_EXIT_<nonce>_<code>` AFTER it (per the `runtime-dispatcher` capability). The Executor SHALL read the nonce from the FIRST `__SANDBOX_BEGIN_` line (the dispatcher's — emitted before any op output) and SHALL accept only an `__SANDBOX_EXIT_` line carrying that exact nonce. Untrusted op output cannot forge the trailer because it cannot read the dispatcher's prior stdout to learn the nonce.

Both mechanisms SHALL strip the framing lines from `result.stdout`, SHALL raise `SandboxExecutionError` on a non-zero recovered exit, and SHALL fail closed (raise `SandboxExecutionError` indicating the exit sentinel was not found, surfacing stderr) when the expected framing is absent. Interactive calls (`interactive=True`) SHALL bypass both mechanisms. A call passing neither `sentinel` nor `framed` SHALL inject nothing and perform no recovery (backward-compatible with non-`machinectl` call sites).

This forge-binding closes the F-018 residual: an `__SANDBOX_EXIT_` line is authoritative only when its token/nonce matches the expected one, so a malicious image / `docker-manifest-inspect` registry JSON / compose log on the captured stream cannot spoof a success exit. It does NOT defend against a fully-compromised sandbox UID (the dispatcher runs as that UID) — that is out of reach of any in-band scheme and is bounded by OS isolation + the immutable root-owned dispatcher binary.

#### Scenario: Wrap-path exit code recovered and bound to the injected token
- **WHEN** `Executor.run()` is called with `sentinel=True` and `interactive=False`
- **THEN** the inner bash payload is wrapped as `( <original_cmd> ); echo __SANDBOX_EXIT_{token}_$?` (a subshell, so an inner `exit` cannot swallow the sentinel — F-023) and only an `__SANDBOX_EXIT_` line carrying that exact `token` is used to recover the exit

#### Scenario: Framed-path exit recovered from the dispatcher's begin-bound nonce
- **WHEN** `Executor.run()` is called with `framed=True` and the captured output contains `__SANDBOX_BEGIN_<nonce>` followed by `__SANDBOX_EXIT_<nonce>_<code>`
- **THEN** the recovered exit is `<code>`, both framing lines are stripped from `result.stdout`, and a non-zero `<code>` raises `SandboxExecutionError`

#### Scenario: Forged exit line with a non-matching token/nonce is ignored
- **WHEN** the captured output contains an `__SANDBOX_EXIT_` line whose token/nonce does NOT match the expected (injected token, or first begin nonce) and no matching line is present
- **THEN** recovery fails closed with `SandboxExecutionError` (the forged exit is NOT trusted) rather than returning a fabricated success

#### Scenario: Non-zero recovered exit raises SandboxExecutionError
- **WHEN** the authoritative (matching token/nonce) sentinel line is parsed and the exit code is non-zero
- **THEN** a `SandboxExecutionError` is raised containing the exit code and the sanitized command output

#### Scenario: Missing framing fails closed
- **WHEN** neither the expected begin line (framed path) nor any matching exit line is present in the captured output
- **THEN** a `SandboxExecutionError` is raised indicating the exit sentinel was not found and the command may have crashed, surfacing any stderr (e.g. a sudo `password is required` refusal)

#### Scenario: Interactive calls bypass both mechanisms
- **WHEN** `Executor.run()` is called with `interactive=True` (regardless of `sentinel`/`framed`)
- **THEN** nothing is injected and no exit code parsing is performed

### Requirement: PTY Output Sanitization
The system SHALL sanitize captured output from non-interactive `machinectl shell` calls by stripping PTY artifacts. Sanitization SHALL NOT apply to interactive calls.

#### Scenario: ANSI escape sequences stripped
- **WHEN** captured non-interactive output contains ANSI escape codes (e.g., `\033[37m`, `\033[31mERROR\033[0m`)
- **THEN** the escape sequences are removed and only the plain text content remains

#### Scenario: Carriage returns from PTY ONLCR removed
- **WHEN** captured output contains `\r` characters from PTY line discipline
- **THEN** all `\r` characters are removed

#### Scenario: Excessive blank lines collapsed
- **WHEN** captured output contains three or more consecutive newlines
- **THEN** the run is collapsed to exactly two newlines

#### Scenario: Interactive output not sanitized
- **WHEN** `Executor.run()` is called with `interactive=True`
- **THEN** output passes through to the operator's terminal unmodified with full terminal capabilities preserved

### Requirement: Source Suppression for Non-Interactive Compose Calls
The system SHALL prepend environment variables to non-interactive compose command payloads that suppress terminal-aware output at the source.

#### Scenario: Source suppression environment applied
- **WHEN** a non-interactive compose command is constructed for machinectl execution
- **THEN** the command payload is prefixed with `TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain` and includes `--ansi never` in the compose arguments

