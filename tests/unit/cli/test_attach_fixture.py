"""Byte-equivalence fixture gate for ``_build_attach_argv()``.

Per ``admin-reframe`` design D9 and the methodology rule "fixture-diff for
contract surfaces": the argv produced by :func:`cli.main._build_attach_argv`
is the operator-visible attach contract — it determines the exact PTY-
handover invocation that runs on the dev-side host. A unit test asserting
structural fields (in ``test_cli.py::TestBuildAttachArgv``) is necessary
but insufficient; this fixture-diff catches regressions that field-level
assertions miss (token order, quoting drift, accidental flag insertion,
etc.).

The two fixture files are byte-identical because :func:`pipe_cmd` is
auth-mode-independent (per design D2) — the ``ProxyCommand`` shape does
not vary with ``host.machinectl_authentication``. A dedicated cross-mode
invariant test pins this property.

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
FIXTURE_POLKIT = FIXTURE_DIR / "argv_polkit.txt"

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


def _capture_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, auth_mode: str) -> list[str]:
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

    fixed_host = HostConfig.model_validate(
        {"host": {"docker_unprivileged_user": _STABLE_SBUSER, "machinectl_authentication": auth_mode}}
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
        argv = _capture_argv(monkeypatch, tmp_path, "sudo")
        _assert_or_regen(_argv_to_text(argv), FIXTURE_SUDO)

    def test_attach_argv_matches_fixture_polkit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = _capture_argv(monkeypatch, tmp_path, "polkit")
        _assert_or_regen(_argv_to_text(argv), FIXTURE_POLKIT)

    def test_attach_argv_cross_mode_invariant(self) -> None:
        """The argv MUST be byte-identical across auth modes.

        Per design D2, ``pipe_cmd`` (used in the ``ProxyCommand``) is
        auth-mode-independent — it always emits ``systemd-run --pipe
        --uid=<user>`` and never the ``sudo machinectl`` form. The
        attach argv therefore does not vary with ``machinectl_authentication``.
        Pinning this as a load-bearing invariant via the fixtures
        themselves catches any future drift that re-introduces a sudo
        prefix on one mode but not the other.
        """
        assert FIXTURE_SUDO.read_text() == FIXTURE_POLKIT.read_text(), (
            "argv_sudo.txt and argv_polkit.txt diverged — `_build_attach_argv` "
            "is supposed to be auth-mode-independent (admin-reframe D2). If "
            "this divergence is intentional, update the design doc and remove "
            "this invariant test."
        )


class TestAttachArgvStructural:
    """NIT-catcher structural assertions — complement to the fixture-diff.

    These assertions check shape invariants that the fixture-diff also
    catches but encodes the *intent* of each token, so a future reader
    can map a diff to a contract clause quickly. The fixture-diff is the
    load-bearing test; this class is documentation-by-test.
    """

    def _argv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
        return _capture_argv(monkeypatch, tmp_path, "sudo")

    def test_argv_starts_with_tlog_rec_writer_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._argv(monkeypatch, tmp_path)
        assert argv[0] == "tlog-rec"
        assert "--writer=file" in argv

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

    def test_argv_proxy_command_uses_systemd_run_not_sudo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._argv(monkeypatch, tmp_path)
        proxy = next(a for a in argv if a.startswith("ProxyCommand="))
        assert "systemd-run" in proxy
        assert "sudo" not in proxy

    def test_argv_proxy_command_uses_unprivileged_pipe_cmd_not_sudo_pipe_cmd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # C-009 routed SUDO separate-user *dispatch* crossings onto the
        # privileged byte-pipe (``sudo_pipe_cmd`` = ``["sudo", *pipe_cmd]``),
        # but attach's ProxyCommand is UNTOUCHED: it crosses via the
        # unprivileged ``pipe_cmd`` (polkit ``manage-units``), so its
        # ProxyCommand begins with a bare ``systemd-run --pipe --uid=`` and
        # never the ``sudo``-prefixed ``sudo_pipe_cmd`` form. Asserted in SUDO
        # mode — the auth mode where the dispatch path *does* add ``sudo`` —
        # to prove attach does not share that routing.
        from core.host_config import pipe_cmd, sudo_pipe_cmd

        argv = self._argv(monkeypatch, tmp_path)
        proxy = next(a for a in argv if a.startswith("ProxyCommand="))
        proxy_value = proxy[len("ProxyCommand=") :]
        proxy_tokens = shlex.split(proxy_value)
        unpriv = pipe_cmd(_STABLE_SBUSER)
        priv = sudo_pipe_cmd(_STABLE_SBUSER)
        # The unprivileged prefix leads the ProxyCommand verbatim …
        assert proxy_tokens[: len(unpriv)] == unpriv
        # … and the privileged ``sudo``-prefixed prefix does NOT.
        assert proxy_tokens[: len(priv)] != priv

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
