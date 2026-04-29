## Purpose

This specification defines the automated image digest rotation tool that resolves current tag digests, verifies signatures, patches `core/hydration.py`, and optionally commits the result. This ensures the `IMAGE_REGISTRY` remains current with upstream releases while maintaining cryptographic verification of the supply chain.

## Requirements

### Requirement: Automated Digest Rotation Script
The system SHALL provide `scripts/rotate_digests.py` as a developer-facing CLI tool that resolves current tag digests, verifies signatures, patches `core/hydration.py`, and optionally commits the result. The script SHALL NOT be an operator-facing CLI command.

#### Scenario: No drift detected
- **WHEN** `scripts/rotate_digests.py` runs and all pinned digests match their current tag resolution
- **THEN** the script exits 0 with a message indicating no rotation needed

#### Scenario: Drift detected and patched
- **WHEN** `scripts/rotate_digests.py` runs and one or more pinned digests differ from their current tag resolution
- **THEN** the script resolves the new manifest-list digests, patches the `ImagePin` entries in `hydration.py`, and runs `pytest tests/unit/test_hydration.py` to verify

#### Scenario: Dry-run mode
- **WHEN** `scripts/rotate_digests.py --dry-run` runs
- **THEN** it reports which digests have drifted and what the new values would be, without modifying any files

#### Scenario: Auto-commit mode
- **WHEN** `scripts/rotate_digests.py --auto-commit` runs and drift is detected
- **THEN** the script patches, verifies tests pass, and creates a structured git commit with the rotation details

#### Scenario: Auto-commit refuses on verification failure
- **WHEN** `scripts/rotate_digests.py --auto-commit` runs and cosign/DCT verification fails for any image
- **THEN** the script exits non-zero without patching or committing

### Requirement: Dirty Tree Guard
The script SHALL refuse to patch `hydration.py` when the git working tree has uncommitted changes, preventing interleaving of rotation patches with unrelated edits. The guard SHALL be checked after drift detection, not at startup.

#### Scenario: Clean tree allows patch
- **WHEN** `git status --porcelain` returns empty output and drift is detected
- **THEN** the script proceeds with patching

#### Scenario: Dirty tree blocks patch
- **WHEN** `git status --porcelain` returns non-empty output and drift is detected
- **THEN** the script exits non-zero with a message to commit or stash changes first

#### Scenario: No drift bypasses guard
- **WHEN** no digest drift is detected and the working tree is dirty
- **THEN** the script exits 0 without checking tree state (guard is not reached)

### Requirement: Signature Verification Registry
The script SHALL declare a `SIGNER_REGISTRY` mapping image refs to their signature verification method (`cosign-keyless`, `docker-content-trust`, or `none`). Verification SHALL be mandatory in `--auto-commit` mode and advisory in manual/dry-run modes.

#### Scenario: Cosign keyless verification succeeds
- **WHEN** an image in `SIGNER_REGISTRY` is marked `cosign-keyless` and `cosign verify` succeeds
- **THEN** the script proceeds with the rotation for that image

#### Scenario: Verification failure in auto-commit mode
- **WHEN** signature verification fails for any image and `--auto-commit` is active
- **THEN** the script exits non-zero without committing

#### Scenario: Verification failure in manual mode
- **WHEN** signature verification fails for any image in manual (non-auto-commit, non-dry-run) mode
- **THEN** the script emits a warning and prompts the developer to proceed or abort

### Requirement: Manifest-List Digest Resolution
The script SHALL extract the top-level manifest-list digest (not per-platform manifest digest) from `docker manifest inspect` output. This ensures cross-platform portability and matches `FROM image@sha256:...` resolution behavior.

#### Scenario: Multi-arch image resolves to manifest-list digest
- **WHEN** `docker manifest inspect <ref>:<tag>` returns a manifest list
- **THEN** the script extracts the top-level `RepoDigests` digest, not a platform-specific sub-manifest digest

#### Scenario: Single-arch image resolves to image digest
- **WHEN** `docker manifest inspect <ref>:<tag>` returns a single manifest (no manifest list)
- **THEN** the script extracts the image digest directly
