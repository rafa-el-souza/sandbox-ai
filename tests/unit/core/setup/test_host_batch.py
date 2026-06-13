# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the operator-rootless host-root batch model (§8-A / D5a)."""

from __future__ import annotations

import grp
import os
import pwd
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from core.host_config import (
    DockerExecutionMode,
    MachinectlAuth,
    minimal_host_config,
)
from core.setup import host_batch, l1_kernel, l2a_delegate
from core.setup.host_batch import (
    HOST_ROOT_BATCH,
    BatchItem,
    BatchParams,
    apply_host_root_batch,
    build_bootstrap_argv,
    classify_host_root_batch,
    render_remediation_block,
)
from core.setup.phase_runner import SetupContext
from core.setup_state import read_mode


def _params(
    *,
    operator: str = "alice",
    operator_uid: int = 1000,
    bridge_group: str = "sb-ws",
    bridge_gid: int = 100500,
    distro_family: str = "debian",
    mode: DockerExecutionMode = DockerExecutionMode.OPERATOR_ROOTLESS,
) -> BatchParams:
    return BatchParams(
        operator=operator,
        operator_uid=operator_uid,
        bridge_group=bridge_group,
        bridge_gid=bridge_gid,
        distro_family=distro_family,
        mode=mode,
    )


def _ctx(operator: str = "alice") -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config(
            "sandboxuser",
            MachinectlAuth.SUDO,
            DockerExecutionMode.OPERATOR_ROOTLESS,
        ),
        operator=operator,
    )


# --------------------------------------------------------------------------- #
# Ordering meta-test
# --------------------------------------------------------------------------- #


def test_marker_is_last() -> None:
    assert HOST_ROOT_BATCH[-1] is BatchItem.MARKER


def test_subid_before_groupadd() -> None:
    assert HOST_ROOT_BATCH.index(BatchItem.SUBID) < HOST_ROOT_BATCH.index(BatchItem.GROUPADD)


def test_batch_is_the_full_enumeration() -> None:
    assert set(HOST_ROOT_BATCH) == set(BatchItem)
    assert len(HOST_ROOT_BATCH) == len(BatchItem)


def test_canonical_order_exact() -> None:
    assert HOST_ROOT_BATCH == (
        BatchItem.SUBID,
        BatchItem.GROUPADD,
        BatchItem.SYSCTL,
        BatchItem.NFTABLES,
        BatchItem.DELEGATE,
        BatchItem.LINGER,
        BatchItem.RUNSC,
        BatchItem.MARKER,
    )


# --------------------------------------------------------------------------- #
# Marker-last crash-safety (THE load-bearing test)
# --------------------------------------------------------------------------- #


def test_mid_batch_failure_never_writes_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "setup-state.json"
    monkeypatch.setattr("core.setup_state.MARKER_PATH", marker)

    write_calls: list[tuple[str, DockerExecutionMode]] = []

    def _spy_write_mode(operator: str, mode: DockerExecutionMode) -> None:
        write_calls.append((operator, mode))

    # Spy the root-owned marker write that the real ``_apply_marker`` delegates to.
    monkeypatch.setattr(host_batch, "write_mode_root_owned", _spy_write_mode)

    # Make every applier a no-op EXCEPT the real MARKER applier (kept real so the
    # spy would fire iff it were reached) and DELEGATE, which raises mid-batch.
    for item in HOST_ROOT_BATCH:
        if item is not BatchItem.MARKER:
            monkeypatch.setitem(host_batch._APPLIERS, item, lambda _p: None)

    def _boom(_p: BatchParams) -> None:
        raise RuntimeError("delegate failed")

    monkeypatch.setitem(host_batch._APPLIERS, BatchItem.DELEGATE, _boom)

    with pytest.raises(RuntimeError, match="delegate failed"):
        apply_host_root_batch(frozenset(BatchItem), _params())

    assert write_calls == []
    assert not marker.exists()


# --------------------------------------------------------------------------- #
# Apply order + only-requested
# --------------------------------------------------------------------------- #


def _recorder(item: BatchItem, sink: list[BatchItem]) -> Callable[[BatchParams], None]:
    def _apply(_p: BatchParams) -> None:
        sink.append(item)

    return _apply


