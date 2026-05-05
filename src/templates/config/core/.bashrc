umask 007

HISTSIZE=50000
SAVEHIST=50000

# History file path injected by rendered compose.yml
if [ -n "${BASH_HISTORY_PATH:-}" ]; then
    export HISTFILE="$BASH_HISTORY_PATH"
fi
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export OPENSPEC_TELEMETRY=0

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"

# User Override Hook
if [ -f "{{ custom_config_core }}/.bashrc" ]; then
    source "{{ custom_config_core }}/.bashrc"
fi