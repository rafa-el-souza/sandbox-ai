## Purpose

This specification defines the admin container's shell environment configuration — tmux plugin path resolution, locale settings, and terminal type defaults for correct rendering under non-TTY container startup.

## Requirements

### Requirement: tmux Plugin Path Resolution
The admin container's `.tmux.conf` template SHALL set `TMUX_PLUGIN_MANAGER_PATH` to `/usr/local/tmux-plugins` and all `run` directives SHALL reference plugin paths under `/usr/local/tmux-plugins/`, matching the Dockerfile install location.

#### Scenario: TMUX_PLUGIN_MANAGER_PATH set
- **WHEN** the source `.config/admin/.tmux.conf` template is inspected
- **THEN** it contains `set-environment -g TMUX_PLUGIN_MANAGER_PATH '/usr/local/tmux-plugins'`

#### Scenario: Catppuccin plugin path correct
- **WHEN** the source `.config/admin/.tmux.conf` template is inspected
- **THEN** the catppuccin `run` directive references `/usr/local/tmux-plugins/catppuccin/tmux/catppuccin.tmux` (not `~/.config/tmux/plugins/catppuccin/tmux/catppuccin.tmux`)

#### Scenario: TPM plugin path correct
- **WHEN** the source `.config/admin/.tmux.conf` template is inspected
- **THEN** the TPM `run` directive references `/usr/local/tmux-plugins/tpm/tpm` (not `~/.config/tmux/plugins/tpm/tpm`)

#### Scenario: No stale ~/.config/tmux/plugins paths
- **WHEN** the source `.config/admin/.tmux.conf` template is inspected
- **THEN** it does NOT contain any path matching `~/.config/tmux/plugins/`

### Requirement: C.UTF-8 Locale Configuration
The admin container's `.zshrc` template SHALL set `LANG` and `LC_ALL` to `C.UTF-8`. The `en_US.UTF-8` locale is not installed in the container image; glibc silently falls back to `POSIX`, causing `wcwidth()` to miscompute display widths of multi-byte UTF-8 characters (starship prompt misalignment).

#### Scenario: LANG is C.UTF-8
- **WHEN** the source `.config/admin/.zshrc` template is inspected
- **THEN** it contains `export LANG=C.UTF-8`

#### Scenario: LC_ALL is C.UTF-8
- **WHEN** the source `.config/admin/.zshrc` template is inspected
- **THEN** it contains `export LC_ALL=C.UTF-8`

#### Scenario: en_US.UTF-8 is not used
- **WHEN** the source `.config/admin/.zshrc` template is inspected
- **THEN** it does NOT contain `en_US.UTF-8` in any `LANG` or `LC_ALL` export

### Requirement: TERM Environment Variable Default
The admin container's `entrypoint.sh` SHALL export `TERM` with a default value of `xterm-256color` before launching tmux. Docker does not set `TERM` for non-TTY containers; tmux requires `TERM` for correct terminal capability detection.

#### Scenario: TERM exported before tmux
- **WHEN** the admin `entrypoint.sh` is inspected
- **THEN** it contains `export TERM="${TERM:-xterm-256color}"` before the `tmux new-session` line

#### Scenario: TERM preserves existing value
- **WHEN** `TERM` is already set in the environment and the admin container starts
- **THEN** the existing `TERM` value is preserved (the `${TERM:-xterm-256color}` pattern does not overwrite)

#### Scenario: TERM defaults when unset
- **WHEN** `TERM` is not set in the environment and the admin container starts
- **THEN** `TERM` is set to `xterm-256color`