def test_apply_walks_canonical_order(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[BatchItem] = []
    for item in HOST_ROOT_BATCH:
        monkeypatch.setitem(host_batch._APPLIERS, item, _recorder(item, recorded))

    apply_host_root_batch(frozenset(BatchItem), _params())

    assert recorded == list(HOST_ROOT_BATCH)


def test_apply_runs_only_requested_items_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[BatchItem] = []
    for item in HOST_ROOT_BATCH:
        monkeypatch.setitem(host_batch._APPLIERS, item, _recorder(item, recorded))

    # Request a subset, deliberately given in non-canonical order.
    requested = frozenset({BatchItem.MARKER, BatchItem.SUBID, BatchItem.SYSCTL})
    apply_host_root_batch(requested, _params())

    assert recorded == [BatchItem.SUBID, BatchItem.SYSCTL, BatchItem.MARKER]


# --------------------------------------------------------------------------- #
# Each applier reuses the right helper
# --------------------------------------------------------------------------- #


def test_subid_applier_appends_operator_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_batch, "parse_subuid_for_user", lambda _u: [])
    monkeypatch.setattr(host_batch, "parse_subgid_for_user", lambda _u: [])
    runs: list[list[str]] = []
    monkeypatch.setattr(host_batch, "_run", lambda argv: runs.append(argv))

    host_batch._apply_subid(_params(operator="bob"))

    assert runs == [
        ["usermod", "--add-subuids", "100000-165535", "bob"],
        ["usermod", "--add-subgids", "100000-165535", "bob"],
    ]


def test_subid_applier_is_append_only_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_batch, "parse_subuid_for_user", lambda _u: [(100000, 65536)])
    monkeypatch.setattr(host_batch, "parse_subgid_for_user", lambda _u: [(100000, 65536)])
    runs: list[list[str]] = []
    monkeypatch.setattr(host_batch, "_run", lambda argv: runs.append(argv))

    host_batch._apply_subid(_params())

    assert runs == []


def test_groupadd_applier_creates_group_and_adds_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_group(_name: str) -> object:
        raise KeyError(_name)

    monkeypatch.setattr(grp, "getgrnam", _no_group)
    monkeypatch.setattr(host_batch, "_operator_in_group", lambda _o, _g: False)
    runs: list[list[str]] = []
    monkeypatch.setattr(host_batch, "_run", lambda argv: runs.append(argv))

    host_batch._apply_groupadd(_params(operator="carol", bridge_group="sb-ws", bridge_gid=100500))

    assert runs == [
        ["groupadd", "-g", "100500", "sb-ws"],
        ["usermod", "-aG", "sb-ws", "carol"],
    ]


def test_groupadd_applier_idempotent_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grp, "getgrnam", lambda _n: object())
    monkeypatch.setattr(host_batch, "_operator_in_group", lambda _o, _g: True)
    runs: list[list[str]] = []
    monkeypatch.setattr(host_batch, "_run", lambda argv: runs.append(argv))

    host_batch._apply_groupadd(_params())

    assert runs == []


def test_sysctl_applier_reuses_l1_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[tuple[Path, str, int]] = []
    monkeypatch.setattr(
        l1_kernel,
        "write_root_file",
        lambda path, body, mode: written.append((path, body, mode)),
    )
    monkeypatch.setattr(l1_kernel, "render_sysctl_dropin", lambda: "BODY\n")
    monkeypatch.setattr(l1_kernel, "is_debian_family", lambda: True)
    runs: list[list[str]] = []
    monkeypatch.setattr(host_batch, "_run", lambda argv: runs.append(argv))

    host_batch._apply_sysctl(_params())

    assert written == [(l1_kernel.SYSCTL_DROPIN, "BODY\n", 0o644)]
    assert runs == [
        ["sysctl", "-w", f"user.max_user_namespaces={l1_kernel.MAX_USER_NS}"],
        ["sysctl", "-w", "kernel.unprivileged_userns_clone=1"],
    ]


