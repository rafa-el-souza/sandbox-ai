#!/bin/sh
set -eu

# Execute structural daemon loop natively bounding the background process
tmux new-session -d -s sandbox

# Suspend the primary process loop infinitely within detached memory unconditionally
exec sleep infinity
