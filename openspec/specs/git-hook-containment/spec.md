## Purpose

This specification defines the Git hook containment controls that prevent execution of agent-planted hooks in workspace `.git/hooks/` directories by overriding `core.hooksPath` in the core container's gitconfig.

## Requirements

### Requirement: Core Git Hook Path Override
The `templates/config/core/.gitconfig` template SHALL include `[core] hooksPath = /dev/null` to prevent execution of git hooks planted in any workspace `.git/hooks/` directory. With multi-workspace mounts at `/workspaces/<ws>` (one per workspace per `cli-workspace`), each workspace's `.git/hooks/` is a potential planting target; the global `hooksPath` override applies uniformly to all of them via the agent's gitconfig.

#### Scenario: Core gitconfig contains hooksPath override
- **WHEN** the `templates/config/core/.gitconfig` template source is inspected
- **THEN** it contains a `[core]` section with `hooksPath = /dev/null`

#### Scenario: hooksPath precedes include section
- **WHEN** the `templates/config/core/.gitconfig` template source is inspected
- **THEN** the `[core]` section with `hooksPath` appears before the `[include]` section

#### Scenario: hooksPath override applies to all workspaces
- **WHEN** the agent runs git operations inside any of `/workspaces/<ws>` (multi-workspace setup with multiple bind-mounted repos)
- **THEN** the `hooksPath = /dev/null` setting in `~/.gitconfig` applies to every workspace's `.git/hooks/` uniformly; no per-workspace gitconfig override is required

