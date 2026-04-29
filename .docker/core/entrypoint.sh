#!/bin/bash
set -euo pipefail

# Set HISTFILE from environment if provided
if [ -n "${BASH_HISTORY_PATH:-}" ]; then
    export HISTFILE="$BASH_HISTORY_PATH"
fi

# Create sshd runtime directory and start OpenSSH daemon
mkdir -p /run/sshd
exec /usr/sbin/sshd -D -e
