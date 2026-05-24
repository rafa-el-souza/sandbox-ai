# binary-digest-rotation Specification

## Purpose
TBD - created by archiving change sandbox-setup. Update Purpose after archive.
## Requirements
### Requirement: Rotation Script Location and Renaming

The maintainer-only rotation script SHALL be located at `scripts/rotate_pins.py`. The pre-existing `scripts/rotate_digests.py` SHALL be renamed to `scripts/rotate_pins.py` as part of this change. No file at the old `scripts/rotate_digests.py` path SHALL remain in the codebase after the rename.

The script SHALL NOT be packaged into the wheel (`pyproject.toml`'s `[tool.hatch.build.targets.wheel] packages` field does NOT include `scripts/`). It is maintainer-only: invoked by developers rotating pins, not by operators running sandbox-ai.

#### Scenario: rotate_digests.py renamed
- **WHEN** the codebase is searched for `scripts/rotate_digests.py`
- **THEN** the file does not exist; `scripts/rotate_pins.py` exists in its place

#### Scenario: Rotation script not packaged in wheel
- **WHEN** a built wheel of sandbox-ai is unpacked and its contents enumerated
- **THEN** no file path containing `scripts/rotate_pins.py` (or any `scripts/` file) appears in the wheel

### Requirement: Polymorphic Drift Detection Across Registry Kinds

The system SHALL handle two registry kinds in a single polymorphic rotation tool:

1. **`IMAGE_REGISTRY` (existing)** — docker images. Drift detection via `docker manifest inspect <ref>:<tag>` returning the top-level manifest-list digest. Signature verification via cosign-keyless (chainguard images) or Docker Content Trust (DCT, official images) per the existing `SIGNER_REGISTRY` mapping.

2. **`BINARY_REGISTRY` (new)** — HTTP-tarball binaries. Drift detection via fetching the `<url>/runsc.sha512` sidecar (for `fetch_method=GVISOR_TARBALL`) and comparing against `BINARY_REGISTRY[<name>].sha512`. Signature verification via sha512-sidecar match (gVisor publishes shas alongside binaries).

The rotation script SHALL dispatch on the entry's kind (`IMAGE_REGISTRY` entry vs. `BINARY_REGISTRY` entry) and apply the appropriate drift-detection + signature-verification path. The two paths SHALL NOT share verification code; they share only the rotation script's shell (CLI parsing, dirty-tree guard, patch + commit workflow).

#### Scenario: Rotation script detects drift in IMAGE_REGISTRY entries
- **WHEN** `scripts/rotate_pins.py --dry-run` runs and a `IMAGE_REGISTRY` entry's `docker manifest inspect <ref>:<tag>` returns a digest different from the pinned `digest`
- **THEN** the script reports the drift entry as `<key>: <old_digest> → <new_digest>` and the verification method (`cosign-keyless` or `docker-content-trust` or `none`)

#### Scenario: Rotation script detects drift in BINARY_REGISTRY entries
- **WHEN** `scripts/rotate_pins.py --dry-run` runs and a `BINARY_REGISTRY` entry's published `<url>/runsc.sha512` returns a sha different from the pinned `sha512`
- **THEN** the script reports the drift entry as `<key>: <old_sha512> → <new_sha512>` and the verification method (`sha512-sidecar` for `fetch_method=GVISOR_TARBALL`)

#### Scenario: Rotation script handles both registries in one run
- **WHEN** drift exists in both `IMAGE_REGISTRY` and `BINARY_REGISTRY` entries
- **THEN** the script reports drift for entries of both kinds in a single output; `--auto-commit` mode rotates both kinds in a single commit (if signature verification passes for all drifted entries)

### Requirement: Dirty-Tree Guard

The rotation script SHALL refuse to patch `core/hydration.py` (or write any other file) when the git working tree contains uncommitted changes. Detection via `git status --porcelain` returning non-empty output. The guard SHALL fire AFTER drift detection (so dry-run mode can report drift even on a dirty tree) but BEFORE any file mutation in non-dry-run mode.

#### Scenario: Dirty tree refuses patch
- **WHEN** `scripts/rotate_pins.py` (non-dry-run mode) detects drift and `git status --porcelain` returns non-empty output
- **THEN** the script exits non-zero with `Error: working tree has uncommitted changes. Commit or stash changes before rotating pins.` and makes no mutations

### Requirement: Dry-Run vs. Auto-Commit Modes

The rotation script SHALL accept mutually exclusive `--dry-run` and `--auto-commit` flags. Without either flag, the default behavior is: detect drift, verify signatures (if `--auto-commit` would have run), patch `core/hydration.py` with new pin values, do NOT commit (leave the diff for manual commit).

- `--dry-run`: detect drift; report; make no mutations; exit 0 with summary
- `--auto-commit`: detect drift; verify signatures (refuse on any verification failure); apply dirty-tree guard; patch; git add + git commit with conventional-commit message
- default (neither flag): detect drift; verify signatures (warn-only); apply dirty-tree guard; patch; leave for manual commit

#### Scenario: --dry-run reports drift without mutation
- **WHEN** `scripts/rotate_pins.py --dry-run` runs with detected drift
- **THEN** the script reports `<N> pin(s) would be rotated`, lists each entry's old→new value, makes no file changes, makes no git commits, exits 0

#### Scenario: --auto-commit verifies signatures before committing
- **WHEN** `scripts/rotate_pins.py --auto-commit` runs with detected drift and ANY entry's signature verification fails
- **THEN** the script exits non-zero with `Error: signature verification failed for <key>. Refusing to auto-commit.`; no file mutations; no commits

### Requirement: Commit Message Convention

In `--auto-commit` mode, the commit message SHALL follow the conventional-commits style with subject:

```
chore(deps): rotate pins (<comma-separated-keys>)
```

Where `<comma-separated-keys>` enumerates the rotated entries (e.g., `wolfi_base, busybox_musl, runsc`). The body SHALL be empty (or optionally include the old→new digests for each entry; maintainer's choice via a `--commit-body` flag, out of scope for this change).

#### Scenario: Auto-commit produces conventional-commit message
- **WHEN** `scripts/rotate_pins.py --auto-commit` rotates `wolfi_base` and `runsc`
- **THEN** the resulting commit's message subject is `chore(deps): rotate pins (wolfi_base, runsc)`

### Requirement: Python 3 except-tuple Syntax Fix

The rename from `scripts/rotate_digests.py` to `scripts/rotate_pins.py` SHALL include a one-line fix at the pre-existing line 133 (in `_verify_signature`'s `cosign-keyless` branch): replace `except subprocess.TimeoutExpired, FileNotFoundError:` (Python-2 syntax) with `except (subprocess.TimeoutExpired, FileNotFoundError):` (Python-3 syntax with parenthesized tuple).

The fix SHALL be accompanied by a regression test that imports the module and asserts no SyntaxError. The test SHALL live in `tests/unit/scripts/test_rotate_pins.py` (a new test module; `scripts/` is not tested today, so this introduces the first script-test module).

#### Scenario: Module imports under Python 3.14
- **WHEN** `import importlib; importlib.import_module("scripts.rotate_pins")` runs under the project's pinned Python 3.14
- **THEN** the import succeeds (no SyntaxError); the regression test passes

#### Scenario: Old Python-2 syntax is removed
- **WHEN** the codebase is searched for the literal `except subprocess.TimeoutExpired, FileNotFoundError:`
- **THEN** zero matches; the corrected form `except (subprocess.TimeoutExpired, FileNotFoundError):` exists in `scripts/rotate_pins.py`

