## Purpose

This specification defines the Git hook containment controls that prevent execution of agent-planted hooks in workspace `.git/hooks/` directories by overriding `core.hooksPath` in both core and admin container gitconfigs.

## Requirements

### Requirement: Core Git Hook Path Override
The `templates/config/core/.gitconfig` template SHALL include `[core] hooksPath = /dev/null` to prevent execution of git hooks planted in `/workspace/.git/hooks/`.

#### Scenario: Core gitconfig contains hooksPath override
- **WHEN** the `templates/config/core/.gitconfig` template source is inspected
- **THEN** it contains a `[core]` section with `hooksPath = /dev/null`

#### Scenario: hooksPath precedes include section
- **WHEN** the `templates/config/core/.gitconfig` template source is inspected
- **THEN** the `[core]` section with `hooksPath` appears before the `[include]` section

### Requirement: Admin Git Hook Path Override
The system SHALL create a new `templates/config/admin/.gitconfig` template with `[core] hooksPath = /dev/null` and an `[include]` section for user overrides via `{{ custom_config_admin }}/.gitconfig`.

#### Scenario: Admin gitconfig file exists
- **WHEN** the `templates/config/admin/` directory is inspected
- **THEN** it contains a `.gitconfig` file

#### Scenario: Admin gitconfig contains hooksPath override
- **WHEN** the `templates/config/admin/.gitconfig` template source is inspected
- **THEN** it contains a `[core]` section with `hooksPath = /dev/null`

#### Scenario: Admin gitconfig includes custom override path
- **WHEN** the `templates/config/admin/.gitconfig` template source is inspected
- **THEN** it contains `[include]` with `path = {{ custom_config_admin }}/.gitconfig`

### Requirement: Admin Gitconfig Compose Mount
The rendered `compose.yml` SHALL mount the admin `.gitconfig` as a read-only bind mount at `/home/human/.gitconfig`.

#### Scenario: Admin gitconfig mounted in compose
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the admin service volumes contain `{{ instance_dir }}/config/admin/.gitconfig:/home/human/.gitconfig:ro`

### Requirement: Admin Gitconfig Hydration Entry
The hydration pipeline SHALL render `templates/config/admin/.gitconfig` as a Jinja2 template into the instance config directory.

#### Scenario: Admin gitconfig in render registry
- **WHEN** `_JINJA_RENDERED_CONFIG` is inspected
- **THEN** it contains an entry for `("admin/.gitconfig", "config/admin/.gitconfig")`
