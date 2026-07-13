#!/bin/bash
# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail

# Set HISTFILE from environment if provided
if [ -n "${BASH_HISTORY_PATH:-}" ]; then
    export HISTFILE="$BASH_HISTORY_PATH"
fi

exec /usr/sbin/sshd -D -e
