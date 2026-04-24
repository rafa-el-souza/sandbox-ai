## Purpose

This specification governs the isolated execution boundary inside the Python Orchestrator, guaranteeing that POSIX subprocess logic is strictly bifurcated from core orchestration string factory arrays using deterministic sterile matrices.

## Requirements

### Requirement: The Subprocess Execution Boundary
The system SHALL strictly isolate POSIX level string generation away from execution logic natively. The `run()` method SHALL accept an optional `sentinel` parameter controlling exit code sentinel injection for machinectl-mediated calls.

#### Scenario: Synchronous Deterministic Blocking
- **WHEN** the orchestrator triggers daemon components natively
- **THEN** it strictly implements fully synchronous bounding via localized `subprocess.run()` without triggering volatile `asyncio` parallel event loops natively.

#### Scenario: Subprocess Stream Bifurcation
- **WHEN** allocating native system processes
- **THEN** the system rigidly suppresses stream mapping utilizing `capture_output=True`, except when specifically toggling interactive overrides triggering native descriptor mappings for the `machinectl` PTY bounds natively.

#### Scenario: Sterile Environment Matrix
- **WHEN** the executor evaluates the `env` argument dictionary natively
- **THEN** it deliberately overwrites standard `os.environ` inheritance limits cleanly passing only explicitly vetted minimum POSIX arrays alongside core generated cryptographics mathematically enforcing zero-trust limits safely blinding root shell parameters natively.

#### Scenario: Sentinel parameter scopes exit code recovery
- **WHEN** `Executor.run()` is called with `sentinel=True`
- **THEN** the exit code sentinel protocol is activated for that invocation only, and the return value reflects the inner command's exit code rather than `machinectl`'s always-zero exit code

### Requirement: Exit Code Sentinel Protocol
The system SHALL inject a per-invocation nonce sentinel into non-interactive `machinectl shell` bash payloads to recover the inner command's exit code. The sentinel SHALL use `secrets.token_hex(8)` for per-invocation uniqueness. The system SHALL fail-closed when the sentinel is not found in the output.

#### Scenario: Non-interactive command exit code recovered
- **WHEN** `Executor.run()` is called with `sentinel=True` and `interactive=False`
- **THEN** the inner bash payload is wrapped as `{ <original_cmd>; }; echo __SANDBOX_EXIT_{token}_$?` where `token` is a per-invocation random hex string

#### Scenario: Non-zero exit code raises SandboxExecutionError
- **WHEN** the sentinel line is parsed and the exit code is non-zero
- **THEN** a `SandboxExecutionError` is raised containing the exit code and the sanitized command output

#### Scenario: Missing sentinel fails closed
- **WHEN** the sentinel regex does not match any line in the captured output
- **THEN** a `SandboxExecutionError` is raised indicating the exit sentinel was not found and the command may have crashed

#### Scenario: Last-match semantics for sentinel parsing
- **WHEN** multiple lines in the output match the sentinel pattern (e.g., due to echo in nested scripts)
- **THEN** the last match is used as the authoritative exit code

#### Scenario: Sentinel line stripped from output
- **WHEN** the sentinel is successfully parsed
- **THEN** the sentinel line is removed from `result.stdout` before returning to the caller

#### Scenario: Interactive calls bypass sentinel injection
- **WHEN** `Executor.run()` is called with `interactive=True` (regardless of `sentinel` parameter)
- **THEN** no sentinel is injected and no exit code parsing is performed

#### Scenario: Sentinel disabled by default
- **WHEN** `Executor.run()` is called without explicitly passing `sentinel=True`
- **THEN** no sentinel is injected (backward-compatible with existing non-machinectl call sites)

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
