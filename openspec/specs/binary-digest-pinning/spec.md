# binary-digest-pinning Specification

## Purpose
TBD - created by archiving change sandbox-setup. Update Purpose after archive.
## Requirements
### Requirement: BINARY_REGISTRY Module Location and Schema

The system SHALL define a `BINARY_REGISTRY: dict[str, BinaryPin]` mapping in `src/core/hydration.py`, parallel to the existing `IMAGE_REGISTRY`. Each entry's value SHALL be an immutable Pydantic model (`BinaryPin`) with the following fields:

- `url_template: str` — URL template with `$(arch)` placeholder; resolved at fetch time via `uname -m` substitution
- `version: str` — human-readable version label (gVisor release date string, semver, etc.)
- `sha512: str` — lowercase hex sha512 of the binary content
- `fetch_method: FetchMethod` — Python enum; this change defines exactly one variant, `FetchMethod.GVISOR_TARBALL`; future variants anticipated. The enum literal is `FetchMethod.GVISOR_TARBALL` everywhere it appears (Pydantic field assignment, comparisons, test fixtures); proposal/design prose references the enum value by name.

The v1 `BINARY_REGISTRY` SHALL contain exactly one entry: `"runsc"`. Additional entries SHALL NOT be added in this change; future entries (e.g., `containerd-shim-runsc-v1`) require a separate change.

#### Scenario: BINARY_REGISTRY module location and import
- **WHEN** any module imports `from core.hydration import BINARY_REGISTRY`
- **THEN** the import succeeds; `BINARY_REGISTRY["runsc"]` resolves to a `BinaryPin` instance

#### Scenario: BINARY_REGISTRY has exactly one entry as delivered by this change
- **WHEN** the codebase as delivered by this change enumerates `BINARY_REGISTRY.keys()`
- **THEN** the set is exactly `{"runsc"}` — no additional entries

### Requirement: runsc Entry Contract

The `BINARY_REGISTRY["runsc"]` entry SHALL have:

- `url_template = "https://storage.googleapis.com/gvisor/releases/release/<version>/$(arch)/runsc"` — where `<version>` is substituted from `version` field at fetch time, AND `$(arch)` is substituted from `uname -m` output (e.g., `x86_64`, `aarch64`) at fetch time. (Note: gVisor's URL convention uses `uname -m` directly, NOT the Go-toolchain `GOARCH` translation `amd64`/`arm64`. Per V5 validation.)
- `version` set to a gVisor release date string (e.g., `"20260101"`) — chosen by maintainer at pin-rotation time
- `sha512` set to the lowercase hex sha512 of the binary at the URL — verified by maintainer during pin rotation against gVisor's published sidecar `<url>/runsc.sha512`
- `fetch_method = FetchMethod.GVISOR_TARBALL`

#### Scenario: runsc URL template uses uname -m directly
- **WHEN** `BINARY_REGISTRY["runsc"].url_template` is read
- **THEN** it contains the literal `$(arch)` placeholder (resolved by setup against `uname -m` output: `x86_64` not `amd64`, `aarch64` not `arm64`)

#### Scenario: runsc sha512 matches gVisor's published sidecar at pin time
- **WHEN** a maintainer rotates the runsc pin via `scripts/rotate_pins.py`
- **THEN** the rotation script fetches the URL's sidecar (`<url>/runsc.sha512`), parses the published hash, and embeds it as the new `sha512` value in `BINARY_REGISTRY["runsc"]` (the rotation script's responsibility; documented in `binary-digest-rotation` capability)

### Requirement: Reserved Install Path

Binaries from `BINARY_REGISTRY` SHALL be installed to `/usr/local/libexec/sandbox-ai/<binary-name>`. The path `/usr/local/libexec/sandbox-ai/` is a sandbox-ai-owned namespace; setup SHALL `mkdir -p` it if absent (Ubuntu 24.04 does not pre-create `/usr/local/libexec/`).

Binaries SHALL be installed with mode `0755`, owner `root:root`, and SHALL carry the immutable file attribute (`chattr +i`) after install.

Binaries from `BINARY_REGISTRY` SHALL NOT be installed to `/usr/local/bin/` (operator's PATH; reserved for the operator's own runsc/etc. installations) or any other path outside `/usr/local/libexec/sandbox-ai/`.

#### Scenario: runsc installs to the reserved path
- **WHEN** setup's L6a phase installs runsc
- **THEN** the binary lands at `/usr/local/libexec/sandbox-ai/runsc` (NOT at `/usr/local/bin/runsc`); mode 0755, owner root:root, immutable bit set

#### Scenario: Parent directory created idempotently
- **WHEN** L6a runs on Ubuntu 24.04 (where `/usr/local/libexec/` is absent by default)
- **THEN** setup `mkdir -p`'s `/usr/local/libexec/sandbox-ai/` before installing; subsequent re-runs find the directory present and skip the mkdir step

### Requirement: sha512 Verification Before Install

The system SHALL verify the downloaded binary's sha512 against the `BINARY_REGISTRY[<name>].sha512` value BEFORE writing to the reserved install path. On mismatch, the system SHALL refuse the install, leave the install path untouched, and report the mismatch with both the expected and observed hashes.

The verification SHALL be performed on the downloaded file's full content (not on a partial stream).

#### Scenario: Download succeeds, sha matches, install proceeds
- **WHEN** setup downloads `BINARY_REGISTRY["runsc"]` to a staging path and the computed sha512 equals `BINARY_REGISTRY["runsc"].sha512`
- **THEN** setup proceeds to install the binary at the reserved path

#### Scenario: sha mismatch refuses install
- **WHEN** the downloaded file's sha512 differs from `BINARY_REGISTRY[<name>].sha512`
- **THEN** setup refuses the install with `sha512 mismatch on <name>: expected <expected>, got <actual>. Refusing install. Re-verify upstream source or rotate the pin via scripts/rotate_pins.py.` and leaves any pre-existing binary at the reserved path untouched

### Requirement: Operator-Side Install Module Location

The system SHALL provide `src/core/binary_install.py` as the operator-side packaged module for `BINARY_REGISTRY` consumption. The module SHALL expose:

- `detect_drift(name: str, host_config: HostConfig) -> DriftResult` — probe the installed binary at the reserved path; return one of {`ABSENT`, `MATCH`, `DRIFT (installed_sha, pinned_sha)`}
- `install_pinned(name: str, host_config: HostConfig, *, force: bool = False) -> None` — download from the registry's URL template, verify sha512, install at reserved path with mode 0755 + root:root + `chattr +i`. If `force=False` and the installed binary's sha already matches, SKIP. If `force=True`, `chattr -i` → replace → `chattr +i`.
- `verify_only(name: str, host_config: HostConfig) -> VerifyResult` — read-only probe used by doctor's `runsc_pinned_match` check; returns the same drift info as `detect_drift` without making network calls

The module SHALL be consumed by: setup's L6a phase, the `--update-runsc` flag, and doctor's `runsc_pinned_match` check.

The module SHALL NOT import or call anything from `scripts/`. The maintainer-side rotation script (per `binary-digest-rotation`) shares only the `BINARY_REGISTRY` data with this module; no code reuse.

#### Scenario: Module is packaged in the wheel
- **WHEN** `pip install sandbox-ai` installs the package
- **THEN** `core.binary_install` is importable from the installed package (per `pyproject.toml`'s `packages = ["src/cli", "src/core", "src/templates"]`)

#### Scenario: Module does not depend on scripts/
- **WHEN** static analysis (mypy, ruff) walks `src/core/binary_install.py`
- **THEN** no import statement references `scripts.`; the module is wheel-self-contained

