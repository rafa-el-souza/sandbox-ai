"""Generate the separate-user.sudo real-host acceptance battery from the frozen baseline.

This derives an acceptance battery from the tracked, frozen `baseline-separate-user-sudo.json`
yardstick WITHOUT mutating it (the baseline is the pre-fix matrix snapshot — see README + F-096).
It reuses the baseline's per-command lifecycle probes verbatim and layers the deltas that the
`crossing-sudo-systemd-run` change (C-009) is validated against:

  * start  — the baseline `start` probe's core-running assertion crossed into the sandbox user via an
    interactive `machinectl shell` PTY (the F-018/F-055 empty-crossing class — it returns empty when
    performed inside the same session that ran `sandbox start`, producing a false `START_CORE_RUNNING_FAIL`
    on the apt family). This builder replaces that assertion with the product's OWN `up -d --build --wait`
    verdict (start exit 0 + no crossing error + the "containers healthy" line) — a non-crossing signal.
  * core-running — a separate, authoritative "is core up?" probe that reads docker via a NON-PTY
    `sudo -u <user> env XDG_RUNTIME_DIR/DOCKER_HOST docker ps` crossing. Real-host runs showed the non-PTY
    crossing is reliable both inside and outside the start session, whereas the `machinectl shell` PTY
    crossing is not (F-097 — harden a harness's own verification crossing; don't reuse the fragile
    primitive the product itself moved off of).
  * preflight-crossing-count — re-runs `sandbox start` on the already-running instance and counts the
    dispatcher's structured journald op audit: exactly one `preflight` + one `compose-ps` crossing, and
    ZERO of the old per-check ops as separate crossings (C-009 D6 8->2 burst-collapse).

The generated battery carries no host/personal/network data — it is pure probe definitions over the
product's own identifiers (`sandbox@.host`, the `smoke` test instance). Raw run outputs (which DO carry
host IPs/transcripts) are written by `vmlease` to its `--results-dir`, kept under the gitignored
exploration tree, never here.

Usage:
    uv run python tests/vmlease/build_acceptance_separate_user_sudo.py --out <path-to-emit-battery.json>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline-separate-user-sudo.json"

# Anchors bounding the baseline `start` core-running + health block (an interactive `machinectl shell`
# PTY crossing — the fragile primitive). The block runs from the assert-core banner up to the next
# banner; we slice it out and substitute the product-signal gate, leaving the rest of the baseline
# `start` command (the `sandbox start` call, crossing-error guard, non-gating diagnostics, `exit $rc`)
# byte-for-byte unchanged. Slicing-by-anchor avoids embedding the long PTY-crossing literal here.
_CORE_BLOCK_START = "echo '== assert CORE running"
_CORE_BLOCK_END = "echo '== crossing mechanism"

# Gate on the product's OWN `up -d --build --wait` verdict instead of an interactive crossing.
_PRODUCT_SIGNAL_GATE = (
    "echo '== core-up via the product OWN --wait verdict (no interactive crossing) =='; "
    "if grep -qiE 'containers healthy' /tmp/start.out; then echo START_COMPOSE_HEALTHY_OK; "
    "else echo START_COMPOSE_HEALTHY_FAIL; rc=1; fi; "
)


def _core_running_probe() -> dict[str, Any]:
    """Authoritative core-running via a NON-PTY `sudo -u` crossing (F-097)."""
    command = (
        "rc=0; echo '== authoritative core-running via the non-PTY sudo -u crossing =='; "
        "SBUID=$(id -u sandbox 2>/dev/null); FOUND=0; CT=''; "
        "for i in $(seq 1 10); do "
        "CT=$(sudo -u sandbox env XDG_RUNTIME_DIR=/run/user/$SBUID "
        "DOCKER_HOST=unix:///run/user/$SBUID/docker.sock "
        'docker ps --format "{{.Names}} {{.Status}}" 2>&1 | tr -d \'\\r\'); '
        "if echo \"$CT\" | grep -q -- '-smoke-core-1'; then "
        'FOUND=1; echo "core visible on attempt $i"; break; fi; '
        'sleep 2; done; echo "containers: $CT"; '
        "if [ \"$FOUND\" -eq 1 ]; then echo CORE_RUNNING_OK; else echo CORE_RUNNING_FAIL; rc=1; fi; "
        "exit $rc"
    )
    return {
        "id": "core-running",
        "title": "core container is running, read via the non-PTY sudo -u crossing (reliable in/out of session)",
        "tag": "mutating:host-root",
        "timeout": 120,
        "classifies": (
            "After `start`, assert `-smoke-core-1` is in `docker ps` read as the sandbox user via a NON-PTY "
            "`sudo -u <user> env XDG_RUNTIME_DIR DOCKER_HOST docker ps` crossing (NOT `machinectl shell`, "
            "whose PTY crossing returns empty in-session on apt — F-018/F-055/F-097). Short poll for "
            "robustness; stderr kept. Gate CORE_RUNNING_OK — the cell that demonstrates the stack is up."
        ),
        "command": command,
    }


def _crossing_count_probe() -> dict[str, Any]:
    """C-009 D6 8->2 burst-collapse — re-run start (warm) and count read-only crossings."""
    command = (
        'export PATH="$HOME/sandbox-ai/.venv/bin:$PATH"; rc=0; '
        'echo "== record a journald window, then re-run start (warm path) =="; '
        'sleep 1; T=$(date "+%Y-%m-%d %H:%M:%S"); sleep 1; '
        "sandbox start smoke --no-handover > /tmp/recount.out 2>&1; SRC=$?; "
        'echo "re-run exit: $SRC"; tail -6 /tmp/recount.out; '
        'grep -qi "already running" /tmp/recount.out && echo CC_WARM_PATH_OK || echo CC_WARM_PATH_review; '
        'echo "== count read-only dispatch ops since T via SANDBOX_AI_OP =="; '
        'NPRE=$(sudo journalctl --since "$T" SANDBOX_AI_OP=preflight --no-pager 2>/dev/null '
        '| grep -c "dispatch preflight"); '
        'NPS=$(sudo journalctl --since "$T" SANDBOX_AI_OP=compose-ps --no-pager 2>/dev/null '
        '| grep -c "dispatch compose-ps"); '
        'echo "preflight ops=$NPRE  compose-ps ops=$NPS"; '
        'echo "== old per-check ops must NOT appear as SEPARATE crossings (bundled into preflight) =="; '
        "NOLD=0; for op in auth-probe docker-version docker-info compose-ls; do "
        'c=$(sudo journalctl --since "$T" SANDBOX_AI_OP=$op --no-pager 2>/dev/null '
        '| grep -c "dispatch $op"); '
        'echo "  $op separate crossings: $c"; NOLD=$((NOLD+c)); done; '
        'echo "old per-check separate crossings total: $NOLD"; '
        'if [ "$NPRE" -eq 1 ] && [ "$NPS" -eq 1 ] && [ "$NOLD" -eq 0 ]; then echo CC_TWO_CROSSINGS_OK; '
        "else echo CC_NOT_TWO_CROSSINGS_FAIL; rc=1; fi; "
        'echo "== diagnostic: all SANDBOX_AI_OP entries in the window (non-gating) =="; '
        'sudo journalctl --since "$T" --no-pager 2>/dev/null '
        '| grep -oE "dispatch [a-z-]+" | sort | uniq -c; '
        "echo CC_OP_HISTOGRAM_review; "
        "exit $rc"
    )
    return {
        "id": "preflight-crossing-count",
        "title": "start's read-only preflight is exactly TWO crossings (preflight + compose-ps), not ~8",
        "tag": "mutating:host-root",
        "timeout": 180,
        "classifies": (
            "C-009 D6 burst-collapse acceptance (faithful, real-host): re-runs `sandbox start smoke "
            "--no-handover` on the already-running instance so only the read-only preflight crossings "
            "fire (the warm-check returns 'already running' before any compose-up/helper crossing), then "
            "counts the dispatcher's structured journald op audit (SANDBOX_AI_OP=<op>) in that window. "
            "Gate: exactly one `preflight` op + one `compose-ps` op, and ZERO of the old per-check ops "
            "(auth-probe/docker-version/docker-info/compose-ls) as separate crossings."
        ),
        "command": command,
    }


def build(baseline_path: Path) -> dict[str, Any]:
    """Derive the acceptance battery dict from the frozen baseline (baseline never mutated)."""
    base: dict[str, Any] = json.loads(baseline_path.read_text())
    probes: list[dict[str, Any]] = [dict(p) for p in base["probes"]]

    start = next(p for p in probes if p.get("id") == "start")
    cmd: str = start["command"]
    try:
        a = cmd.index(_CORE_BLOCK_START)
        b = cmd.index(_CORE_BLOCK_END)
    except ValueError as exc:
        raise SystemExit(
            "FATAL: baseline `start` core-running block anchors not found — the frozen baseline changed "
            "shape; re-derive _CORE_BLOCK_START/_CORE_BLOCK_END before regenerating."
        ) from exc
    start["command"] = cmd[:a] + _PRODUCT_SIGNAL_GATE + cmd[b:]

    start_idx = next(i for i, p in enumerate(probes) if p.get("id") == "start")
    probes[start_idx + 1 : start_idx + 1] = [_core_running_probe(), _crossing_count_probe()]

    return {"name": "acceptance-separate-user-sudo", "probes": probes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path, help="path to write the generated battery JSON")
    parser.add_argument("--baseline", type=Path, default=BASELINE, help="frozen baseline (default: sibling)")
    args = parser.parse_args()

    battery = build(args.baseline)
    out_path: Path = args.out
    out_path.write_text(json.dumps(battery, indent=2) + "\n")
    n = len(battery["probes"])
    print(f"wrote {out_path} ({n} probes; start=product-signal gate, +core-running, +crossing-count)")


if __name__ == "__main__":
    main()