def test_sysctl_applier_skips_userns_clone_off_debian(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(l1_kernel, "write_root_file", lambda *_a: None)
    monkeypatch.setattr(l1_kernel, "render_sysctl_dropin", lambda: "BODY\n")
    monkeypatch.setattr(l1_kernel, "is_debian_family", lambda: False)
    runs: list[list[str]] = []
    monkeypatch.setattr(host_batch, "_run", lambda argv: runs.append(argv))

    host_batch._apply_sysctl(_params())

    assert runs == [["sysctl", "-w", f"user.max_user_namespaces={l1_kernel.MAX_USER_NS}"]]


@pytest.mark.parametrize(
    ("family", "expected"),
    [
        ("fedora", ("ip_tables",)),
        ("arch", ("nf_tables", "ip_tables")),
        ("debian", ("nf_tables",)),
        ("", ("nf_tables",)),
    ],
)
def test_nftables_module_set_per_distro(family: str, expected: tuple[str, ...]) -> None:
    assert host_batch._nftables_modules(family) == expected


def test_nftables_applier_loads_and_persists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    persist = tmp_path / "modules-load.conf"
    monkeypatch.setattr(host_batch, "_MODULES_LOAD_DROPIN", persist)
    monkeypatch.setattr(host_batch, "_module_loaded", lambda _m: False)
    runs: list[list[str]] = []
    monkeypatch.setattr(host_batch, "_run", lambda argv: runs.append(argv))
    written: list[tuple[Path, str, int]] = []
    monkeypatch.setattr(
        l1_kernel,
        "write_root_file",
        lambda path, body, mode: written.append((path, body, mode)),
    )

    host_batch._apply_nftables(_params(distro_family="arch"))

    assert runs == [["modprobe", "nf_tables"], ["modprobe", "ip_tables"]]
    assert written[0][0] == persist
    assert "nf_tables" in written[0][1]
    assert "ip_tables" in written[0][1]


def test_nftables_applier_skips_loaded_and_matching_persist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    persist = tmp_path / "modules-load.conf"
    persist.write_text(host_batch._render_modules_load_dropin("debian"))
    monkeypatch.setattr(host_batch, "_MODULES_LOAD_DROPIN", persist)
    monkeypatch.setattr(host_batch, "_module_loaded", lambda _m: True)
    runs: list[list[str]] = []
    monkeypatch.setattr(host_batch, "_run", lambda argv: runs.append(argv))
    monkeypatch.setattr(
        l1_kernel,
        "write_root_file",
        lambda *_a: pytest.fail("persist file should not be rewritten"),
    )

    host_batch._apply_nftables(_params(distro_family="debian"))

    assert runs == []


def test_delegate_applier_reuses_l2a(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "delegate.conf"
    monkeypatch.setattr(
        l2a_delegate,
        "delegate_dropin_path_for_uid",
        lambda uid: target if uid == 1234 else pytest.fail("wrong uid"),
    )
    monkeypatch.setattr(l2a_delegate, "render_delegate_dropin", lambda: "DELEGATE\n")
    written: list[tuple[Path, str, int]] = []
    monkeypatch.setattr(
        l2a_delegate,
        "write_root_file",
        lambda path, body, mode: written.append((path, body, mode)),
    )
    runs: list[list[str]] = []
    monkeypatch.setattr(host_batch, "_run", lambda argv: runs.append(argv))

    host_batch._apply_delegate(_params(operator_uid=1234))

    assert written == [(target, "DELEGATE\n", 0o644)]
    assert runs == [["systemctl", "daemon-reload"]]


def test_linger_applier_enables_linger_for_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[list[str]] = []
    monkeypatch.setattr(host_batch, "_run", lambda argv: runs.append(argv))

    host_batch._apply_linger(_params(operator="alice"))

    assert runs == [["loginctl", "enable-linger", "alice"]]


def test_runsc_applier_installs_with_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """The batch is the op-rootless runsc lifecycle (install AND pin-update), so it
    installs with ``force=True`` — unsealing the immutable target on a drift
    re-install (``force=False`` would EPERM-fail the rename over a sealed binary
    and abort the batch). Inert on a fresh install (no target to unseal)."""
    calls: list[tuple[str, bool]] = []

    def _spy_install(name: str, _host_config: object, *, force: bool) -> None:
        calls.append((name, force))

    monkeypatch.setattr(host_batch, "install_pinned", _spy_install)

    host_batch._apply_runsc(_params())

    assert calls == [("runsc", True)]


def test_marker_applier_writes_mode_and_root_owns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "setup-state.json"
    monkeypatch.setattr("core.setup_state.MARKER_PATH", marker)
    chmods: list[tuple[Path, int]] = []
    chowns: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(os, "chmod", lambda p, m: chmods.append((p, m)))
    monkeypatch.setattr(os, "chown", lambda p, u, g: chowns.append((p, u, g)))

    host_batch._apply_marker(_params(operator="dave"))

    assert read_mode("dave") is DockerExecutionMode.OPERATOR_ROOTLESS
    assert (marker, 0o644) in chmods
    assert (marker, 0, 0) in chowns


def test_marker_is_only_write_mode_caller() -> None:
    # Structural assertion: MARKER is wired to _apply_marker, the single writer.
    assert host_batch._APPLIERS[BatchItem.MARKER] is host_batch._apply_marker


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #


def _all_satisfied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_batch, "_subid_satisfied", lambda _o: True)
    monkeypatch.setattr(host_batch, "_group_satisfied", lambda _o, _g: True)
    monkeypatch.setattr(host_batch, "_sysctl_satisfied", lambda: True)
    monkeypatch.setattr(host_batch, "_nftables_satisfied", lambda _f: True)
    monkeypatch.setattr(host_batch, "_delegation_present", lambda _u: True)
    monkeypatch.setattr(host_batch, "_linger_satisfied", lambda _o: True)
    monkeypatch.setattr(host_batch, "_runsc_satisfied", lambda: True)
    monkeypatch.setattr(host_batch, "read_mode", lambda _o: DockerExecutionMode.OPERATOR_ROOTLESS)
    monkeypatch.setattr(
        host_batch, "autodetect_workspace_bridge_gid_recommendation", lambda _o: 100500
    )
    monkeypatch.setattr(host_batch, "detect_distro", lambda: "debian")
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda _n: pwd.struct_passwd(("alice", "x", 1000, 1000, "", "/home/alice", "/bin/bash")),
    )


