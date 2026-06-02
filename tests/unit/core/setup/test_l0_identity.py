"""Unit tests for ``core.setup.l0_identity`` (Group 5, task 5.4).

Covers operator resolution precedence, the three-tier distro classification +
canonical texts, the binary check, the sudo-version floor WARN, and the
``resolve_machinectl_path`` uniqueness assertion (exactly-one / zero / >1 /
sole-non-canonical), plus the phase probe/act/reverify branches and the
content-aware-probe contract via the conftest fixture.
"""

from __future__ import annotations

import pytest
from core.host_config import DockerExecutionMode, MachinectlAuth, minimal_host_config
from core.setup import l0_identity
from core.setup.l0_identity import (
    PHASE,
    MachinectlResolutionError,
    OperatorResolutionError,
    classify_distro,
    missing_binaries,
    resolve_machinectl_path,
    resolve_operator,
    sudo_floor_warning,
    unsupported_distro_refusal,
    untested_distro_warning,
)
from core.setup.phase_runner import Identity, PhaseResult, SetupContext


def _ctx(operator: str = "alice") -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config("sandboxuser", MachinectlAuth.SUDO),
        operator=operator,
    )


def _oprootless_ctx(operator: str = "alice") -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config(
            "sandboxuser", MachinectlAuth.SUDO, DockerExecutionMode.OPERATOR_ROOTLESS
        ),
        operator=operator,
    )


class _Pw:
    def __init__(self, name: str, uid: int) -> None:
        self.pw_name = name
        self.pw_uid = uid


# ── PHASE wiring ─────────────────────────────────────────────────────────────


def test_phase_identity_and_deps() -> None:
    assert PHASE.id == "l0"
    assert PHASE.depends_on == ()
    assert PHASE.identity == Identity.ROOT


# ── operator resolution ──────────────────────────────────────────────────────


def test_resolve_operator_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pwd.getpwnam", lambda n: _Pw(n, 1000))
    assert resolve_operator("bob") == "bob"


def test_resolve_operator_flag_unknown_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_n: str) -> _Pw:
        raise KeyError(_n)

    monkeypatch.setattr("pwd.getpwnam", _boom)
    with pytest.raises(OperatorResolutionError, match="does not match"):
        resolve_operator("ghost")


def test_resolve_operator_sudo_user_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.delenv("PKEXEC_UID", raising=False)
    monkeypatch.setattr("pwd.getpwnam", lambda n: _Pw(n, 1000))
    assert resolve_operator() == "alice"


def test_resolve_operator_sudo_user_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("SUDO_UID", "1000")

    def _boom(_n: str) -> _Pw:
        raise KeyError(_n)

    monkeypatch.setattr("pwd.getpwnam", _boom)
    with pytest.raises(OperatorResolutionError, match="SUDO_USER"):
        resolve_operator()


def test_resolve_operator_sudo_uid_inconsistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("SUDO_UID", "999")
    monkeypatch.setattr("pwd.getpwnam", lambda n: _Pw(n, 1000))
    with pytest.raises(OperatorResolutionError, match="inconsistent"):
        resolve_operator()


def test_resolve_operator_pkexec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.setenv("PKEXEC_UID", "1001")
    monkeypatch.setattr("pwd.getpwuid", lambda u: _Pw("carol", u))
    assert resolve_operator() == "carol"


def test_resolve_operator_pkexec_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.setenv("PKEXEC_UID", "4242")

    def _boom(_u: int) -> _Pw:
        raise KeyError(_u)

    monkeypatch.setattr("pwd.getpwuid", _boom)
    with pytest.raises(OperatorResolutionError, match="PKEXEC_UID"):
        resolve_operator()


def test_resolve_operator_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("PKEXEC_UID", raising=False)
    with pytest.raises(OperatorResolutionError, match="cannot resolve operator"):
        resolve_operator()


# ── distro classification + canonical texts ──────────────────────────────────


