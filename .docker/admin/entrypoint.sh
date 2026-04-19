#!/bin/sh
set -eu

# Set HISTFILE from environment if provided
if [ -n "${ZSH_HISTORY_PATH:-}" ]; then
    export HISTFILE="$ZSH_HISTORY_PATH"
fi

# Execute structural daemon loop natively bounding the background process
tmux new-session -d -s sandbox

# Suspend the primary process loop infinitely within detached memory unconditionally
exec sleep infinity
