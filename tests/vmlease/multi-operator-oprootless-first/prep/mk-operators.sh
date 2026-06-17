# Create the extra operators for the multi-operator (op-rootless first) arc.
#
# DRY refactor: replaces the per-operator mk-op2 / mk-op3f / mk-op4 [[prep.setup]]
# steps with ONE loop over the operator list, the create-logic written ONCE.
# vmlease prep script= inlines this file's TEXT as the step command (no arg
# passing, no host-side callable), so the dedup MUST be an internal loop.
#
# Behavior-equivalence with the separate mk-opN steps it replaces:
#   - same operators created (op2 op3f op4), same NOPASSWD sudoers drop-in,
#     same enable-linger (+ rundir poll), same R6 source distribution
#     (/tmp/${op}-src.tar.gz cp + chmod 0644), same per-operator uv sync, and
#     the same op3f subid-strip.
#   - FAIL-FAST: the original mk-op2/mk-op3f/mk-op4 were SEPARATE required=true
#     steps, so the first operator's failure hard-aborted before the next was
#     created. This loop replicates that: any operator sub-step failure makes
#     the loop exit non-zero (PREP_HARD_FAIL) WITHOUT creating later operators.
#   - cosmetic normalization: the original mk-op4 step OMITTED the linger rundir
#     poll; this loop applies the poll UNIFORMLY to every operator (op4 included).
OPERATORS="op2 op3f op4"

for op in $OPERATORS; do
  OPU=$(printf '%s' "$op" | tr '[:lower:]' '[:upper:]')
  echo "== create ${op} (idempotent) =="
  id "$op" >/dev/null 2>&1 || sudo useradd -m -s /bin/bash "$op"
  if id "$op" >/dev/null 2>&1; then echo "MK_${OPU}_USER_OK"; else echo "MK_${OPU}_USER_FAIL"; exit 1; fi

  echo "== passwordless sudo drop-in for ${op} (setup self-escalates via sudo -n) =="
  printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$op" | sudo tee "/etc/sudoers.d/${op}-sandbox" >/dev/null
  sudo chmod 0440 "/etc/sudoers.d/${op}-sandbox"
  if sudo visudo -cf "/etc/sudoers.d/${op}-sandbox" >/dev/null 2>&1; then echo "MK_${OPU}_SUDOERS_OK"; else echo "MK_${OPU}_SUDOERS_FAIL"; exit 1; fi

  echo "== enable linger for ${op} (materializes /run/user/<uid> without an interactive login) =="
  sudo loginctl enable-linger "$op"
  OP_UID=$(id -u "$op")
  GONE=0
  for i in $(seq 1 15); do
    if [ -d "/run/user/${OP_UID}" ]; then GONE=1; break; fi
    sleep 1
  done
  [ "$GONE" -eq 1 ] && echo "MK_${OPU}_LINGER_RUNDIR_OK" || echo "MK_${OPU}_LINGER_RUNDIR_review"

  echo "== ${op} installs its OWN copy of the uploaded source (per-operator venv) =="
  TARBALL=$(ls "$HOME"/sandbox-ai-src*.tar.gz 2>/dev/null | head -1)
  if [ -z "$TARBALL" ]; then echo "MK_${OPU}_TARBALL_MISSING_FAIL"; exit 1; fi
  sudo cp "$TARBALL" "/tmp/${op}-src.tar.gz"
  sudo chmod 0644 "/tmp/${op}-src.tar.gz"
  # UNQUOTED heredoc (<<SCRIPT): ${op} is BAKED at write-time (the per-operator tarball path);
  # \$HOME/\$PATH are backslash-escaped to DEFER to operator-run-time. Any NEW runtime $var added
  # below MUST be escaped (\$) or it will wrongly expand on op1's shell here. Single path arg to
  # `sudo -iu` (no sudo -i newline collapse).
  cat > "/tmp/${op}_uvsync.sh" <<SCRIPT
set -e
curl -LsSf https://astral.sh/uv/install.sh | sh
. "\$HOME/.local/bin/env" 2>/dev/null || . "\$HOME/.cargo/env" 2>/dev/null || true
export PATH="\$HOME/.local/bin:\$HOME/.cargo/bin:\$PATH"
rm -rf "\$HOME/sandbox-ai"
mkdir -p "\$HOME/sandbox-ai"
tar -xzf /tmp/${op}-src.tar.gz -C "\$HOME/sandbox-ai"
uv sync --directory "\$HOME/sandbox-ai" 2>&1 | tail -4
"\$HOME/sandbox-ai/.venv/bin/sandbox" --help >/dev/null 2>&1
SCRIPT
  chmod 0755 "/tmp/${op}_uvsync.sh"
  if sudo -iu "$op" bash "/tmp/${op}_uvsync.sh"; then echo "MK_${OPU}_UV_SYNC_OK"; else echo "MK_${OPU}_UV_SYNC_FAIL"; exit 1; fi

  if [ "$op" = "op3f" ]; then
    echo "== FORCE the entry-less F-071 precondition: strip op3f auto-assigned subid entries =="
    echo "op3f subid entries BEFORE strip:"
    grep -E "^op3f:" /etc/subuid /etc/subgid 2>/dev/null || echo '<none>'
    sudo sed -i '/^op3f:/d' /etc/subuid /etc/subgid
    if grep -qE "^op3f:" /etc/subuid /etc/subgid 2>/dev/null; then
      echo MK_OP3F_SUBID_STRIP_FAIL; exit 1
    else
      echo MK_OP3F_SUBID_STRIPPED_OK
    fi
  fi
done

echo MK_OPERATORS_ALL_OK
exit 0
