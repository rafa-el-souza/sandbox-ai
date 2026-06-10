"""Generate the attach-fwd (C-010) real-host acceptance battery from the frozen baseline.

This is the C-010 sibling of ``build_acceptance_separate_user_sudo.py`` (C-009). It derives an
acceptance battery from the tracked, frozen ``baseline-separate-user-sudo.json`` yardstick WITHOUT
mutating it (the baseline is the pre-fix matrix snapshot — F-096), and layers the deltas the
``attach-fwd-dispatch-op`` change (C-010) is validated against:

  * attach — flipped from the F-060 RED baseline (``vmlease-c009acc2`` attach FAIL x4: the unprivileged
    ``systemd-run`` ProxyCommand needs ``manage-units`` polkit auth, headless-blocked) to
    expected-PASS. The probe exercises a **non-interactive command-mode attach** in the validated
    ``pipe-attach`` shape (E-005 validation.md): the ssh ProxyCommand is the product's OWN crossing
    argv (``sudo systemd-run --pipe … /bin/bash -c 'dispatch fwd <wire>'`` — the C-010 fix routing
    attach through the dispatcher so the per-op sudoers ``Cmnd_Spec`` authorizes it headlessly), and
    ssh runs a short remote command (echoing a sentinel) instead of the interactive ``bash -l`` — no
    PTY, no ``tlog-rec``, no ``script(1)``. The ProxyCommand is obtained on the leased host via the
    product surface (``core.dispatch.proxy_argv``), NEVER a hand-typed mirror, so the probe crosses
    the REAL sudoers rule (F-019: a battery mirrors the real invocation). The sentinel coming back
    proves the whole chain: sudo authorized the ``fwd`` crossing headlessly → dispatcher streamed →
    admin ``/fwd`` → core sshd → auth → exec.

  * start — gates on the product's OWN ``up -d --build --wait`` "containers healthy" verdict instead
    of the baseline's in-session ``machinectl shell`` PTY core-running assertion (the F-018/F-055
    empty-crossing class — a false ``START_CORE_RUNNING_FAIL`` on the apt family). Standing C-009 fix
    (F-097), carried forward verbatim from the C-009 builder.
  * core-running — a separate, authoritative "is core up?" probe that reads docker via a NON-PTY
    ``sudo -u <user> env XDG_RUNTIME_DIR/DOCKER_HOST docker ps`` crossing. Standing C-009 fix (F-097),
    carried forward verbatim from the C-009 builder.

Every OTHER probe is kept identical to the C-009 acceptance battery so the rest of the matrix stays
directly comparable to the ``vmlease-c009acc2`` baseline. (The C-009 builder also adds a
``preflight-crossing-count`` probe; this builder reuses the C-009 builder's derivation wholesale and
then substitutes only the ``attach`` probe, so that probe is inherited too.)

The generated battery carries no host/personal/network data — it is pure probe definitions over the
product's own identifiers (``sandbox@.host``, the ``smoke`` test instance). Raw run outputs (which DO
carry host IPs/transcripts) are written by ``vmlease`` to its ``--results-dir``, kept under the
gitignored exploration tree, never here.

Usage:
    uv run python tests/vmlease/build_acceptance_attach_fwd.py --out <path-to-emit-battery.json>
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline-separate-user-sudo.json"


def _load_c009_builder() -> ModuleType:
    """Load the sibling C-009 builder by path (the SSOT for the shared derivation).

    The C-010 battery is the C-009 battery with ONLY the ``attach`` probe flipped, so the C-009
    derivation (product ``--wait`` start gate + non-PTY ``sudo -u`` core-running probe +
    preflight-crossing-count — F-097) is the single source of truth and is reused, never duplicated.
    ``tests/vmlease`` is not a Python package (no ``__init__.py``; not on ``sys.path`` / ``mypy_path``
    — these are vmlease batteries, not collected source), so the sibling is loaded by file path
    rather than imported as a module — this keeps the gate (``mypy .`` over the whole tree) clean
    without widening any package config for a non-source helper directory.
    """
    spec = importlib.util.spec_from_file_location(
        "build_acceptance_separate_user_sudo",
        HERE / "build_acceptance_separate_user_sudo.py",
    )
    if spec is None or spec.loader is None:
        raise SystemExit("FATAL: cannot load the sibling C-009 builder (build_acceptance_separate_user_sudo.py)")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# The product-surface one-liner the attach probe runs ON THE LEASED HOST to obtain the REAL
# ProxyCommand argv (F-019 — never a hand-typed mirror). It calls `core.dispatch.proxy_argv(Op.FWD,
# ['smoke'], host_config)` — the single sanctioned producer of the `dispatch fwd <wire>` payload —
# under a SUDO-mode minimal host_config, and prints `shlex.join(...)`. proxy_argv resolves the wire
# (`--project <P> --ip <IP>`) operator-side and prepends `sudo_pipe_cmd(sandbox)`, so the printed
# string is exactly the `sudo systemd-run -q --pipe --uid=sandbox /bin/bash -c '<dispatch> fwd
# <wire>'` crossing that crosses the real per-op sudoers Cmnd_Spec.
_PROXY_ARGV_PYC = (
    "import shlex; "
    "from core.dispatch import proxy_argv, Op; "
    "from core.host_config import minimal_host_config, MachinectlAuth; "
    "hc = minimal_host_config('sandbox', MachinectlAuth.SUDO); "
    "print(shlex.join(proxy_argv(Op.FWD, ['smoke'], hc)))"
)


def _attach_probe() -> dict[str, Any]:
    """C-010: non-interactive command-mode attach over the product's own dispatch-fwd ProxyCommand.

    Flips the F-060 RED baseline (attach FAIL x4) to expected-PASS. The ProxyCommand is obtained via the
    product surface (``proxy_argv``), so the crossing exercises the real ``sudo systemd-run --pipe …
    dispatch fwd <wire>`` argv against the real per-op sudoers ``Cmnd_Spec`` — headlessly. ssh runs a
    short remote command (``whoami`` + a sentinel) with the per-instance key/known_hosts + the
    hardened client options the product pins, NO PTY (``-T``) and NO ``tlog-rec``. The sentinel
    returning proves the full chain authorized + streamed end-to-end.
    """
    command = (
        'export PATH="$HOME/sandbox-ai/.venv/bin:$PATH"; rc=0; '
        'SBROOT="$HOME/sandbox-ai"; SECRETS="$HOME/.sandbox-ai/instances/smoke/secrets"; '
        "echo '== precondition: core up (read via the reliable non-PTY sudo -u crossing) =='; "
        "SBUID=$(id -u sandbox 2>/dev/null); "
        "CT=$(sudo -u sandbox env XDG_RUNTIME_DIR=/run/user/$SBUID "
        "DOCKER_HOST=unix:///run/user/$SBUID/docker.sock "
        'docker ps --format "{{.Names}}" 2>&1 | tr -d \'\\r\'); '
        "echo \"$CT\" | grep -q -- '-smoke-admin-1' && echo ATTACH_PRECOND_ADMIN_RUNNING_OK "
        "|| { echo ATTACH_PRECOND_ADMIN_NOT_RUNNING_FAIL; rc=1; }; "
        "echo '== obtain the REAL ProxyCommand from the product surface (proxy_argv, NOT hand-typed) =='; "
        'PROXY=$(PYTHONPATH="$SBROOT/src" "$SBROOT/.venv/bin/python3" -c '
        + repr(_PROXY_ARGV_PYC)
        + " 2>/tmp/proxy_build.err); "
        'if [ -z "$PROXY" ]; then echo "--- proxy build err ---"; tail -8 /tmp/proxy_build.err; '
        "echo ATTACH_PROXY_BUILD_FAIL; rc=1; echo \"$rc\" >/dev/null; exit $rc; fi; "
        'echo "PROXY=$PROXY"; '
        "echo \"$PROXY\" | grep -q 'dispatch fwd' && echo ATTACH_PROXY_IS_DISPATCH_FWD_OK "
        "|| { echo ATTACH_PROXY_NOT_DISPATCH_FWD_FAIL; rc=1; }; "
        "echo \"$PROXY\" | grep -q 'sudo' && echo ATTACH_PROXY_SUDO_PIPE_OK "
        "|| { echo ATTACH_PROXY_NOT_SUDO_PIPE_FAIL; rc=1; }; "
        "echo '== resolve the core IPC IP via the SAME product resolver attach uses =='; "
        'IP=$(PYTHONPATH="$SBROOT/src" "$SBROOT/.venv/bin/python3" -c '
        "'from core.dispatch import resolve_fwd_state; print(resolve_fwd_state(\"smoke\")[1])' "
        "2>/tmp/ip_build.err); "
        'echo "CORE_IPC_IP=$IP"; '
        '[ -n "$IP" ] && echo ATTACH_IP_RESOLVED_OK || { echo ATTACH_IP_RESOLVE_FAIL; rc=1; }; '
        "echo '== non-interactive command-mode attach: ssh (-T, no PTY) runs a remote sentinel =='; "
        "echo '== client opts mirror the product pins; ProxyCommand is the product argv above =='; "
        "SRAW=$(ssh -T -F /dev/null "
        '-i "$SECRETS/ipc_ssh_key" '
        '-o UserKnownHostsFile="$SECRETS/ipc_known_hosts" '
        "-o StrictHostKeyChecking=yes "
        "-o IdentitiesOnly=yes "
        "-o IdentityAgent=none "
        "-o ForwardAgent=no "
        "-o ForwardX11=no "
        "-o ClearAllForwardings=yes "
        "-o PermitLocalCommand=no "
        '-o ProxyCommand="$PROXY" '
        "-p 9999 "
        '"agent@$IP" '
        "'echo __ATTACH_OK_$(whoami)_$?__' 2>/tmp/attach_ssh.err); "
        'SEX=$?; echo "ssh exit: $SEX"; '
        'echo "--- ssh remote output ---"; printf "%s\\n" "$SRAW"; '
        'echo "--- ssh stderr (tail) ---"; tail -12 /tmp/attach_ssh.err 2>/dev/null; '
        "echo '== PRIMARY: the remote sentinel came back -> the whole fwd chain authorized + streamed =='; "
        'if [ "$SEX" -eq 0 ] && printf "%s" "$SRAW" | grep -q "__ATTACH_OK_agent_0__"; '
        "then echo ATTACH_FWD_HEADLESS_OK; else echo ATTACH_FWD_HEADLESS_FAIL; rc=1; fi; "
        "exit $rc"
    )
    return {
        "id": "attach",
        "title": (
            "C-010 F-060 fix: NON-INTERACTIVE command-mode attach over the product's own "
            "`sudo systemd-run --pipe … dispatch fwd <wire>` ProxyCommand -> headless success"
        ),
        "tag": "mutating:host-root",
        "timeout": 180,
        "classifies": (
            "C-010 acceptance (flips the F-060 RED baseline attach FAIL x4 to PASS): obtains the REAL "
            "ProxyCommand from the product surface (`core.dispatch.proxy_argv(Op.FWD, ['smoke'], "
            "host_config)` — the single sanctioned `dispatch fwd <wire>` producer, NOT a hand-typed "
            "mirror — F-019), so the crossing exercises the genuine `sudo systemd-run --pipe … "
            "dispatch fwd <wire>` argv against the real per-op sudoers Cmnd_Spec. ssh runs in "
            "command mode (`-T`, no PTY, no tlog-rec) with the per-instance key/known_hosts + the "
            "hardened client options the product pins, executing a remote `whoami` sentinel instead "
            "of the interactive `bash -l`. Gate ATTACH_FWD_HEADLESS_OK: the `__ATTACH_OK_agent_0__` "
            "sentinel returns -> sudo authorized the fwd crossing headlessly, the dispatcher "
            "streamed, the admin /fwd reached core sshd, auth + exec succeeded."
        ),
        "command": command,
    }


def build(baseline_path: Path) -> dict[str, Any]:
    """Derive the C-010 acceptance battery: the C-009 derivation with the attach probe flipped.

    Reuses ``build_acceptance_separate_user_sudo.build`` wholesale (the frozen baseline is read,
    never mutated — F-096; the standing C-009 fixes — product ``--wait`` start gate, non-PTY
    ``sudo -u`` core-running probe, preflight-crossing-count — F-097, all carried forward), then
    substitutes ONLY the ``attach`` probe in place (expected-FAIL -> expected-PASS).
    """
    c009_builder = _load_c009_builder()
    c009 = c009_builder.build(baseline_path)
    probes: list[dict[str, Any]] = c009["probes"]

    try:
        attach_idx = next(i for i, p in enumerate(probes) if p.get("id") == "attach")
    except StopIteration as exc:
        raise SystemExit(
            "FATAL: no `attach` probe in the derived C-009 battery — the baseline/derivation changed "
            "shape; re-confirm the attach probe id before regenerating."
        ) from exc
    probes[attach_idx] = _attach_probe()

    return {"name": "acceptance-attach-fwd", "probes": probes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path, help="path to write the generated battery JSON")
    parser.add_argument("--baseline", type=Path, default=BASELINE, help="frozen baseline (default: sibling)")
    args = parser.parse_args()

    battery = build(args.baseline)
    out_path: Path = args.out
    out_path.write_text(json.dumps(battery, indent=2) + "\n")
    n = len(battery["probes"])
    print(f"wrote {out_path} ({n} probes; attach=expected-PASS via the product dispatch-fwd ProxyCommand)")


if __name__ == "__main__":
    main()
