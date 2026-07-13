# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Byte-equivalence fixture gate for ``_build_attach_argv()``.

Per ``admin-reframe`` design D9 and the methodology rule "fixture-diff for
contract surfaces": the argv produced by :func:`cli.main._build_attach_argv`
is the operator-visible attach contract — it determines the exact PTY-
handover invocation that runs on the dev-side host. A unit test asserting
structural fields (in ``test_cli.py::TestBuildAttachArgv``) is necessary
but insufficient; this fixture-diff catches regressions that field-level
assertions miss (token order, quoting drift, accidental flag insertion,
etc.).

The separate-user ``ProxyCommand`` is obtained from
``core.dispatch.proxy_argv(Op.FWD, …)``, which crosses via ``sudo_pipe_cmd``
(the privileged byte-pipe, the F-060 headless fix) carrying the bare
``dispatch fwd <wire>`` payload.

Determinism strategy mirrors ``test_dry_run_fixture.py``:

* ``IPAMLedger`` is pointed at a tmp ledger pre-seeded with
  ``base_index=0`` so :func:`derive_static_ips` returns ``10.100.4.3``
  for ``core_ipc_ip``.
* ``SANDBOX_AI_HOME`` is pinned to ``/tmp/sandbox-test`` so secrets and
  session-log paths are stable.
* The session-log timestamp is stubbed by patching ``datetime`` in
  ``cli.main`` so ``datetime.now(UTC).strftime(...)`` returns
  ``"20260510T000000Z"`` deterministically.

Regenerating the fixtures::

    SANDBOX_AI_REGEN_ATTACH_FIXTURE=1 \\
        uv run pytest tests/unit/cli/test_attach_fixture.py
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "attach"
FIXTURE_SUDO = FIXTURE_DIR / "argv_sudo.txt"

# Stable values pinned by the fixture; both fixtures must encode these.
_STABLE_HOME = Path("/tmp/sandbox-test")
_STABLE_TIMESTAMP = "20260510T000000Z"
_STABLE_INST = "test-inst"
_STABLE_WS = "main"
_STABLE_SBUSER = "claude-sandbox"


class _FrozenDatetime:
    """Stand-in for ``cli.main.datetime`` whose ``now(...).strftime(...)``
    returns ``_STABLE_TIMESTAMP`` regardless of input."""

    @staticmethod
    def now(_tz: object = None) -> _FrozenDatetime:
        return _FrozenDatetime()

    def strftime(self, _fmt: str) -> str:
        return _STABLE_TIMESTAMP


def _capture_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    from cli.main import _build_attach_argv
    from core.host_config import HostConfig

    # Ledger is read-only here; point it at a tmp file pre-seeded with
    # base_index=0 so peek_next_slot returns (0, True) deterministically.
    ledger_path = tmp_path / "ipam.json"
    ledger_path.write_text(f'{{"{_STABLE_INST}": 0}}\n')
    monkeypatch.setattr("core.ipam._default_ledger_path", lambda: str(ledger_path))

    # Pin SANDBOX_AI_HOME to a stable path so all path tokens in the
    # fixture are reproducible. The directory must exist for the
    # session-log mkdir(parents=True, exist_ok=True) call to succeed
    # without requiring a real /tmp/sandbox-test on disk.
    monkeypatch.setenv("SANDBOX_AI_HOME", str(_STABLE_HOME))
    # Pin the compose project-name user component (getpwuid-based, ignores
    # $USER): the fixture argv was recorded as 'dev' and must not depend on
    # the invoking account (CI runs as 'runner').
    monkeypatch.setattr("core.compose._resolve_dev_username", lambda: "dev")
    # `_build_attach_argv` calls `session_log_dir.mkdir(parents=True,
    # exist_ok=True)`; redirect that to a tmp shadow via a Path subclass
    # would be heavy. Simpler: stub Path.mkdir on the session_log_dir
    # path object. The cleanest hook is to patch Path.mkdir in cli.main.
    real_mkdir = Path.mkdir

    def _mkdir_no_op(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        # Allow real mkdirs anywhere outside the stable home; suppress
        # the one that would touch /tmp/sandbox-test.
        try:
            self.relative_to(_STABLE_HOME)
        except ValueError:
            real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)
            return
        # Inside the stable home — no-op (directory does not need to
        # exist for the argv to be assembled).

    monkeypatch.setattr(Path, "mkdir", _mkdir_no_op)

    # Freeze the timestamp embedded in the session-log filename.
    monkeypatch.setattr("cli.main.datetime", _FrozenDatetime)

    # This fixture pins the SEPARATE-USER attach argv (the sudo-pipe ProxyCommand).
    # The system-wide default mode is now operator-rootless (DEFAULT_PROVISIONING_MODE,
    # F-051), so the separate-user crossing must be requested explicitly rather than
    # relying on the moot field default.
    fixed_host = HostConfig.model_validate(
        {
            "host": {
                "docker_unprivileged_user": _STABLE_SBUSER,
                "docker_execution_mode": "separate-user",
            }
        }
    )

    return _build_attach_argv(_STABLE_INST, _STABLE_WS, fixed_host)


def _argv_to_text(argv: list[str]) -> str:
    """Render argv as one element per line + trailing newline (fixture format)."""
    return "\n".join(argv) + "\n"