def test_classifier_all_satisfied_excludes_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_satisfied(monkeypatch)

    items, params = classify_host_root_batch(_ctx())

    assert items == frozenset()
    assert params.operator == "alice"
    assert params.operator_uid == 1000
    assert params.bridge_group == "sb-ws"
    assert params.bridge_gid == 100500
    assert params.distro_family == "debian"
    assert params.mode is DockerExecutionMode.OPERATOR_ROOTLESS


@pytest.mark.parametrize(
    ("attr", "value", "expected_item"),
    [
        ("_subid_satisfied", lambda _o: False, BatchItem.SUBID),
        ("_group_satisfied", lambda _o, _g: False, BatchItem.GROUPADD),
        ("_sysctl_satisfied", lambda: False, BatchItem.SYSCTL),
        ("_nftables_satisfied", lambda _f: False, BatchItem.NFTABLES),
        ("_delegation_present", lambda _u: False, BatchItem.DELEGATE),
        ("_linger_satisfied", lambda _o: False, BatchItem.LINGER),
        ("_runsc_satisfied", lambda: False, BatchItem.RUNSC),
        ("read_mode", lambda _o: None, BatchItem.MARKER),
    ],
)
def test_classifier_includes_unsatisfied_item(
    monkeypatch: pytest.MonkeyPatch, attr: str, value: object, expected_item: BatchItem
) -> None:
    _all_satisfied(monkeypatch)
    monkeypatch.setattr(host_batch, attr, value)

    items, _ = classify_host_root_batch(_ctx())

    assert items == frozenset({expected_item})


def test_classifier_delegation_present_excludes_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_satisfied(monkeypatch)

    items, _ = classify_host_root_batch(_ctx())

    assert BatchItem.DELEGATE not in items


