export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
echo '== uploaded source tarball present? =='
TARBALL=$(ls "$HOME"/sandbox-ai-src*.tar.gz 2>/dev/null | head -1)
[ -n "$TARBALL" ] || { echo PREP_TARBALL_MISSING; exit 1; }
echo '== extract + uv sync =='
rm -rf "$HOME/sandbox-ai"
mkdir -p "$HOME/sandbox-ai"
tar -xzf "$TARBALL" -C "$HOME/sandbox-ai"
uv sync --directory "$HOME/sandbox-ai" 2>&1 | tail -6
"$HOME/sandbox-ai/.venv/bin/sandbox" --help >/dev/null 2>&1 && echo PREP_SANDBOX_INSTALLED_OK || { echo PREP_SANDBOX_INSTALL_FAIL; exit 1; }