def _fake_os_release(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    monkeypatch.setattr(
        "core.setup.l0_identity.Path.read_text",
        lambda self: content,
    )


def test_classify_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_os_release(monkeypatch, 'ID=ubuntu\nVERSION_ID="24.04"\n')
    assert classify_distro() == ("validated", "ubuntu", "24.04")


def test_classify_untested(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_os_release(monkeypatch, 'ID=fedora\nVERSION_ID="40"\n')
    assert classify_distro() == ("untested", "fedora", "40")


def test_classify_unrecognized(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_os_release(monkeypatch, 'ID=alpine\nVERSION_ID="3.20"\n')
    assert classify_distro() == ("unrecognized", "alpine", "3.20")


def test_classify_os_release_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_self: object) -> str:
        raise FileNotFoundError

    monkeypatch.setattr("core.setup.l0_identity.Path.read_text", _boom)
    assert classify_distro() == ("unrecognized", "unknown", "")


def test_untested_warning_text_with_prompt() -> None:
    text = untested_distro_warning("fedora", "40", with_prompt=True)
    assert text.startswith("⚠ Untested distro")
    assert "Detected: fedora 40" in text
    assert text.endswith("Press Enter to continue, Ctrl-C to abort.")


def test_untested_warning_text_no_prompt() -> None:
    text = untested_distro_warning("fedora", "40", with_prompt=False)
    assert "Press Enter to continue" not in text


def test_unsupported_refusal_text() -> None:
    text = unsupported_distro_refusal("alpine", "3.20")
    assert text.startswith("✗ Unsupported distro")
    assert "Debian, Ubuntu, Fedora, RHEL" in text


def test_emit_distro_gate_validated_noop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_os_release(monkeypatch, "ID=debian\n")
    l0_identity.emit_distro_gate(is_tty=True, assume_yes=False)
    assert capsys.readouterr().err == ""


def test_emit_distro_gate_unrecognized_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_os_release(monkeypatch, "ID=void\n")
    with pytest.raises(SystemExit):
        l0_identity.emit_distro_gate(is_tty=False, assume_yes=True)


def test_emit_distro_gate_untested_tty_prompts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_os_release(monkeypatch, "ID=arch\n")
    monkeypatch.setattr("builtins.input", lambda: "")
    l0_identity.emit_distro_gate(is_tty=True, assume_yes=False)
    assert "Press Enter to continue" in capsys.readouterr().err


def test_emit_distro_gate_untested_yes_no_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_os_release(monkeypatch, "ID=arch\n")
    l0_identity.emit_distro_gate(is_tty=True, assume_yes=True)
    err = capsys.readouterr().err
    assert "Untested distro" in err
    assert "Press Enter to continue" not in err


# ── binary check ─────────────────────────────────────────────────────────────


def _stub_secure_path(monkeypatch: pytest.MonkeyPatch, dirs: list[str]) -> None:
    monkeypatch.setattr(
        "core.setup.l0_identity._secure_path_dirs", lambda: dirs
    )


def test_missing_binaries_all_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_secure_path(monkeypatch, ["/usr/bin"])
    monkeypatch.setattr("os.access", lambda p, m: True)
    assert missing_binaries() == []


def test_missing_binaries_some_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_secure_path(monkeypatch, ["/usr/bin"])
    monkeypatch.setattr(
        "os.access", lambda p, m: not p.endswith("tlog-rec")
    )
    assert missing_binaries() == ["tlog-rec"]


def test_binary_install_cmd_arch_tlog_aur() -> None:
    assert l0_identity._binary_install_cmd("tlog-rec", "arch") == "paru -S tlog"


def test_binary_install_cmd_debian_tlog_source_build() -> None:
    """tlog on debian-family: NOT a bare ``apt install tlog`` (wrong on trixie).

    Round-5 Debian-trixie: ``sudo apt install tlog`` fails (not packaged on
    13+). The hint must surface the source build for trixie while still naming
    the apt path for Ubuntu / Debian <=12 (one ``debian`` family covers both).
    """
    cmd = l0_identity._binary_install_cmd("tlog-rec", "debian")
    assert "github.com/Scribery/tlog" in cmd
    assert "trixie" in cmd
    # The other debian binaries still get the plain apt hint.
    assert l0_identity._binary_install_cmd("rsync", "debian") == "sudo apt install rsync"


def test_binary_install_cmd_acl_package() -> None:
    # setfacl maps to the 'acl' package via the override table.
    cmd = l0_identity._binary_install_cmd("setfacl", "debian")
    assert "acl" in cmd


# ── sudo-version floor ───────────────────────────────────────────────────────


def _stub_sudo_version(monkeypatch: pytest.MonkeyPatch, out: str) -> None:
    class _Proc:
        stdout = out

    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: _Proc()
    )


def test_sudo_floor_warning_below(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_sudo_version(monkeypatch, "Sudo version 1.8.27\n")
    warn = sudo_floor_warning()
    assert warn is not None
    assert "1.8.27" in warn
    assert "1.9.5p2" in warn


def test_sudo_floor_warning_at_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_sudo_version(monkeypatch, "Sudo version 1.9.5p2\n")
    assert sudo_floor_warning() is None


def test_sudo_floor_warning_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_sudo_version(monkeypatch, "no version here\n")
    assert sudo_floor_warning() is None


def test_parse_sudo_version_subprocess_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("no sudo")

    monkeypatch.setattr("subprocess.run", _boom)
    assert l0_identity.parse_sudo_version() is None


# ── machinectl path resolution + uniqueness (the three branches) ─────────────


class _Stat:
    """Minimal ``os.stat_result`` stand-in carrying only the identity keys."""

    def __init__(self, st_dev: int, st_ino: int) -> None:
        self.st_dev = st_dev
        self.st_ino = st_ino


def _stub_stat(
    monkeypatch: pytest.MonkeyPatch, ident: dict[str, tuple[int, int]]
) -> None:
    """Stub ``os.stat`` to return per-path ``(st_dev, st_ino)`` identities.

    A path absent from ``ident`` raises ``OSError`` (mirrors a real
    unstattable path → ``_file_identity`` returns ``None`` → keyed on the
    path string).
    """

    def _stat(path: str, *_a: object, **_k: object) -> _Stat:
        try:
            dev, ino = ident[path]
        except KeyError as exc:
            raise OSError(f"no such stub stat for {path!r}") from exc
        return _Stat(dev, ino)

    monkeypatch.setattr("os.stat", _stat)


def test_resolve_machinectl_exactly_one_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_secure_path(monkeypatch, ["/usr/local/bin", "/usr/bin"])
    monkeypatch.setattr(
        "os.access", lambda p, m: p == "/usr/bin/machinectl"
    )
    _stub_stat(monkeypatch, {"/usr/bin/machinectl": (1, 100)})
    assert (
        resolve_machinectl_path(_ctx().host_config) == "/usr/bin/machinectl"
    )


def test_resolve_machinectl_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_secure_path(monkeypatch, ["/usr/bin", "/sbin"])
    monkeypatch.setattr("os.access", lambda p, m: False)
    with pytest.raises(MachinectlResolutionError, match="no executable"):
        resolve_machinectl_path(_ctx().host_config)


def test_resolve_machinectl_more_than_one_distinct_inode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two GENUINELY DISTINCT binaries (different inodes) → still refused.

    This is the F-005/V9e anti-shadow property: an attacker shadow at a
    non-canonical dir is a different ``(st_dev, st_ino)`` and MUST keep
    triggering the uniqueness refusal even after the usrmerge dedupe lands.
    """
    _stub_secure_path(monkeypatch, ["/usr/local/bin", "/usr/bin"])
    monkeypatch.setattr("os.access", lambda p, m: True)
    _stub_stat(
        monkeypatch,
        {
            "/usr/local/bin/machinectl": (1, 4242),  # attacker shadow inode
            "/usr/bin/machinectl": (1, 100),  # the genuine systemd binary
        },
    )
    with pytest.raises(
        MachinectlResolutionError,
        match="genuinely distinct binaries",
    ):
        resolve_machinectl_path(_ctx().host_config)


def test_resolve_machinectl_usrmerge_same_inode_no_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """usrmerged host: 4 secure_path aliases of ONE inode → canonical, no raise.

    ``/usr/sbin`` / ``/usr/bin`` / ``/sbin`` / ``/bin`` are symlinks to one
    dir, so all four ``machinectl`` paths ``os.stat`` to the SAME
    ``(st_dev, st_ino)``. The dedupe collapses them to a single binary and
    resolves to the canonical ``/usr/bin/machinectl`` the L3 renderer expects.
    """
    _stub_secure_path(
        monkeypatch, ["/usr/sbin", "/usr/bin", "/sbin", "/bin"]
    )
    monkeypatch.setattr("os.access", lambda p, m: True)
    one_inode = (66, 94592)
    _stub_stat(
        monkeypatch,
        {
            "/usr/sbin/machinectl": one_inode,
            "/usr/bin/machinectl": one_inode,
            "/sbin/machinectl": one_inode,
            "/bin/machinectl": one_inode,
        },
    )
    assert (
        resolve_machinectl_path(_ctx().host_config) == "/usr/bin/machinectl"
    )


def test_resolve_machinectl_usrmerge_canonical_only_sbin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-inode aliases where the only canonical alias is ``/usr/sbin``.

    No ``/usr/bin`` alias on secure_path → fall back deterministically to the
    sole canonical alias (``/usr/sbin/machinectl``), still no refusal.
    """
    _stub_secure_path(monkeypatch, ["/usr/sbin", "/sbin"])
    monkeypatch.setattr("os.access", lambda p, m: True)
    one_inode = (66, 94592)
    _stub_stat(
        monkeypatch,
        {
            "/usr/sbin/machinectl": one_inode,
            "/sbin/machinectl": one_inode,
        },
    )
    assert (
        resolve_machinectl_path(_ctx().host_config)
        == "/usr/sbin/machinectl"
    )


def test_resolve_machinectl_unstattable_paths_keyed_distinctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two paths that both fail ``os.stat`` key on their own strings.

    A path whose ``os.stat`` raises is NEVER silently merged into another
    binary's identity group — it keys on its own path string, so two
    unstattable copies are still two distinct binaries → refused.
    """
    _stub_secure_path(monkeypatch, ["/usr/local/bin", "/usr/bin"])
    monkeypatch.setattr("os.access", lambda p, m: True)
    _stub_stat(monkeypatch, {})  # every os.stat raises OSError
    with pytest.raises(
        MachinectlResolutionError,
        match="genuinely distinct binaries",
    ):
        resolve_machinectl_path(_ctx().host_config)


def test_resolve_machinectl_sole_non_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_secure_path(monkeypatch, ["/usr/local/bin", "/usr/sbin"])
    monkeypatch.setattr(
        "os.access", lambda p, m: p == "/usr/local/bin/machinectl"
    )
    _stub_stat(monkeypatch, {"/usr/local/bin/machinectl": (1, 100)})
    with pytest.raises(
        MachinectlResolutionError,
        match="only outside a canonical",
    ):
        resolve_machinectl_path(_ctx().host_config)


# ── _secure_path_dirs parsing branches ───────────────────────────────────────


def test_secure_path_from_sudo_v(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        stdout = 'Defaults secure_path = "/a:/b"\n'

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())
    assert l0_identity._secure_path_dirs() == ["/a", "/b"]


def test_secure_path_from_sudoers_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Proc:
        stdout = "nothing useful\n"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())
    monkeypatch.setattr(
        "core.setup.l0_identity.Path.read_text",
        lambda self: 'Defaults    secure_path="/x:/y"\n',
    )
    assert l0_identity._secure_path_dirs() == ["/x", "/y"]


def test_secure_path_fallback_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise OSError

    monkeypatch.setattr("subprocess.run", _boom)

    def _boom_read(_self: object) -> str:
        raise FileNotFoundError

    monkeypatch.setattr(
        "core.setup.l0_identity.Path.read_text", _boom_read
    )
    dirs = l0_identity._secure_path_dirs()
    assert dirs[0] == "/usr/local/sbin"
    assert "/usr/bin" in dirs


def test_secure_path_sudoers_no_secure_path_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Proc:
        stdout = "nothing\n"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())
    monkeypatch.setattr(
        "core.setup.l0_identity.Path.read_text",
        lambda self: "Defaults env_reset\n# a comment\n",
    )
    assert l0_identity._secure_path_dirs()[0] == "/usr/local/sbin"


# ── phase probe / act / reverify branches ────────────────────────────────────


def _ok_world(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pwd.getpwnam", lambda n: _Pw(n, 1000))
    _fake_os_release(monkeypatch, "ID=ubuntu\n")
    _stub_secure_path(monkeypatch, ["/usr/bin"])
    monkeypatch.setattr("os.access", lambda p, m: True)
    monkeypatch.setattr(
        "core.setup.l0_identity.parse_sudo_version", lambda: (1, 9, 17, 2)
    )


def test_probe_already_correct(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_world(monkeypatch)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.ALREADY_CORRECT
    assert "operator=alice" in detail


def test_probe_untested_distro_warn_in_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ok_world(monkeypatch)
    _fake_os_release(monkeypatch, "ID=fedora\n")
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.ALREADY_CORRECT
    assert "untested distro fedora" in detail


def test_probe_sub_floor_warn_in_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ok_world(monkeypatch)
    monkeypatch.setattr(
        "core.setup.l0_identity.parse_sudo_version", lambda: (1, 8, 27, 0)
    )
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.ALREADY_CORRECT
    assert "predates the validated floor" in detail


def test_probe_uses_ctx_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    # L0's probe reads the already-resolved operator from the context — it
    # does NOT re-resolve it (the env/flag resolution lives in
    # ``resolve_operator``, exercised separately above).
    _ok_world(monkeypatch)
    result, detail = PHASE.probe(_ctx(operator="zoe"))
    assert result == PhaseResult.ALREADY_CORRECT
    assert "operator=zoe" in detail


def test_probe_conflict_unrecognized_distro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pwd.getpwnam", lambda n: _Pw(n, 1000))
    _fake_os_release(monkeypatch, "ID=gentoo\n")
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.CONFLICT
    assert "Unsupported distro" in detail


def test_probe_conflict_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing required binary is a CONFLICT (refuse), not a DRIFT.

    Round-5 Debian: missing tlog-rec showed ⊙ (DRIFT "will mutate") in the plan
    but the apply hard-FAILed via act-raise — a plan/apply contradiction. L0
    cannot install packages, so a missing prereq is an unconvergeable refusal:
    CONFLICT in BOTH passes (Pattern A — refuse early with an actionable hint).
    """
    monkeypatch.setattr("pwd.getpwnam", lambda n: _Pw(n, 1000))
    _fake_os_release(monkeypatch, "ID=ubuntu\n")
    _stub_secure_path(monkeypatch, ["/usr/bin"])
    monkeypatch.setattr("os.access", lambda p, m: not p.endswith("rsync"))
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.CONFLICT
    assert "rsync" in detail


def test_probe_conflict_machinectl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pwd.getpwnam", lambda n: _Pw(n, 1000))
    _fake_os_release(monkeypatch, "ID=ubuntu\n")
    _stub_secure_path(monkeypatch, ["/usr/bin"])
    monkeypatch.setattr(
        "os.access", lambda p, m: not p.endswith("machinectl")
    )
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.CONFLICT
    assert "no executable 'machinectl'" in detail


def test_act_raises_on_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_os_release(monkeypatch, "ID=ubuntu\n")
    _stub_secure_path(monkeypatch, ["/usr/bin"])
    monkeypatch.setattr("os.access", lambda p, m: not p.endswith("sysctl"))
    with pytest.raises(RuntimeError, match="required binaries missing"):
        PHASE.act(_ctx())


def test_act_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_world(monkeypatch)
    assert PHASE.act(_ctx()) == "L0 prerequisites satisfied"


def test_reverify_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_world(monkeypatch)
    assert PHASE.reverify(_ctx()) is True


def test_reverify_false_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_secure_path(monkeypatch, ["/usr/bin"])
    monkeypatch.setattr("os.access", lambda p, m: not p.endswith("chattr"))
    assert PHASE.reverify(_ctx()) is False


def test_reverify_false_machinectl_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_secure_path(monkeypatch, ["/usr/bin"])
    monkeypatch.setattr(
        "os.access", lambda p, m: not p.endswith("machinectl")
    )
    assert PHASE.reverify(_ctx()) is False


# ── operator-rootless: machinectl-path assertion is gated out (§5.1) ──────────


def test_probe_oprootless_skips_machinectl_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """op-rootless L0 probe must NOT run the machinectl uniqueness assertion.

    With ``machinectl`` ABSENT from secure_path — a CONFLICT in separate-user
    (see ``test_probe_conflict_machinectl``) — operator-rootless still converges
    ``ALREADY_CORRECT`` and the detail carries no ``machinectl=`` field, because
    there is no machinectl crossing in that mode (D2).
    """
    _ok_world(monkeypatch)
    monkeypatch.setattr("os.access", lambda p, m: not p.endswith("machinectl"))
    result, detail = PHASE.probe(_oprootless_ctx())
    assert result == PhaseResult.ALREADY_CORRECT
    assert "machinectl=" not in detail
    assert "operator=alice" in detail


def test_reverify_oprootless_skips_machinectl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """op-rootless reverify mirrors the probe: machinectl unresolvable is moot.

    An unresolvable machinectl makes ``_reverify`` False in separate-user (see
    ``test_reverify_false_machinectl_unresolvable``); in operator-rootless it is
    True so long as the required binaries are present.
    """
    _ok_world(monkeypatch)
    monkeypatch.setattr("os.access", lambda p, m: not p.endswith("machinectl"))
    assert PHASE.reverify(_oprootless_ctx()) is True


# ── content-aware probe contract (conftest fixture) ──────────────────────────


# NOTE: L0 has NO content-aware-DRIFT test (design D10 is for phases that own
# mutable state and re-converge drift). L0 mutates nothing — it is a
# verify/refuse phase: a missing prerequisite is a CONFLICT (see
# test_probe_conflict_missing_binary), never a convergeable DRIFT. So the
# assert_phase_content_aware fixture (which asserts stale→DRIFT) does not apply.