def test_classifier_marker_mismatch_included(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_satisfied(monkeypatch)
    monkeypatch.setattr(host_batch, "read_mode", lambda _o: DockerExecutionMode.SEPARATE_USER)

    items, _ = classify_host_root_batch(_ctx())

    assert BatchItem.MARKER in items


def test_classifier_empty_distro_family(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_satisfied(monkeypatch)
    monkeypatch.setattr(host_batch, "detect_distro", lambda: None)

    _, params = classify_host_root_batch(_ctx())

    assert params.distro_family == ""


# --------------------------------------------------------------------------- #
# Satisfaction helpers
# --------------------------------------------------------------------------- #


def test_subid_satisfied_requires_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_batch, "parse_subuid_for_user", lambda _u: [(1, 2)])
    monkeypatch.setattr(host_batch, "parse_subgid_for_user", lambda _u: [])
    assert host_batch._subid_satisfied("x") is False
    monkeypatch.setattr(host_batch, "parse_subgid_for_user", lambda _u: [(1, 2)])
    assert host_batch._subid_satisfied("x") is True


def test_group_satisfied_absent_group(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_group(_n: str) -> object:
        raise KeyError(_n)

    monkeypatch.setattr(grp, "getgrnam", _no_group)
    assert host_batch._group_satisfied("x", "g") is False


def test_group_satisfied_present_member(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grp, "getgrnam", lambda _n: object())
    monkeypatch.setattr(host_batch, "_operator_in_group", lambda _o, _g: True)
    assert host_batch._group_satisfied("x", "g") is True


def test_operator_in_group_supplementary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        grp,
        "getgrnam",
        lambda _n: grp.struct_group(("g", "x", 100500, ["alice"])),
    )
    assert host_batch._operator_in_group("alice", "g") is True


def test_operator_in_group_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        grp,
        "getgrnam",
        lambda _n: grp.struct_group(("g", "x", 100500, [])),
    )
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda _n: pwd.struct_passwd(("alice", "x", 1000, 100500, "", "/h", "/bin/bash")),
    )
    assert host_batch._operator_in_group("alice", "g") is True


def test_operator_in_group_absent_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        grp,
        "getgrnam",
        lambda _n: grp.struct_group(("g", "x", 100500, [])),
    )

    def _no_user(_n: str) -> object:
        raise KeyError(_n)

    monkeypatch.setattr(pwd, "getpwnam", _no_user)
    assert host_batch._operator_in_group("alice", "g") is False


def test_operator_in_group_missing_group(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_group(_n: str) -> object:
        raise KeyError(_n)

    monkeypatch.setattr(grp, "getgrnam", _no_group)
    assert host_batch._operator_in_group("alice", "g") is False


def test_module_loaded_true(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        stdout = "Module Size Used\nnf_tables 1 0\nip_tables 2 0\n"

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _Proc())
    assert host_batch._module_loaded("ip_tables") is True
    assert host_batch._module_loaded("missing") is False


def test_module_loaded_handles_blank_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        stdout = "Module Size Used\n\nnf_tables 1 0\n"

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _Proc())
    assert host_batch._module_loaded("nf_tables") is True


def test_module_loaded_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("no lsmod")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert host_batch._module_loaded("nf_tables") is False


def test_user_manager_controllers_reads_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    controllers = tmp_path / "cgroup.controllers"
    controllers.write_text("cpu io memory pids\n")
    monkeypatch.setattr(
        host_batch, "Path", lambda _p: controllers if "user@1000.service" in _p else Path(_p)
    )
    assert host_batch._user_manager_cgroup_controllers(1000) == {"cpu", "io", "memory", "pids"}


def test_user_manager_controllers_missing_file() -> None:
    assert host_batch._user_manager_cgroup_controllers(424242) == set()


def test_delegation_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        host_batch, "_user_manager_cgroup_controllers", lambda _u: {"cpu", "io", "memory", "pids"}
    )
    assert host_batch._delegation_present(1000) is True


def test_delegation_absent_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_batch, "_user_manager_cgroup_controllers", lambda _u: set())
    assert host_batch._delegation_present(1000) is False


def test_delegation_absent_when_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_batch, "_user_manager_cgroup_controllers", lambda _u: {"cpu", "io"})
    assert host_batch._delegation_present(1000) is False


def test_run_invokes_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

    host_batch._run(["echo", "hi"])

    assert calls[0][0] == (["echo", "hi"],)
    assert calls[0][1]["check"] is True


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert host_batch._read(tmp_path / "nope") is None


