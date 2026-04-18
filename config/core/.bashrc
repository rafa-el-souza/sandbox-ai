HISTSIZE=50000
SAVEHIST=50000

export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export OPENSPEC_TELEMETRY=0

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"

# User Override Hook
if [ -f "/workspace/.sandbox/custom/.bashrc" ]; then
    source "/workspace/.sandbox/custom/.bashrc"
fi