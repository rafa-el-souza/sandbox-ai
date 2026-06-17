# Install the shared probe-helper library to a fixed host path (/tmp/sbai-helpers.sh)
# that every probe in this battery sources. vmlease `script=` inlines THIS file's
# text as the prep command; the heredoc below then materializes the helper at a
# fixed absolute path (same write-a-helper-to-/tmp pattern mk-operators.sh uses for
# the per-operator uv-sync scripts), so probe `run` blocks can `. /tmp/sbai-helpers.sh`.
#
# The helpers close four C-013 battery blindspots in ONE place (no per-probe copy):
#   - per-operator bridge-group resolution (sb-ws-<op> op-rootless / sb-ws separate-user)
#     read from the RUNTIME source of truth (the per-operator setup-state marker),
#   - forensic capture of a gated container's logs/state via the owner's rootless DOCKER_HOST,
#   - a capture-before-destroy teardown sentinel so a failed run is not self-erasing.

# Clear any stale forensics sentinel from a previous run on a recycled host.
rm -f /tmp/sbai-forensics-needed

cat > /tmp/sbai-helpers.sh <<'SBAI_HELPERS_EOF'
# shellcheck shell=bash
# Shared probe helpers — sourced by battery probes. See install-helpers.sh for intent.
SBAI_FORENSICS_SENTINEL="/tmp/sbai-forensics-needed"

# sbai_bridge_group <operator>
#   Echo the workspace bridge group NAME recorded for <operator> in the root-owned,
#   world-readable setup-state marker — i.e. the exact field the RUNTIME reads
#   (HostSettings.workspace_bridge_group), resolved through the shipped product
#   surface (core.setup_state.read_entry). op-rootless -> sb-ws-<operator>;
#   separate-user -> sb-ws. Empty when the operator has no marker entry.
#   This is THE single resolver that replaces every hard-coded `getent group sb-ws`,
#   so a probe asserts against the group the operator ACTUALLY uses, not a guess.
sbai_bridge_group() {
  _sbai_op="$1"
  _sbai_root="${SBAI_VENV_ROOT:-$HOME/sandbox-ai}"
  PYTHONPATH="$_sbai_root/src" "$_sbai_root/.venv/bin/python3" -c '
import sys
from core.setup_state import read_entry
entry = read_entry(sys.argv[1])
print(entry.workspace_bridge_group if entry else "")
' "$_sbai_op" 2>/dev/null
}

# sbai_bridge_gid <operator>
#   Echo the LIVE gid (via getent) of <operator>'s marker-recorded bridge group, or
#   empty when the operator has no entry or the group is absent on the host.
sbai_bridge_gid() {
  _sbai_name=$(sbai_bridge_group "$1")
  [ -n "$_sbai_name" ] || return 0
  getent group "$_sbai_name" 2>/dev/null | cut -d: -f3
}

# _sbai_docker <owner> <docker-args...>
#   Run `docker <args>` against <owner>'s rootless daemon (its XDG_RUNTIME_DIR +
#   DOCKER_HOST). Runs directly when <owner> is the current user, else via `sudo -u`.
_sbai_docker() {
  _sbai_owner="$1"; shift
  _sbai_uid=$(id -u "$_sbai_owner" 2>/dev/null)
  [ -n "$_sbai_uid" ] || return 0
  if [ "$_sbai_owner" = "$(id -un)" ]; then
    env XDG_RUNTIME_DIR="/run/user/$_sbai_uid" DOCKER_HOST="unix:///run/user/$_sbai_uid/docker.sock" docker "$@"
  else
    sudo -u "$_sbai_owner" env XDG_RUNTIME_DIR="/run/user/$_sbai_uid" DOCKER_HOST="unix:///run/user/$_sbai_uid/docker.sock" docker "$@"
  fi
}

# sbai_forensic_dump <owner> <container>
#   Capture a gated container's failure forensics — `docker ps -a`, `inspect .State`
#   (exit code + OOM + error), and `logs --tail=200` — read through <owner>'s rootless
#   daemon. Called on a probe's container-gating _FAIL branch BEFORE it exits, so the
#   cause of an exited/unhealthy container is in the run record even if teardown runs.
sbai_forensic_dump() {
  _sbai_fo="$1"; _sbai_fc="$2"
  echo "== FORENSIC DUMP container=$_sbai_fc owner=$_sbai_fo (rootless DOCKER_HOST) =="
  _sbai_docker "$_sbai_fo" ps -a --format '{{.Names}} {{.Status}}' 2>&1 | tr -d '\r' | sed 's/^/  ps: /'
  _sbai_docker "$_sbai_fo" inspect --format '{{json .State}}' "$_sbai_fc" 2>&1 | tr -d '\r' | sed 's/^/  state: /'
  _sbai_docker "$_sbai_fo" logs --tail=200 "$_sbai_fc" 2>&1 | tr -d '\r' | sed 's/^/  log: /'
  echo "== END FORENSIC DUMP container=$_sbai_fc =="
}

# sbai_mark_forensics <owner> <container>
#   Record (owner container) so the terminal teardown probe captures it before any
#   destroy AND preserves the live host instead of erasing the evidence.
sbai_mark_forensics() {
  printf '%s %s\n' "$1" "$2" >> "$SBAI_FORENSICS_SENTINEL"
}

# sbai_teardown_guard
#   Capture-before-destroy gate for the terminal teardown probe. If any gated probe
#   marked a failed container: dump each one's forensics, then return 1 (the caller
#   MUST skip destroy/disable-linger so the orchestrator can inspect the live host).
#   Returns 0 (proceed with normal teardown) when nothing is pending.
sbai_teardown_guard() {
  [ -s "$SBAI_FORENSICS_SENTINEL" ] || return 0
  echo "== teardown forensic guard: failed gated container(s) pending — capturing before destroy =="
  while read -r _sbai_go _sbai_gc; do
    [ -n "$_sbai_gc" ] || continue
    sbai_forensic_dump "$_sbai_go" "$_sbai_gc"
  done < "$SBAI_FORENSICS_SENTINEL"
  return 1
}

# sbai_doctor_hard_violations <doctor-output-file>
#   Echo the doctor ✗ (red, hard-fail) rows that are NOT env-capability/network
#   exceptions — i.e. the rows that represent a real readiness regression. ACL
#   support (host-fs capability), image-digest/registry reachability (network), and
#   the init-ordering rows (state dir writable / per-user tree exists, which a
#   not-yet-init'd operator legitimately lacks) are EXCLUDED — they are surfaced as
#   _review by the caller, never gated. ⚠ (yellow) advisory rows never match (we grep
#   the ✗ glyph only), so a stale-shared-group warning is not a hard violation.
sbai_doctor_hard_violations() {
  grep '✗' "$1" 2>/dev/null \
    | grep -ivE "ACL support|image digest|registry|state dir writable|per-user tree exists" \
    || true
}
SBAI_HELPERS_EOF
chmod 0644 /tmp/sbai-helpers.sh
echo "INSTALL_HELPERS_OK"