def _assert_or_regen(captured: str, fixture_path: Path) -> None:
    if os.environ.get("SANDBOX_AI_REGEN_ATTACH_FIXTURE"):
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(captured)
        return
    assert fixture_path.exists(), (
        f"Fixture {fixture_path} missing. Regenerate via "
        "SANDBOX_AI_REGEN_ATTACH_FIXTURE=1 uv run pytest "
        f"{Path(__file__).name}"
    )
    expected = fixture_path.read_text()
    if captured != expected:
        # Produce a readable line-by-line diff in the assertion message
        # so a divergence points at the offending element directly.
        captured_lines = captured.splitlines()
        expected_lines = expected.splitlines()
        diff_lines: list[str] = []
        for i, (got, want) in enumerate(zip(captured_lines, expected_lines, strict=False)):
            marker = " " if got == want else "!"
            diff_lines.append(f"  [{i}]{marker} got={got!r}  want={want!r}")
        if len(captured_lines) != len(expected_lines):
            diff_lines.append(
                f"  length mismatch: got {len(captured_lines)} lines, "
                f"want {len(expected_lines)} lines"
            )
        raise AssertionError(
            f"_build_attach_argv output diverged from {fixture_path.name}.\n"
            + "\n".join(diff_lines)
            + "\nIf the divergence is intentional, regenerate the fixture via "
            "SANDBOX_AI_REGEN_ATTACH_FIXTURE=1 and review the diff before committing."
        )


class TestAttachArgvFixtureGate:
    """Pin ``_build_attach_argv`` argv to a checked-in fixture for both auth modes."""

    def test_attach_argv_matches_fixture_sudo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = _capture_argv(monkeypatch, tmp_path)
        _assert_or_regen(_argv_to_text(argv), FIXTURE_SUDO)

class TestAttachArgvStructural:
    """NIT-catcher structural assertions — complement to the fixture-diff.

    These assertions check shape invariants that the fixture-diff also
    catches but encodes the *intent* of each token, so a future reader
    can map a diff to a contract clause quickly. The fixture-diff is the
    load-bearing test; this class is documentation-by-test.
    """

    def _argv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
        return _capture_argv(monkeypatch, tmp_path)

    def test_argv_starts_with_tlog_rec_writer_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._argv(monkeypatch, tmp_path)
        assert argv[0] == "tlog-rec"
        assert "--writer=file" in argv

    def test_argv_ssh_ignores_operator_config_via_dash_capital_f_devnull(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Hardened ssh client (cli-attach "Hardened ssh Client Invocation"):
        # ``-F /dev/null`` precedes any ``-o`` option so no ~/.ssh/config or
        # system ssh_config stanza can alter the connection's posture.
        argv = self._argv(monkeypatch, tmp_path)
        ssh_idx = argv.index("ssh")
        assert argv[ssh_idx + 1] == "-F"
        assert argv[ssh_idx + 2] == "/dev/null"
        first_o = next(i for i, a in enumerate(argv) if a == "-o")
        assert argv.index("-F") < first_o

    def test_argv_ssh_identity_points_at_ipc_ssh_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._argv(monkeypatch, tmp_path)
        i_idx = argv.index("-i")
        assert argv[i_idx + 1].endswith("/secrets/ipc_ssh_key")

    def test_argv_known_hosts_option_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._argv(monkeypatch, tmp_path)
        assert any(a.startswith("UserKnownHostsFile=") for a in argv)

    def test_argv_strict_host_key_checking_enforced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._argv(monkeypatch, tmp_path)
        assert "StrictHostKeyChecking=yes" in argv

    def test_argv_forwarding_and_agent_access_pinned_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # cli-attach "Forwarding and agent access pinned off" scenario.
        argv = self._argv(monkeypatch, tmp_path)
        for opt in (
            "ForwardAgent=no",
            "ForwardX11=no",
            "ClearAllForwardings=yes",
            "IdentitiesOnly=yes",
            "IdentityAgent=none",
            "PermitLocalCommand=no",
        ):
            assert opt in argv, f"missing hardening option {opt!r}"

    def test_argv_proxy_command_sudo_mode_uses_sudo_pipe_cmd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # C-010 / F-060: SUDO separate-user attach crosses via the privileged
        # ``sudo_pipe_cmd`` (the headless-capable byte-pipe), carrying the bare
        # ``dispatch fwd <wire>`` payload that the per-op sudoers Cmnd_Spec matches.
        from core.host_config import sudo_pipe_cmd

        argv = self._argv(monkeypatch, tmp_path)
        proxy = next(a for a in argv if a.startswith("ProxyCommand="))
        proxy_value = proxy[len("ProxyCommand=") :]
        proxy_tokens = shlex.split(proxy_value)
        priv = sudo_pipe_cmd(_STABLE_SBUSER)
        assert proxy_tokens[: len(priv)] == priv
        # The crossed payload is the bare dispatch fwd wire, never a hand-built
        # docker-exec argv here (proxy_argv is the sole producer).
        assert "/usr/local/libexec/sandbox-ai/dispatch fwd" in proxy_value

    def test_argv_target_port_is_9999(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._argv(monkeypatch, tmp_path)
        p_idx = argv.index("-p")
        assert argv[p_idx + 1] == "9999"

    def test_argv_request_pty_via_dash_t(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._argv(monkeypatch, tmp_path)
        assert "-t" in argv

    def test_argv_user_at_host_is_agent_at_core_ipc_ip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._argv(monkeypatch, tmp_path)
        assert any(a.startswith("agent@") for a in argv)

    def test_argv_remote_command_suffix_cds_into_workspace_then_execs_bash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._argv(monkeypatch, tmp_path)
        assert argv[-1] == f"cd /workspaces/{_STABLE_WS} && exec bash -l"
