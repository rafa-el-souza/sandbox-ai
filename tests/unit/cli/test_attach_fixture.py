"""Byte-equivalence fixture gate for ``_build_attach_argv()``.

Per ``admin-reframe`` design D9 and the methodology rule "fixture-diff for
contract surfaces": the argv produced by :func:`cli.main._build_attach_argv`
is the operator-visible attach contract — it determines the exact PTY-
handover invocation that runs on the dev-side host. A unit test asserting
structural fields (in ``test_cli.py::TestBuildAttachArgv``) is necessary
but insufficient; this fixture-diff catches regressions that field-level
assertions miss (token order, quoting drift, accidental flag insertion,
etc.).

The two fixture files DIVERGE by auth mode (C-010): the separate-user
``ProxyCommand`` is obtained from ``core.dispatch.proxy_argv(Op.FWD, …)``,
whose crossing prefix is auth-mode-selected — **SUDO** crosses via
``sudo_pipe_cmd`` (the privileged byte-pipe, the F-060 headless fix) and
**POLKIT** via the unprivileged ``pipe_cmd``. The crossed ``dispatch fwd
<wire>`` payload and the surrounding ssh argv (incl. the ``-F /dev/null``
hardening pins) are identical; only the ``sudo`` prefix on the ProxyCommand
differs. A dedicated cross-mode divergence test pins this property.

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

    def test_attach_argv_cross_mode_diverges_only_on_sudo_prefix(self) -> None:
        """The two fixtures differ ONLY by the ``sudo`` prefix on the ProxyCommand.

        Per C-010, the separate-user ProxyCommand comes from
        ``core.dispatch.proxy_argv(Op.FWD, …)``, whose crossing prefix is
        auth-mode-selected: SUDO → ``sudo_pipe_cmd`` (``sudo systemd-run
        --pipe …``), POLKIT → the unprivileged ``pipe_cmd`` (``systemd-run
        --pipe …``). Everything else — the crossed ``dispatch fwd <wire>``
        payload and the entire surrounding ssh argv (incl. the ``-F
        /dev/null`` hardening pins) — is identical. This pins the divergence
        to exactly the ``sudo`` token so a future change that leaks any other
        per-mode difference into the attach argv fails the gate.
        """
        sudo_lines = FIXTURE_SUDO.read_text().splitlines()
        polkit_lines = FIXTURE_POLKIT.read_text().splitlines()
        assert len(sudo_lines) == len(polkit_lines), (
            "argv_sudo.txt and argv_polkit.txt have different element counts — "
            "the only sanctioned per-mode difference is the ProxyCommand's "
            "``sudo`` prefix, which does not change the element count."
        )
        diffs = [(s, p) for s, p in zip(sudo_lines, polkit_lines, strict=True) if s != p]
        assert len(diffs) == 1, (
            f"expected exactly one differing argv element (the ProxyCommand); got {diffs!r}"
        )
        sudo_proxy, polkit_proxy = diffs[0]
        assert sudo_proxy.startswith("ProxyCommand=")
        assert polkit_proxy.startswith("ProxyCommand=")
        # The SUDO ProxyCommand value is the POLKIT one with a leading ``sudo``.
        sudo_val = sudo_proxy[len("ProxyCommand=") :]
        polkit_val = polkit_proxy[len("ProxyCommand=") :]
        assert sudo_val == "sudo " + polkit_val


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

    def test_argv_polkit_mode_proxy_command_has_no_sudo_prefix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # POLKIT separate-user attach crosses via the unprivileged ``pipe_cmd``
        # (polkit ``manage-units``); no ``sudo`` prefix on the ProxyCommand.
        from core.host_config import pipe_cmd

        argv = _capture_argv(monkeypatch, tmp_path, "polkit")
        proxy = next(a for a in argv if a.startswith("ProxyCommand="))
        proxy_value = proxy[len("ProxyCommand=") :]
        proxy_tokens = shlex.split(proxy_value)
        unpriv = pipe_cmd(_STABLE_SBUSER)
        assert proxy_tokens[: len(unpriv)] == unpriv
        assert proxy_tokens[0] != "sudo"

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
