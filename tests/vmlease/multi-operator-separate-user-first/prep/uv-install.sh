echo '== install uv =='
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env" 2>/dev/null || . "$HOME/.cargo/env" 2>/dev/null || true
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
uv --version || { echo PREP_UV_FAIL; exit 1; }
