#!/bin/sh
set -eu

# Set HISTFILE from environment if provided
if [ -n "${ZSH_HISTORY_PATH:-}" ]; then
    export HISTFILE="$ZSH_HISTORY_PATH"
fi

# Set TERM default for non-TTY container startup
export TERM="${TERM:-xterm-256color}"

# Execute structural daemon loop natively bounding the background process
tmux new-session -d -s sandbox

# Suspend the primary process loop infinitely within detached memory unconditionally
exec sleep infinity
