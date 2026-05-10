## Purpose

This specification defines runtime-aware configuration adjustments that reduce syscall amplification under gVisor's user-space sentry. When containers run with `runtime: runsc`, polling-heavy configurations that are imperceptible on bare metal can consume significant host CPU due to gVisor's 5–20× syscall translation overhead.

**Status**: this capability is slated for deletion at archive time as part of `admin-reframe`. All requirements have been removed.

## Requirements

### Requirement: Capability Slated for Deletion
This capability SHALL be deleted at archive time as part of `admin-reframe`. All substantive requirements have been removed; this stub exists solely to satisfy `openspec validate --strict` until the directory is deleted by `openspec-archive-change`.

#### Scenario: Capability marked for archival deletion
- **WHEN** the `admin-reframe` change is archived
- **THEN** the `openspec/specs/gvisor-resource-tuning/` directory is removed in full