def test_runsc_satisfied(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Drift:
        status = "match"

    monkeypatch.setattr(host_batch, "detect_drift", lambda _n, _h: _Drift())
    assert host_batch._runsc_satisfied() is True


def test_linger_satisfied_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "Linger=yes\n", ""),
    )
    assert host_batch._linger_satisfied("alice") is True


def test_linger_satisfied_false_when_not_lingering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "not logged in"),
    )
    assert host_batch._linger_satisfied("alice") is False


def test_linger_satisfied_false_on_loginctl_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("loginctl not found")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert host_batch._linger_satisfied("alice") is False


def test_sysctl_satisfied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dropin = tmp_path / "sysctl.conf"
    dropin.write_text("BODY\n")
    monkeypatch.setattr(l1_kernel, "SYSCTL_DROPIN", dropin)
    monkeypatch.setattr(l1_kernel, "render_sysctl_dropin", lambda: "BODY\n")
    assert host_batch._sysctl_satisfied() is True
    monkeypatch.setattr(l1_kernel, "render_sysctl_dropin", lambda: "OTHER\n")
    assert host_batch._sysctl_satisfied() is False


def test_nftables_satisfied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    persist = tmp_path / "modules.conf"
    persist.write_text(host_batch._render_modules_load_dropin("fedora"))
    monkeypatch.setattr(host_batch, "_MODULES_LOAD_DROPIN", persist)
    monkeypatch.setattr(host_batch, "_module_loaded", lambda _m: True)
    assert host_batch._nftables_satisfied("fedora") is True


def test_nftables_unsatisfied_when_module_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    persist = tmp_path / "modules.conf"
    persist.write_text(host_batch._render_modules_load_dropin("fedora"))
    monkeypatch.setattr(host_batch, "_MODULES_LOAD_DROPIN", persist)
    monkeypatch.setattr(host_batch, "_module_loaded", lambda _m: False)
    assert host_batch._nftables_satisfied("fedora") is False


# --------------------------------------------------------------------------- #
# build_bootstrap_argv / render_remediation_block
# --------------------------------------------------------------------------- #


def test_build_bootstrap_argv_order_and_content() -> None:
    items = frozenset({BatchItem.MARKER, BatchItem.SUBID, BatchItem.RUNSC})
    argv = build_bootstrap_argv(items, _params())

    # --item flags appear in canonical order, not set order.
    item_values = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--item"]
    assert item_values == ["subid", "runsc", "marker"]

    assert argv[0:2] == ["sandbox", "_bootstrap-host"]
    assert "--operator" in argv
    assert argv[argv.index("--operator") + 1] == "alice"
    assert argv[argv.index("--operator-uid") + 1] == "1000"
    assert argv[argv.index("--bridge-group") + 1] == "sb-ws"
    assert argv[argv.index("--bridge-gid") + 1] == "100500"
    assert argv[argv.index("--distro-family") + 1] == "debian"
    assert argv[argv.index("--docker-execution-mode") + 1] == "operator-rootless"


def test_build_bootstrap_argv_empty_items() -> None:
    argv = build_bootstrap_argv(frozenset(), _params())
    assert "--item" not in argv
    assert argv[0:2] == ["sandbox", "_bootstrap-host"]


def test_render_remediation_block_order() -> None:
    items = frozenset({BatchItem.MARKER, BatchItem.GROUPADD, BatchItem.SUBID})
    block = render_remediation_block(items, _params())
    lines = block.splitlines()

    # Header + one line per requested item, in canonical order.
    body = lines[1:]
    assert body[0].startswith("sudo usermod --add-subuids")  # subid
    assert "groupadd" in body[1]  # groupadd
    assert "--docker-execution-mode" in body[2]  # marker


def test_render_remediation_block_each_item_renders() -> None:
    block = render_remediation_block(frozenset(BatchItem), _params(distro_family="arch"))
    assert "usermod --add-subuids" in block
    assert "groupadd" in block
    assert str(l1_kernel.SYSCTL_DROPIN) in block
    assert "modprobe nf_tables ip_tables" in block
    assert "daemon-reload" in block
    assert "loginctl enable-linger" in block
    assert "--update-runsc" in block
    assert "--docker-execution-mode operator-rootless" in block
