# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the typed dispatcher orchestration (Milestone 2).

Covers the op enum surface + ``invoke`` signature contract (load-bearing for
later milestones, carried over from Milestone 1), plus the real per-op
argument validators and target-argv builders.

The expected target-argv strings are the single source of truth in
``src/templates/dispatch/fixtures/target_argv_cases.json`` — this module and
Milestone 3's Go ``main_test.go`` both read that one file. Tests assert
spec-scenario behavior ("Per-Op Argument Validation" and "Target Argv
Construction Per Op"), never whatever the code happens to return.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from core.compose import compose_project_name
from core.dispatch import (
    _DISPATCH_BINARY,
    OP_SPECS,
    DispatchValidationError,
    Op,
    OpSpec,
    ProbeOutcome,
    StreamingOpError,
    _expand_compose_wire,
    _expand_fwd_wire,
    _invoke_with_nonce,
    _preflight_inner,
    build_invocation,
    build_target_argv,
    compile_dispatcher,
    dispatch_payload,
    invoke,
    parse_preflight_outcome,
    probe,
    proxy_argv,
    resolve_fwd_state,
    sudo_pipe_crossing_argv,
    validate_args,
)
from core.exceptions import SandboxExecutionError
from core.hydration import IMAGE_REGISTRY
from core.ipam import IPAMLedger, derive_static_ips

if TYPE_CHECKING:
    from core.host_config import HostConfig

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "templates"
    / "dispatch"
    / "fixtures"
    / "target_argv_cases.json"
)

EXPECTED_OP_VALUES = {
    "auth-probe",
    "compose-up",
    "compose-down",
    "compose-ps",
    "compose-ls",
    "docker-version",
    "docker-info",
    "docker-manifest-inspect",
    "helper-chown-files",
    "helper-mkdir-chown-dirs",
    "preflight",
    "fwd",
}

_BUSYBOX_REF = "busybox@sha256:3c6ae8008e2c2eedd141725c30b20d9c36b026eb796688f88205845ef17aa213"


def _load_fixture() -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", json.loads(_FIXTURE_PATH.read_text()))


@pytest.fixture
def host_config() -> HostConfig:
    # Builders accept the resolved HostConfig but the deterministic ops never
    # read it; a sentinel object is sufficient for those, and the compose ops
    # below use a real seeded instance instead.
    return cast("HostConfig", object())


# ─── Op enum surface (load-bearing contract — carried from Milestone 1) ──────


class TestOpEnum:
    def test_op_enum_has_exactly_twelve_members(self) -> None:
        assert len(list(Op)) == 12

    def test_op_enum_values_match_expected_wire_names(self) -> None:
        assert {op.value for op in Op} == EXPECTED_OP_VALUES

    @pytest.mark.parametrize("wire_name", sorted(EXPECTED_OP_VALUES))
    def test_each_wire_name_round_trips_via_strenum(self, wire_name: str) -> None:
        op = Op(wire_name)
        assert op == wire_name
        assert op.value == wire_name

    def test_op_specs_cover_every_op(self) -> None:
        assert set(OP_SPECS) == set(Op)

    @pytest.mark.parametrize("op", list(Op))
    def test_op_spec_shape(self, op: Op) -> None:
        spec = OP_SPECS[op]
        assert isinstance(spec, OpSpec)
        assert spec.name == op.value
        assert spec.min_args >= 0
        assert spec.max_args is None or spec.max_args >= spec.min_args

    def test_op_spec_is_frozen(self) -> None:
        spec = OP_SPECS[Op.AUTH_PROBE]
        attr = "name"
        with pytest.raises(AttributeError):
            setattr(spec, attr, "mutated")

    def test_per_op_arg_bounds_are_real(self) -> None:
        # Milestone 2 sets the true per-op bounds (Milestone 1 left 0/None
        # placeholders); assert each op's declared min/max args.
        bounds = {op: (OP_SPECS[op].min_args, OP_SPECS[op].max_args) for op in Op}
        assert bounds == {
            Op.AUTH_PROBE: (0, 0),
            Op.COMPOSE_LS: (0, 0),
            Op.DOCKER_VERSION: (0, 0),
            Op.COMPOSE_UP: (1, 1),
            Op.COMPOSE_PS: (1, 1),
            Op.COMPOSE_DOWN: (1, 2),
            Op.DOCKER_INFO: (1, 1),
            Op.DOCKER_MANIFEST_INSPECT: (1, 1),
            Op.HELPER_CHOWN_FILES: (5, None),
            Op.HELPER_MKDIR_CHOWN_DIRS: (4, None),
            Op.PREFLIGHT: (0, 0),
            Op.FWD: (1, 1),
        }


# ─── invoke() signature contract (load-bearing — carried from Milestone 1) ──


class TestInvokeSignature:
    def test_invoke_signature_is_load_bearing(self) -> None:
        sig = inspect.signature(invoke)
        params = sig.parameters
        assert list(params) == ["op", "args", "host_config", "timeout"]
        assert params["op"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert params["args"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert params["host_config"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        timeout = params["timeout"]
        assert timeout.kind is inspect.Parameter.KEYWORD_ONLY
        assert timeout.default is None

    def test_invoke_return_annotation_is_completed_process(self) -> None:
        # M3 reconciled the M1-era ``[bytes]`` placeholder: the sterile
        # ``core.executor.Executor`` (the only sanctioned execution path)
        # returns text-mode streams, so the annotation is ``[str]``.
        sig = inspect.signature(invoke)
        assert sig.return_annotation == "subprocess.CompletedProcess[str]"


# ─── Per-Op Argument Validation ─────────────────────────────────────────────


_NULLARY_OPS = ["auth-probe", "compose-ls", "docker-version", "preflight"]


class TestValidatorsNullary:
    @pytest.mark.parametrize("op", _NULLARY_OPS)
    def test_accepts_no_args(self, op: str) -> None:
        validate_args(op, [])

    @pytest.mark.parametrize("op", _NULLARY_OPS)
    def test_rejects_any_args(self, op: str) -> None:
        with pytest.raises(DispatchValidationError, match="no arguments"):
            validate_args(op, ["unexpected"])

    @pytest.mark.parametrize("op", _NULLARY_OPS)
    def test_rejects_multiple_args(self, op: str) -> None:
        with pytest.raises(DispatchValidationError):
            validate_args(op, ["a", "b"])


class TestValidatorsComposeInstance:
    @pytest.mark.parametrize("op", ["compose-up", "compose-ps"])
    def test_accepts_valid_instance_name(self, op: str) -> None:
        validate_args(op, ["my-inst_01"])

    @pytest.mark.parametrize("op", ["compose-up", "compose-ps"])
    def test_rejects_zero_args(self, op: str) -> None:
        with pytest.raises(DispatchValidationError, match="exactly one"):
            validate_args(op, [])

    @pytest.mark.parametrize("op", ["compose-up", "compose-ps"])
    def test_rejects_two_args(self, op: str) -> None:
        with pytest.raises(DispatchValidationError, match="exactly one"):
            validate_args(op, ["a", "b"])

    @pytest.mark.parametrize("op", ["compose-up", "compose-ps"])
    def test_rejects_path_traversal_instance(self, op: str) -> None:
        # `compose-up ../../../etc/passwd` rejected by the instance-name regex
        # (design D4; spec "Per-Op Argument Validation").
        with pytest.raises(DispatchValidationError, match=r"invalid characters|must not start"):
            validate_args(op, ["../../../etc/passwd"])

    @pytest.mark.parametrize("op", ["compose-up", "compose-ps"])
    def test_rejects_leading_dash(self, op: str) -> None:
        with pytest.raises(DispatchValidationError, match="must not start"):
            validate_args(op, ["-bad"])

    @pytest.mark.parametrize("op", ["compose-up", "compose-ps"])
    def test_rejects_uppercase(self, op: str) -> None:
        with pytest.raises(DispatchValidationError, match="invalid characters"):
            validate_args(op, ["BadInst"])

    @pytest.mark.parametrize("op", ["compose-up", "compose-ps"])
    def test_rejects_over_length(self, op: str) -> None:
        with pytest.raises(DispatchValidationError, match="character cap"):
            validate_args(op, ["a" * 31])

    @pytest.mark.parametrize("op", ["compose-up", "compose-ps"])
    def test_rejects_reserved_name(self, op: str) -> None:
        with pytest.raises(DispatchValidationError, match="reserved"):
            validate_args(op, ["ipc"])

    @pytest.mark.parametrize("op", ["compose-up", "compose-ps"])
    def test_rejects_empty_name(self, op: str) -> None:
        with pytest.raises(DispatchValidationError, match="must not be empty"):
            validate_args(op, [""])


class TestValidatorComposeDown:
    def test_accepts_instance_only(self) -> None:
        validate_args("compose-down", ["myinst"])

    def test_accepts_optional_volumes(self) -> None:
        # Spec scenario: "Validator accepts compose-down with optional --volumes"
        validate_args("compose-down", ["myinst", "--volumes"])

    def test_rejects_non_volumes_second_arg(self) -> None:
        # Spec scenario: "Validator rejects compose-down with a non---volumes
        # second arg"
        with pytest.raises(DispatchValidationError, match="--volumes"):
            validate_args("compose-down", ["myinst", "-v"])

    def test_rejects_zero_args(self) -> None:
        with pytest.raises(DispatchValidationError):
            validate_args("compose-down", [])

    def test_rejects_third_arg(self) -> None:
        with pytest.raises(DispatchValidationError):
            validate_args("compose-down", ["myinst", "--volumes", "extra"])

    def test_rejects_bad_instance_name(self) -> None:
        with pytest.raises(DispatchValidationError, match=r"invalid characters|must not start"):
            validate_args("compose-down", ["../escape", "--volumes"])


class TestValidatorDockerInfo:
    @pytest.mark.parametrize("preset", ["security-options", "runtimes"])
    def test_accepts_known_presets(self, preset: str) -> None:
        # Spec scenario: "Validator accepts known good args"
        validate_args("docker-info", [preset])

    def test_rejects_unknown_preset(self) -> None:
        # Spec scenario: "Validator rejects unknown docker-info preset"
        with pytest.raises(DispatchValidationError, match=r"security-options.*runtimes|unknown"):
            validate_args("docker-info", ["all"])

    def test_rejects_zero_args(self) -> None:
        with pytest.raises(DispatchValidationError, match="exactly one"):
            validate_args("docker-info", [])

    def test_rejects_two_args(self) -> None:
        with pytest.raises(DispatchValidationError, match="exactly one"):
            validate_args("docker-info", ["runtimes", "extra"])


class TestValidatorDockerManifestInspect:
    """Q7: validation is by ``IMAGE_REGISTRY`` set membership, not by grammar.

    The op's legitimate domain is exactly ``{pin.pinned}`` union ``{pin.tagged}`` over
    ``IMAGE_REGISTRY`` (spec "Per-Op Argument Validation" + scenarios).
    """

    def test_accepts_registry_pinned_ref(self) -> None:
        # Spec scenario: "Validator accepts a registry pinned-digest ref".
        validate_args(
            "docker-manifest-inspect", [IMAGE_REGISTRY["busybox_musl"].pinned]
        )

    def test_accepts_registry_tagged_ref(self) -> None:
        # Spec scenario: "Validator accepts a registry tag ref (tag-drift probe
        # path)" — the supply_chain.py tag-drift call now routes through the op.
        validate_args(
            "docker-manifest-inspect", [IMAGE_REGISTRY["busybox_musl"].tagged]
        )

    def test_accepts_every_registry_pinned_and_tagged(self) -> None:
        for pin in IMAGE_REGISTRY.values():
            validate_args("docker-manifest-inspect", [pin.pinned])
            validate_args("docker-manifest-inspect", [pin.tagged])

    def test_rejects_bare_digest(self) -> None:
        # Spec scenario: "Validator rejects a ref not in IMAGE_REGISTRY (incl.
        # bare digest)" — a bare ``sha256:<hex>`` is not a registry member.
        with pytest.raises(DispatchValidationError, match="IMAGE_REGISTRY"):
            validate_args("docker-manifest-inspect", ["sha256:" + "a" * 64])

    def test_rejects_arbitrary_non_registry_digest_ref(self) -> None:
        # Spec scenario: "Validator rejects a ref not in IMAGE_REGISTRY" — an
        # arbitrary ``name@sha256:<hex>`` not present in the registry.
        with pytest.raises(DispatchValidationError, match="IMAGE_REGISTRY"):
            validate_args(
                "docker-manifest-inspect", ["evil/image@sha256:" + "a" * 64]
            )

    def test_rejects_non_registry_tag_ref(self) -> None:
        with pytest.raises(DispatchValidationError, match="IMAGE_REGISTRY"):
            validate_args("docker-manifest-inspect", ["busybox:latest"])

    def test_rejects_zero_args(self) -> None:
        with pytest.raises(DispatchValidationError, match="exactly one"):
            validate_args("docker-manifest-inspect", [])


class TestValidatorHelperChownFiles:
    def test_accepts_minimal_valid(self) -> None:
        validate_args("helper-chown-files", ["/srv/p", "0644", "1000", "1000", "a.log"])

    def test_accepts_multiple_files(self) -> None:
        validate_args(
            "helper-chown-files", ["/srv/p", "7777", "0", "0", "a", "b", "c"]
        )

    def test_rejects_path_traversal_in_file(self) -> None:
        # Spec scenario: "Validator rejects path traversal in helper-chown-files"
        with pytest.raises(DispatchValidationError, match=r"\.\."):
            validate_args(
                "helper-chown-files",
                ["/srv/parent", "0644", "1000", "1000", "../escape.txt"],
            )

    def test_rejects_too_few_args(self) -> None:
        with pytest.raises(DispatchValidationError, match=">=5"):
            validate_args("helper-chown-files", ["/srv/p", "0644", "1000", "1000"])

    def test_rejects_relative_parent(self) -> None:
        with pytest.raises(DispatchValidationError, match="absolute"):
            validate_args("helper-chown-files", ["srv/p", "0644", "1000", "1000", "a"])

    def test_rejects_parent_traversal(self) -> None:
        with pytest.raises(DispatchValidationError, match="components"):
            validate_args("helper-chown-files", ["/srv/../etc", "0644", "1000", "1000", "a"])

    def test_rejects_non_octal_mode(self) -> None:
        with pytest.raises(DispatchValidationError, match="octal"):
            validate_args("helper-chown-files", ["/srv/p", "0888", "1000", "1000", "a"])

    def test_rejects_three_digit_mode(self) -> None:
        with pytest.raises(DispatchValidationError, match="4-digit octal"):
            validate_args("helper-chown-files", ["/srv/p", "644", "1000", "1000", "a"])

    def test_rejects_negative_uid(self) -> None:
        with pytest.raises(DispatchValidationError, match="uid"):
            validate_args("helper-chown-files", ["/srv/p", "0644", "-1", "1000", "a"])

    def test_rejects_non_numeric_gid(self) -> None:
        with pytest.raises(DispatchValidationError, match="gid"):
            validate_args("helper-chown-files", ["/srv/p", "0644", "1000", "root", "a"])

    def test_rejects_slash_in_file(self) -> None:
        with pytest.raises(DispatchValidationError, match="'/'"):
            validate_args("helper-chown-files", ["/srv/p", "0644", "1000", "1000", "sub/a"])

    def test_rejects_nul_in_file(self) -> None:
        with pytest.raises(DispatchValidationError, match="NUL"):
            validate_args("helper-chown-files", ["/srv/p", "0644", "1000", "1000", "a\x00b"])

    def test_rejects_newline_in_file(self) -> None:
        with pytest.raises(DispatchValidationError, match="newline"):
            validate_args("helper-chown-files", ["/srv/p", "0644", "1000", "1000", "a\nb"])

    def test_rejects_nul_in_parent(self) -> None:
        with pytest.raises(DispatchValidationError, match="NUL"):
            validate_args("helper-chown-files", ["/srv\x00", "0644", "1000", "1000", "a"])

    def test_rejects_newline_in_parent(self) -> None:
        with pytest.raises(DispatchValidationError, match="newline"):
            validate_args("helper-chown-files", ["/srv\n", "0644", "1000", "1000", "a"])

    def test_rejects_dotdot_exact_file(self) -> None:
        with pytest.raises(DispatchValidationError, match=r"\.\."):
            validate_args("helper-chown-files", ["/srv/p", "0644", "1000", "1000", ".."])


class TestValidatorHelperMkdirChownDirs:
    def test_accepts_minimal_valid(self) -> None:
        validate_args("helper-mkdir-chown-dirs", ["/srv/p", "1000", "1000", "logs"])

    def test_accepts_multiple_leaves(self) -> None:
        validate_args("helper-mkdir-chown-dirs", ["/srv/p", "0", "0", "a", "b"])

    def test_rejects_too_few_args(self) -> None:
        with pytest.raises(DispatchValidationError, match=">=4"):
            validate_args("helper-mkdir-chown-dirs", ["/srv/p", "1000", "1000"])

    def test_rejects_relative_parent(self) -> None:
        with pytest.raises(DispatchValidationError, match="absolute"):
            validate_args("helper-mkdir-chown-dirs", ["srv/p", "1000", "1000", "logs"])

    def test_rejects_parent_traversal(self) -> None:
        with pytest.raises(DispatchValidationError, match="components"):
            validate_args("helper-mkdir-chown-dirs", ["/srv/../x", "1000", "1000", "logs"])

    def test_rejects_non_numeric_uid(self) -> None:
        with pytest.raises(DispatchValidationError, match="uid"):
            validate_args("helper-mkdir-chown-dirs", ["/srv/p", "x", "1000", "logs"])

    def test_rejects_traversal_in_leaf(self) -> None:
        with pytest.raises(DispatchValidationError, match=r"\.\."):
            validate_args("helper-mkdir-chown-dirs", ["/srv/p", "1000", "1000", ".."])

    def test_rejects_slash_in_leaf(self) -> None:
        with pytest.raises(DispatchValidationError, match="'/'"):
            validate_args("helper-mkdir-chown-dirs", ["/srv/p", "1000", "1000", "a/b"])

    def test_rejects_nul_in_leaf(self) -> None:
        with pytest.raises(DispatchValidationError, match="NUL"):
            validate_args("helper-mkdir-chown-dirs", ["/srv/p", "1000", "1000", "a\x00"])

    def test_rejects_newline_in_leaf(self) -> None:
        with pytest.raises(DispatchValidationError, match="newline"):
            validate_args("helper-mkdir-chown-dirs", ["/srv/p", "1000", "1000", "a\n"])


class TestValidateArgsUnknownOp:
    def test_unknown_op_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            validate_args("not-a-real-op", [])

    def test_accepts_op_enum_member(self) -> None:
        validate_args(Op.AUTH_PROBE, [])


# ─── fwd: the streaming op (C-010) — validator (typed-arg surface) ──────────
#
# Typed callers pass ONLY ``[<inst>]``; the validator reuses the instance-name
# rule (the same one the compose ops enforce) and rejects any second positional
# (the --project/--ip flags are produced operator-side by _expand_fwd_wire,
# never caller-supplied). Spec "Per-Op Argument Validation".


class TestValidatorFwd:
    def test_accepts_valid_instance_name(self) -> None:
        validate_args("fwd", ["my-inst_01"])

    def test_accepts_via_op_enum(self) -> None:
        validate_args(Op.FWD, ["myinst"])

    def test_rejects_zero_args(self) -> None:
        with pytest.raises(DispatchValidationError, match="exactly one"):
            validate_args("fwd", [])

    def test_rejects_path_traversal_instance(self) -> None:
        # ``fwd ../escape`` rejected by the instance-name regex.
        with pytest.raises(DispatchValidationError, match=r"invalid characters|must not start"):
            validate_args("fwd", ["../escape"])

    def test_rejects_second_positional(self) -> None:
        # A second positional (e.g. a caller smuggling the IP) is rejected:
        # only [<inst>] is a legal typed arg.
        with pytest.raises(DispatchValidationError, match="exactly one"):
            validate_args("fwd", ["myinst", "10.100.0.7"])

    def test_rejects_leading_dash(self) -> None:
        with pytest.raises(DispatchValidationError, match="must not start"):
            validate_args("fwd", ["-bad"])


# ─── Target Argv Construction Per Op (fixture is the single source) ─────────


class TestTargetArgvFixture:
    def test_fixture_covers_all_ops(self) -> None:
        # Q6 (3.3b): the fixture is keyed on each op's WIRE form. For the
        # deterministic ops (incl. the read-only preflight bundle) that is the
        # typed args; for the three compose ops it is the post-expansion
        # named-flag form. Keyed this way every op's target argv is a pure
        # function of its wire inputs, so all twelve ops live in the one shared
        # fixture and the Python<->Go lockstep covers compose + fwd. (The
        # operator-side <inst>-><operands> resolution stays dynamically tested
        # below — it depends on a seeded instance.)
        ops_in_fixture = {cast("str", c["op"]) for c in _load_fixture()}
        assert ops_in_fixture == EXPECTED_OP_VALUES

    @pytest.mark.parametrize("case", _load_fixture(), ids=lambda c: f"{c['op']}-{c['args']}")
    def test_builder_matches_fixture(
        self, case: dict[str, object], host_config: HostConfig
    ) -> None:
        op = cast("str", case["op"])
        args = cast("list[str]", case["args"])
        expected = cast("list[str]", case["expected_target_argv"])
        assert build_target_argv(op, args, host_config) == expected

    def test_auth_probe_canonical_echo_ok(self, host_config: HostConfig) -> None:
        # Spec scenario: "auth-probe constructs canonical echo ok argv"
        assert build_target_argv("auth-probe", [], host_config) == [
            "/bin/bash",
            "-c",
            "echo ok",
        ]

    def test_docker_info_runtimes_argv(self, host_config: HostConfig) -> None:
        # Spec scenario: "docker-info preset 'runtimes' constructs the
        # runtimes-format argv"
        assert build_target_argv("docker-info", ["runtimes"], host_config) == [
            "/bin/bash",
            "-c",
            "docker info --format '{{json .Runtimes}}'",
        ]

    def test_docker_info_security_options_argv(self, host_config: HostConfig) -> None:
        assert build_target_argv("docker-info", ["security-options"], host_config) == [
            "/bin/bash",
            "-c",
            "docker info --format '{{.SecurityOptions}}'",
        ]

    def test_preflight_inner_is_bash_c(self, host_config: HostConfig) -> None:
        argv = build_target_argv("preflight", [], host_config)
        assert argv[:2] == ["/bin/bash", "-c"]
        assert len(argv) == 3

    def test_preflight_semicolon_sequenced_not_set_e_or_andand(
        self, host_config: HostConfig
    ) -> None:
        # F-065: ``;``-sequenced so one query's failure neither aborts the
        # others nor forges their success. NOT ``&&`` / ``set -e``.
        inner = build_target_argv("preflight", [], host_config)[2]
        assert " ; " in inner
        assert "&&" not in inner
        assert "set -e" not in inner

    def test_preflight_runtimes_query_appears_once_deduped(
        self, host_config: HostConfig
    ) -> None:
        # The runsc / runsc-runtimeArgs / host-uds checks all derive from a
        # SINGLE ``docker info --format '{{json .Runtimes}}'`` query.
        inner = build_target_argv("preflight", [], host_config)[2]
        assert inner.count("docker info --format '{{json .Runtimes}}'") == 1

    def test_preflight_bundle_reuses_each_contributing_builder_inner(
        self, host_config: HostConfig
    ) -> None:
        # SSOT meta-test (C-009 4.2): the bundled inner CONTAINS each
        # contributing read-only op's individual builder inner VERBATIM. A
        # future edit to any single builder (e.g. a docker-version flag change)
        # is reflected here automatically, or this test fails — proving the
        # bundle never re-spells a query string that has drifted from its op.
        inner = build_target_argv("preflight", [], host_config)[2]
        contributing = [
            build_target_argv("auth-probe", [], host_config)[2],
            build_target_argv("docker-version", [], host_config)[2],
            build_target_argv("docker-info", ["security-options"], host_config)[2],
            build_target_argv("docker-info", ["runtimes"], host_config)[2],
            build_target_argv("compose-ls", [], host_config)[2],
        ]
        for op_inner in contributing:
            assert op_inner in inner, op_inner

    def test_preflight_each_query_individually_attributable(
        self, host_config: HostConfig
    ) -> None:
        # Each query is preceded by a per-query begin marker on its own echo and
        # followed by a per-query exit marker (``__PREFLIGHT_RC_<name>_$?__``),
        # with stderr merged (``2>&1``), so M4b (cli-start) can split + map each
        # segment to its check and reconstruct stdout + exit + merged stderr.
        inner = build_target_argv("preflight", [], host_config)[2]
        for name in (
            "auth-probe",
            "docker-version",
            "docker-info-security-options",
            "docker-info-runtimes",
            "compose-ls",
        ):
            assert f"echo __PREFLIGHT_Q_${{__PFNONCE}}_{name}__" in inner
            assert f"echo __PREFLIGHT_RC_${{__PFNONCE}}_{name}_$?__" in inner

    def test_preflight_markers_reference_the_pfnonce_shell_var(
        self, host_config: HostConfig
    ) -> None:
        # H-1: the bundle inner is byte-static (the literal ``${__PFNONCE}``
        # token, NOT a concrete nonce) so the Python↔Go fixture stays identical;
        # the per-crossing nonce is supplied at shell-expansion time by
        # wrapSentinel (framed) or core.dispatch (operator-rootless local).
        inner = build_target_argv("preflight", [], host_config)[2]
        assert "${__PFNONCE}" in inner
        # No concrete hex nonce baked into the template.
        assert "__PREFLIGHT_Q_${__PFNONCE}_auth-probe__" in inner

    def test_preflight_each_query_merges_stderr_and_carries_exit(
        self, host_config: HostConfig
    ) -> None:
        # Per query: ``<inner> 2>&1`` (stderr merged into the attributed segment)
        # immediately followed by the RC marker. Count parity proves every query
        # got both — and that ``2>&1`` is paired with an RC echo.
        inner = build_target_argv("preflight", [], host_config)[2]
        n_queries = 5
        assert inner.count(" 2>&1; echo __PREFLIGHT_RC_") == n_queries
        assert inner.count("__PREFLIGHT_Q_") == n_queries
        assert inner.count("__PREFLIGHT_RC_") == n_queries
        assert inner.count("_$?__") == n_queries

    def test_helper_chown_files_byte_faithful_to_hardened_helper(
        self, host_config: HostConfig
    ) -> None:
        # Spec scenario: "helper-chown-files target argv is byte-faithful to
        # the existing hardened helper". Assert the builder reuses
        # _hardened_docker_run rather than re-deriving flags.
        from core.helper_container import _hardened_docker_run
        from core.hydration import IMAGE_REGISTRY

        argv = build_target_argv(
            "helper-chown-files",
            ["/srv/cache", "0644", "1000", "1000", "a.log", "b.log"],
            host_config,
        )
        inner = (
            'set -e; for f in a.log b.log; do cp /p/"$f" /tmp/"$f" && '
            'unlink /p/"$f" && cp /tmp/"$f" /p/"$f" && '
            'chmod 0644 /p/"$f" && chown 1000:1000 /p/"$f"; done'
        )
        expected_cmd = _hardened_docker_run(
            IMAGE_REGISTRY["busybox_musl"].pinned, "/srv/cache", inner
        )
        assert argv == ["/bin/bash", "-c", expected_cmd]
        # The space-separated source form, NOT the =-joined form.
        assert "--cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE" in argv[2]
        assert "--cap-drop=ALL" not in argv[2]
        assert "--security-opt no-new-privileges:true" in argv[2]

    def test_helper_mkdir_chown_dirs_byte_faithful(self, host_config: HostConfig) -> None:
        from core.helper_container import _hardened_docker_run
        from core.hydration import IMAGE_REGISTRY

        argv = build_target_argv(
            "helper-mkdir-chown-dirs",
            ["/srv/cache", "1000", "1000", "logs", "cache"],
            host_config,
        )
        inner = (
            'set -e; for d in logs cache; do mkdir -p /p/"$d" && '
            'chown 1000:1000 /p/"$d"; done'
        )
        expected_cmd = _hardened_docker_run(
            IMAGE_REGISTRY["busybox_musl"].pinned, "/srv/cache", inner
        )
        assert argv == ["/bin/bash", "-c", expected_cmd]
        # No chmod in the mkdir loop (Decision 14).
        assert "chmod" not in argv[2]

    def test_helper_chown_normalizes_mode_to_four_digit_octal(
        self, host_config: HostConfig
    ) -> None:
        # Octal-int round-trip mirrors helper_container's format(mode, "04o").
        argv = build_target_argv(
            "helper-chown-files",
            ["/srv/p", "0640", "1000", "1000", "f"],
            host_config,
        )
        assert "chmod 0640 " in argv[2]

    def test_helper_chown_files_quotes_special_names(self, host_config: HostConfig) -> None:
        argv = build_target_argv(
            "helper-chown-files",
            ["/srv/p", "0644", "1000", "1000", "name with space"],
            host_config,
        )
        assert "'name with space'" in argv[2]

    def test_docker_manifest_inspect_argv(self, host_config: HostConfig) -> None:
        assert build_target_argv(
            "docker-manifest-inspect", [_BUSYBOX_REF], host_config
        ) == ["/bin/bash", "-c", f"docker manifest inspect {_BUSYBOX_REF}"]

    def test_docker_manifest_inspect_tagged_argv(self, host_config: HostConfig) -> None:
        # Q7: the tag-drift probe path (``pin.tagged``) builds the same
        # ``docker manifest inspect <ref>`` shape (matches the ``.tagged``
        # fixture row added in 6b.2).
        tagged = IMAGE_REGISTRY["busybox_musl"].tagged
        assert build_target_argv(
            "docker-manifest-inspect", [tagged], host_config
        ) == ["/bin/bash", "-c", f"docker manifest inspect {tagged}"]

    def test_build_target_argv_accepts_op_enum(self, host_config: HostConfig) -> None:
        assert build_target_argv(Op.COMPOSE_LS, [], host_config) == [
            "/bin/bash",
            "-c",
            "docker compose ls --format json --all",
        ]

    def test_build_target_argv_unknown_op_raises(self, host_config: HostConfig) -> None:
        with pytest.raises(ValueError):
            build_target_argv("not-real", [], host_config)


# ─── Compose ops: runtime-state-dependent target argv (seeded instance) ─────


def _seed_instance(home: Path, inst: str, *, pg: bool = False, fc: bool = False) -> None:
    import json as _json

    inst_dir = home / "instances" / inst
    (inst_dir / "docker" / "extras").mkdir(parents=True, exist_ok=True)
    (inst_dir / "docker" / "compose.yml").write_text("services: {}\n")
    (inst_dir / ".sandbox.env").write_text("")
    # DbPostgresConfig.enabled defaults to True, so always write the section
    # explicitly to make the extra-compose-file toggle deterministic.
    components = f"\n[components.db_postgres]\nenabled = {str(pg).lower()}\n"
    if fc:
        components += "\n[components]\nmcp_firecrawl = true\n"
    (inst_dir / "sandbox.toml").write_text(
        f'[instance]\nname = "{inst}"\nhost_uid = "1000"\n\n'
        '[workspaces.main]\nbootstrap_mode = "empty"\n'
        f'path = "{home}/workspaces/{inst}/main"\n' + components
    )
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instances.json").write_text(
        _json.dumps(
            {inst: {"instance_dir": str(inst_dir), "created_at": "2026-01-01T00:00:00Z"}}
        )
    )


# ─── Q6: pure wire-keyed compose builder (byte-identical to Go) ─────────────


_WIRE = [
    "myinst",
    "--project",
    "op-myinst",
    "--env-file",
    "/home/op/.sandbox-ai/instances/myinst/.sandbox.env",
    "--compose-file",
    "/home/op/.sandbox-ai/instances/myinst/docker/compose.yml",
]


class TestComposeWireBuilder:
    """The compose ``build_target_argv`` is now a PURE function of the
    post-expansion wire form (Q6). It is fully covered by the shared-fixture
    parametrized test above; these add the spec-scenario byte-faithfulness
    assertions and the wire-flag parse-rejection paths the Go binary mirrors.
    """

    def test_compose_up_wire_byte_faithful(self, host_config: HostConfig) -> None:
        assert build_target_argv("compose-up", _WIRE, host_config) == [
            "/bin/bash",
            "-c",
            "TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain "
            "COMPOSE_PROJECT_NAME=op-myinst docker compose "
            "-f /home/op/.sandbox-ai/instances/myinst/docker/compose.yml "
            "--ansi never --env-file "
            "/home/op/.sandbox-ai/instances/myinst/.sandbox.env up -d --build --wait",
        ]

    def test_compose_down_no_volumes(self, host_config: HostConfig) -> None:
        argv = build_target_argv("compose-down", _WIRE, host_config)
        assert argv[2].endswith(" down")
        assert " -v" not in argv[2]

    def test_compose_down_with_volumes_verb_is_down_dash_v(
        self, host_config: HostConfig
    ) -> None:
        # Spec scenario: "compose-down destroy carries --volumes in the wire
        # form" -> op-hardcoded verb becomes ``down -v``.
        argv = build_target_argv("compose-down", [*_WIRE, "--volumes"], host_config)
        assert argv[2].endswith(" down -v")

    def test_compose_ps_wire_byte_faithful(self, host_config: HostConfig) -> None:
        argv = build_target_argv("compose-ps", _WIRE, host_config)
        assert argv[2].endswith(
            "--env-file /home/op/.sandbox-ai/instances/myinst/.sandbox.env "
            "--ansi never ps --format json"
        )

    def test_multiple_compose_files_preserve_order(self, host_config: HostConfig) -> None:
        wire = [
            "myinst",
            "--project",
            "op-myinst",
            "--env-file",
            "/home/op/.sandbox-ai/instances/myinst/.sandbox.env",
            "--compose-file",
            "/home/op/.sandbox-ai/instances/myinst/docker/compose.yml",
            "--compose-file",
            "/home/op/.sandbox-ai/instances/myinst/docker/extras/db-postgres.yml",
        ]
        argv = build_target_argv("compose-up", wire, host_config)
        assert (
            "-f /home/op/.sandbox-ai/instances/myinst/docker/compose.yml "
            "-f /home/op/.sandbox-ai/instances/myinst/docker/extras/db-postgres.yml"
        ) in argv[2]

    def test_volumes_rejected_for_compose_up(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="only valid for compose-down"):
            build_target_argv("compose-up", [*_WIRE, "--volumes"], host_config)

    def test_unrecognized_flag_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="unrecognized flag"):
            build_target_argv(
                "compose-up", [*_WIRE, "--runtime", "evil"], host_config
            )

    def test_duplicate_project_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="--project given more than once"):
            build_target_argv(
                "compose-up", [*_WIRE, "--project", "op-myinst"], host_config
            )

    def test_duplicate_env_file_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="--env-file given more than once"):
            build_target_argv(
                "compose-up", [*_WIRE, "--env-file", "/x"], host_config
            )

    def test_duplicate_volumes_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="more than once"):
            build_target_argv(
                "compose-down",
                [*_WIRE, "--volumes", "--volumes"],
                host_config,
            )

    def test_missing_project_rejected(self, host_config: HostConfig) -> None:
        wire = [
            "myinst",
            "--env-file",
            "/home/op/.sandbox-ai/instances/myinst/.sandbox.env",
            "--compose-file",
            "/home/op/.sandbox-ai/instances/myinst/docker/compose.yml",
        ]
        with pytest.raises(DispatchValidationError, match="--project is required"):
            build_target_argv("compose-up", wire, host_config)

    def test_missing_env_file_rejected(self, host_config: HostConfig) -> None:
        wire = [
            "myinst",
            "--project",
            "op-myinst",
            "--compose-file",
            "/home/op/.sandbox-ai/instances/myinst/docker/compose.yml",
        ]
        with pytest.raises(DispatchValidationError, match="--env-file is required"):
            build_target_argv("compose-up", wire, host_config)

    def test_missing_compose_file_rejected(self, host_config: HostConfig) -> None:
        wire = [
            "myinst",
            "--project",
            "op-myinst",
            "--env-file",
            "/home/op/.sandbox-ai/instances/myinst/.sandbox.env",
        ]
        with pytest.raises(DispatchValidationError, match="at least one --compose-file"):
            build_target_argv("compose-up", wire, host_config)

    def test_flag_missing_value_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="missing its value"):
            build_target_argv(
                "compose-up",
                ["myinst", "--project", "op-myinst", "--compose-file"],
                host_config,
            )

    def test_empty_wire_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="missing <instance>"):
            build_target_argv("compose-up", [], host_config)


# ─── Q6: operator-side wire-expansion producer (seeded instance) ────────────


class TestComposeWireExpansion:
    """``_expand_compose_wire`` is the single operator-side resolver path —
    it reuses ``_resolve_compose_state`` (no parallel resolver). It depends on
    a seeded registered instance and is therefore tested dynamically (not via
    the static fixture), per spec "Target Argv Construction Per Op".
    """

    def test_expands_compose_up_to_named_flag_form(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        proj = compose_project_name("demo")
        inst_dir = isolated_sandbox_ai_home / "instances" / "demo"
        wire = _expand_compose_wire("compose-up", ["demo"])
        assert wire == [
            "demo",
            "--project",
            proj,
            "--env-file",
            str(inst_dir / ".sandbox.env"),
            "--compose-file",
            str(inst_dir / "docker" / "compose.yml"),
        ]

    def test_round_trip_expansion_then_build_is_byte_faithful(
        self, isolated_sandbox_ai_home: Path, host_config: HostConfig
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        proj = compose_project_name("demo")
        inst_dir = isolated_sandbox_ai_home / "instances" / "demo"
        compose_yml = inst_dir / "docker" / "compose.yml"
        env = inst_dir / ".sandbox.env"
        wire = _expand_compose_wire("compose-up", ["demo"])
        argv = build_target_argv("compose-up", wire, host_config)
        assert argv == [
            "/bin/bash",
            "-c",
            f"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain "
            f"COMPOSE_PROJECT_NAME={proj} docker compose -f {compose_yml} "
            f"--ansi never --env-file {env} up -d --build --wait",
        ]

    def test_compose_down_destroy_appends_volumes(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        wire = _expand_compose_wire("compose-down", ["demo", "--volumes"])
        assert wire[-1] == "--volumes"

    def test_compose_down_stop_omits_volumes(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        wire = _expand_compose_wire("compose-down", ["demo"])
        assert "--volumes" not in wire

    def test_expansion_includes_postgres_extra(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo", pg=True)
        wire = _expand_compose_wire("compose-up", ["demo"])
        assert any("docker/extras/db-postgres.yml" in w for w in wire)

    def test_expansion_includes_firecrawl_extra(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo", fc=True)
        wire = _expand_compose_wire("compose-up", ["demo"])
        assert any("docker/extras/mcp-firecrawl.yml" in w for w in wire)

    def test_unregistered_instance_raises(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        with pytest.raises(DispatchValidationError, match="no sandbox instance"):
            _expand_compose_wire("compose-up", ["nonexistent"])


# ─── fwd: operator-side wire expansion (seeded instance) ────────────────────
#
# Callers pass [<inst>]; _expand_fwd_wire resolves the compose project name
# (compose_project_name) and the core IPC IP (read-only IPAM peek) — mirroring
# the two lookups cli.main._build_attach_argv performs — and emits the named-
# flag wire ``<inst> --project <P> --ip <IP>`` (spec "fwd Op Wire Expansion").


def _expected_fwd_ip(inst: str) -> str:
    """The core IPC IP _expand_fwd_wire resolves for an UNallocated ``inst``.

    A seeded-but-not-IPAM-allocated instance peeks to the lowest free slot;
    derived via the real ``core.ipam`` functions so the expectation tracks the
    allocator, never a hardcoded literal."""
    base_index, _existing = IPAMLedger().peek_next_slot(inst)
    return derive_static_ips(base_index)["core_ipc_ip"]


class TestFwdWireExpansion:
    def test_expands_to_named_flag_wire(self, isolated_sandbox_ai_home: Path) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        proj = compose_project_name("demo")
        ip = _expected_fwd_ip("demo")
        wire = _expand_fwd_wire(["demo"])
        assert wire == ["demo", "--project", proj, "--ip", ip]

    def test_ip_is_in_ipam_superblock(self, isolated_sandbox_ai_home: Path) -> None:
        # Defense-in-depth invariant the Go side re-checks: the resolved IP is a
        # dotted quad whose first octet is 10 and second is in 100..255.
        _seed_instance(isolated_sandbox_ai_home, "demo")
        ip = _expand_fwd_wire(["demo"])[-1]
        octets = [int(o) for o in ip.split(".")]
        assert len(octets) == 4
        assert octets[0] == 10
        assert 100 <= octets[1] <= 255

    def test_round_trip_expansion_then_build_is_byte_faithful(
        self, isolated_sandbox_ai_home: Path, host_config: HostConfig
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        proj = compose_project_name("demo")
        ip = _expected_fwd_ip("demo")
        wire = _expand_fwd_wire(["demo"])
        argv = build_target_argv("fwd", wire, host_config)
        assert argv == [
            "/usr/bin/docker",
            "exec",
            "-i",
            f"{proj}-admin-1",
            "/fwd",
            f"{ip}:9999",
        ]


# ─── fwd: target-argv builder (pure, fixture-keyed on the wire form) ────────
#
# The ONE op whose target argv is a DIRECT docker-exec argv (no /bin/bash -c
# wrapper) — D3 stream hygiene. The fixture row pins the byte form the Go
# sibling consumes unmodified; these add the spec-scenario assertions + the
# wire parse-rejection paths the Go binary mirrors.


_FWD_WIRE = ["myinst", "--project", "dev-myinst", "--ip", "10.100.0.7"]


class TestFwdTargetArgvBuilder:
    def test_builds_direct_docker_exec_argv_no_bash_c(
        self, host_config: HostConfig
    ) -> None:
        # Spec scenario "fwd constructs the direct docker-exec argv (no bash -c)".
        argv = build_target_argv("fwd", _FWD_WIRE, host_config)
        assert argv == [
            "/usr/bin/docker",
            "exec",
            "-i",
            "dev-myinst-admin-1",
            "/fwd",
            "10.100.0.7:9999",
        ]
        assert "/bin/bash" not in argv
        assert "-c" not in argv

    def test_admin_container_name_derived_from_project(
        self, host_config: HostConfig
    ) -> None:
        wire = ["other", "--project", "dev-other", "--ip", "10.200.0.3"]
        argv = build_target_argv("fwd", wire, host_config)
        assert argv[3] == "dev-other-admin-1"
        assert argv[5] == "10.200.0.3:9999"

    def test_missing_project_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="--project is required"):
            build_target_argv("fwd", ["myinst", "--ip", "10.100.0.7"], host_config)

    def test_missing_ip_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="--ip is required"):
            build_target_argv("fwd", ["myinst", "--project", "dev-myinst"], host_config)

    def test_unrecognized_flag_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="unrecognized flag"):
            build_target_argv("fwd", [*_FWD_WIRE, "--port", "1234"], host_config)

    def test_duplicate_project_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="--project given more than once"):
            build_target_argv("fwd", [*_FWD_WIRE, "--project", "dev-myinst"], host_config)

    def test_duplicate_ip_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="--ip given more than once"):
            build_target_argv("fwd", [*_FWD_WIRE, "--ip", "10.100.0.8"], host_config)

    def test_flag_missing_value_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="missing its value"):
            build_target_argv("fwd", ["myinst", "--project"], host_config)

    def test_empty_wire_rejected(self, host_config: HostConfig) -> None:
        with pytest.raises(DispatchValidationError, match="missing <instance>"):
            build_target_argv("fwd", [], host_config)


# ─── fwd: the streaming ProxyCommand entrypoint (proxy_argv) ────────────────
#
# proxy_argv CONSTRUCTS but never executes the crossing argv (the ssh client
# runs it). Per mode it returns: separate-user -> sudo_pipe_cmd prefix +
# bare ``dispatch fwd <wire>``; operator-rootless -> the bare docker-exec target
# argv. invoke()/probe() reject Op.FWD (it carries zero orchestrator-interpreted
# content). Spec "Streaming ProxyCommand Entrypoint".


class TestFwdProxyArgv:
    def test_sudo_mode_rides_sudo_pipe_cmd_with_bare_payload(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        proj = compose_project_name("demo")
        ip = _expected_fwd_ip("demo")
        argv = proxy_argv("fwd", ["demo"], _sudo_hc())
        assert argv[:5] == ["sudo", "systemd-run", "-q", "--pipe", "--uid=sandbox"]
        assert argv[5:7] == ["/bin/bash", "-c"]
        assert argv[7] == (
            f"/usr/local/libexec/sandbox-ai/dispatch fwd demo "
            f"--project {proj} --ip {ip}"
        )

    def test_operator_rootless_returns_bare_target_argv(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        proj = compose_project_name("demo")
        ip = _expected_fwd_ip("demo")
        argv = proxy_argv("fwd", ["demo"], _rootless_hc())
        assert argv == [
            "/usr/bin/docker",
            "exec",
            "-i",
            f"{proj}-admin-1",
            "/fwd",
            f"{ip}:9999",
        ]
        # No crossing prefix, no dispatcher indirection.
        for token in argv:
            assert "sudo" not in token
            assert "systemd-run" not in token
            assert "machinectl" not in token
            assert "/usr/local/libexec/sandbox-ai/dispatch" not in token

    def test_validates_instance_name_before_resolution(self) -> None:
        # The typed-arg validator runs before any wire expansion / crossing.
        with pytest.raises(DispatchValidationError):
            proxy_argv("fwd", ["../escape"], _sudo_hc())

    def test_rejects_non_streaming_op(self) -> None:
        # proxy_argv is the SOLE producer of streaming-op argv; a framed op
        # belongs on invoke()/probe().
        with pytest.raises(StreamingOpError, match="not a streaming op"):
            proxy_argv("auth-probe", [], _sudo_hc())

    def test_never_executes(
        self, isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # proxy_argv constructs only — it must never touch the Executor.
        _seed_instance(isolated_sandbox_ai_home, "demo")

        def fail_run(self: object, *a: object, **k: object) -> object:
            raise AssertionError("proxy_argv must not execute")

        monkeypatch.setattr("core.dispatch.Executor.run", fail_run)
        proxy_argv("fwd", ["demo"], _rootless_hc())


class TestResolveFwdState:
    """``resolve_fwd_state`` is the public SSOT consumed by BOTH the wire
    expansion and ``cli.main._build_attach_argv`` (its session-log dir name +
    ``agent@<ip>`` destination)."""

    def test_returns_project_name_and_core_ipc_ip(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        project_name, core_ipc_ip = resolve_fwd_state("demo")
        assert project_name == compose_project_name("demo")
        assert core_ipc_ip == _expected_fwd_ip("demo")

    def test_is_the_source_the_wire_expansion_uses(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        # The wire form embeds exactly resolve_fwd_state's two values — proving
        # _expand_fwd_wire and any cli.main consumer share one resolution.
        _seed_instance(isolated_sandbox_ai_home, "demo")
        project_name, core_ipc_ip = resolve_fwd_state("demo")
        assert _expand_fwd_wire(["demo"]) == [
            "demo",
            "--project",
            project_name,
            "--ip",
            core_ipc_ip,
        ]

    def test_is_read_only_no_ipam_allocation(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        # Two calls return identical state (peek_next_slot is read-only); the
        # slot is not consumed, so the warm gate's allocation determinism holds.
        _seed_instance(isolated_sandbox_ai_home, "demo")
        assert resolve_fwd_state("demo") == resolve_fwd_state("demo")


class TestStreamingOpRejectedByInvokeProbe:
    def test_invoke_rejects_fwd(self) -> None:
        with pytest.raises(StreamingOpError, match=r"proxy_argv"):
            invoke(Op.FWD, ["myinst"], _sudo_hc())

    def test_probe_rejects_fwd(self) -> None:
        with pytest.raises(StreamingOpError, match=r"proxy_argv"):
            probe(Op.FWD, ["myinst"], _sudo_hc())

    def test_invoke_rejects_fwd_by_wire_name(self) -> None:
        with pytest.raises(StreamingOpError):
            invoke("fwd", ["myinst"], _rootless_hc())

    def test_build_invocation_rejects_fwd(self) -> None:
        # build_invocation itself raises (the framed construction seam): a
        # streaming op is "not reachable through build_invocation" (D3 prose),
        # and the error names proxy_argv as the sanctioned path.
        with pytest.raises(StreamingOpError, match=r"proxy_argv"):
            build_invocation(Op.FWD, ["myinst"], _sudo_hc())

    def test_rejection_happens_before_any_crossing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guard fires before build_invocation / the Executor is ever reached.
        def fail_run(self: object, *a: object, **k: object) -> object:
            raise AssertionError("invoke must reject the streaming op before crossing")

        monkeypatch.setattr("core.dispatch.Executor.run", fail_run)
        with pytest.raises(StreamingOpError):
            invoke(Op.FWD, ["myinst"], _sudo_hc())


# ─── invoke(): validate -> expand -> machinectl_cmd + bash -c -> Executor ──


class _FakeHostSettings:
    docker_unprivileged_user = "sandbox"

    def __init__(self, auth: object, mode: object) -> None:
        self.machinectl_authentication = auth
        self.docker_execution_mode = mode


class _FakeHostConfig:
    def __init__(self, auth: object, mode: object | None = None) -> None:
        from core.host_config import DockerExecutionMode

        resolved_mode = DockerExecutionMode.SEPARATE_USER if mode is None else mode
        self.host = _FakeHostSettings(auth, resolved_mode)


class TestInvoke:
    def _fake_hc(self) -> HostConfig:
        from core.host_config import MachinectlAuth

        return cast("HostConfig", _FakeHostConfig(MachinectlAuth.SUDO))

    def test_deterministic_op_crosses_boundary_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, "out", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        result = invoke("docker-info", ["runtimes"], self._fake_hc(), timeout=15)
        assert result.returncode == 0
        cmd = cast("list[str]", captured["cmd"])
        # SUDO separate-user rides the privileged byte-pipe (C-009 D2), NOT
        # machinectl shell — the prefix is sudo_pipe_cmd(user).
        assert cmd[:5] == ["sudo", "systemd-run", "-q", "--pipe", "--uid=sandbox"]
        assert cmd[5:7] == ["/bin/bash", "-c"]
        assert cmd[7] == (
            "/usr/local/libexec/sandbox-ai/dispatch docker-info runtimes"
        )
        assert cast("dict[str, object]", captured["kwargs"])["timeout"] == 15

    def test_nullary_op_has_no_trailing_space(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        invoke(Op.AUTH_PROBE, [], self._fake_hc())
        cmd = cast("list[str]", captured["cmd"])
        assert cmd[7] == "/usr/local/libexec/sandbox-ai/dispatch auth-probe"

    def test_compose_op_expands_wire_form_internally(
        self, isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        _seed_instance(isolated_sandbox_ai_home, "demo")
        proj = compose_project_name("demo")
        inst_dir = isolated_sandbox_ai_home / "instances" / "demo"
        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        invoke("compose-up", ["demo"], self._fake_hc())
        cmd = cast("list[str]", captured["cmd"])
        # The Q6 expansion is INTERNAL to invoke(); the caller passed only
        # ["demo"]. The crossed inner string carries the named-flag wire form.
        # (SUDO rides the byte-pipe, so the inner payload is at index 7.)
        assert cmd[7] == (
            f"/usr/local/libexec/sandbox-ai/dispatch compose-up demo "
            f"--project {proj} "
            f"--env-file {inst_dir / '.sandbox.env'} "
            f"--compose-file {inst_dir / 'docker' / 'compose.yml'}"
        )

    def test_compose_down_destroy_carries_volumes_in_wire(
        self, isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        _seed_instance(isolated_sandbox_ai_home, "demo")
        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        invoke("compose-down", ["demo", "--volumes"], self._fake_hc())
        cmd = cast("list[str]", captured["cmd"])
        assert cmd[7].endswith(" --volumes")

    def test_invoke_validates_before_crossing(self) -> None:
        # A malformed typed arg is rejected by validate_args BEFORE the
        # boundary is crossed (no Executor call).
        with pytest.raises(DispatchValidationError):
            invoke("docker-info", ["bogus-preset"], self._fake_hc())

    def test_invoke_unknown_op_raises(self) -> None:
        with pytest.raises(ValueError):
            invoke("not-a-real-op", [], self._fake_hc())

    def test_invoke_accepts_str_and_enum_op(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        assert invoke(Op.AUTH_PROBE, [], self._fake_hc()).returncode == 0
        assert invoke("auth-probe", [], self._fake_hc()).returncode == 0


# ─── build_invocation(): the single command-construction seam ───────────────
#
# Rule 7 evidence: invoke() is exactly Executor().run(build_invocation(...)),
# and the dry-run preview + ComposeUpAction render/execute from this same
# function — no parallel argv/inner/compose-state construction anywhere.


class TestBuildInvocation:
    def _fake_hc(self) -> HostConfig:
        from core.host_config import MachinectlAuth

        return cast("HostConfig", _FakeHostConfig(MachinectlAuth.SUDO))

    def test_builds_crossed_argv_without_executing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # build_invocation is PURE: it must not touch the Executor.
        def boom(self: object, *a: object, **k: object) -> object:
            raise AssertionError("build_invocation must not execute")

        monkeypatch.setattr("core.dispatch.Executor.run", boom)
        argv = build_invocation("docker-info", ["runtimes"], self._fake_hc())
        # SUDO separate-user rides the privileged byte-pipe (C-009 D2).
        assert argv[:5] == ["sudo", "systemd-run", "-q", "--pipe", "--uid=sandbox"]
        assert argv[5:7] == ["/bin/bash", "-c"]
        assert argv[7] == "/usr/local/libexec/sandbox-ai/dispatch docker-info runtimes"

    def test_validates_before_building(self) -> None:
        with pytest.raises(DispatchValidationError):
            build_invocation("docker-info", ["bogus-preset"], self._fake_hc())

    def test_unknown_op_raises(self) -> None:
        with pytest.raises(ValueError):
            build_invocation("not-a-real-op", [], self._fake_hc())

    def test_compose_wire_expansion_is_internal(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        proj = compose_project_name("demo")
        inst_dir = isolated_sandbox_ai_home / "instances" / "demo"
        argv = build_invocation("compose-up", ["demo"], self._fake_hc())
        assert argv[7] == (
            f"/usr/local/libexec/sandbox-ai/dispatch compose-up demo "
            f"--project {proj} "
            f"--env-file {inst_dir / '.sandbox.env'} "
            f"--compose-file {inst_dir / 'docker' / 'compose.yml'}"
        )

    def test_invoke_consumes_build_invocation_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # invoke() == Executor().run(build_invocation(...), framed=True,
        # timeout=...): the argv handed to Executor is byte-identical to
        # build_invocation's return, framed=True recovers the in-container exit
        # from the dispatcher's begin/exit nonce framing (machinectl shell does
        # not propagate it, and the sentinel is NOT injected into the crossed
        # payload so the per-op rule still matches — F-018), and timeout is
        # forwarded verbatim.
        import subprocess

        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        hc = self._fake_hc()
        invoke("docker-info", ["runtimes"], hc, timeout=15)
        assert captured["cmd"] == build_invocation("docker-info", ["runtimes"], hc)
        assert cast("dict[str, object]", captured["kwargs"]) == {
            "framed": True,
            "timeout": 15,
        }


# ─── C-009 §2: SUDO separate-user dispatch rides the privileged byte-pipe ────
#
# ─── SSOT crossing primitives (C-009 design D4) ─────────────────────────────
#
# ``dispatch_payload`` + ``sudo_pipe_crossing_argv`` are the single source the
# L3 sudoers ``Cmnd_Spec`` renderer AND ``build_invocation`` both derive from,
# so the authorized grant cannot drift from the actual crossing.


class TestSsotCrossingPrimitives:
    def test_dispatch_payload_no_wire(self) -> None:
        assert (
            dispatch_payload("auth-probe", [])
            == f"{_DISPATCH_BINARY} auth-probe"
        )

    def test_dispatch_payload_with_wire(self) -> None:
        assert (
            dispatch_payload("docker-info", ["runtimes"])
            == f"{_DISPATCH_BINARY} docker-info runtimes"
        )

    def test_sudo_pipe_crossing_argv_drops_sudo_and_abspaths_launcher(
        self,
    ) -> None:
        from core.host_config import sudo_pipe_cmd

        argv = sudo_pipe_crossing_argv("/usr/bin/systemd-run", "sandbox")
        # Leading ``sudo`` dropped; relative launcher (index 1) abspath'd;
        # every other token byte-identical to ``sudo_pipe_cmd``.
        assert argv == ["/usr/bin/systemd-run", "-q", "--pipe", "--uid=sandbox"]
        assert argv[1:] == sudo_pipe_cmd("sandbox")[2:]

    def test_build_invocation_inner_is_dispatch_payload(self) -> None:
        # build_invocation's inner uses dispatch_payload — the SSOT seam.
        argv = build_invocation("auth-probe", [], _sudo_hc())
        assert argv[-1] == dispatch_payload("auth-probe", [])


# build_invocation crosses the separate-user branch via ``sudo_pipe_cmd(user)``
# (the privileged byte-pipe; design D2). The inner ``dispatch <op> <wire>``
# payload and the operator-rootless branch are independent of the crossing.
# The pipe path keeps ``framed=True`` (design D3 / F-064): the inner exit is
# recovered from the dispatcher's ``__SANDBOX_EXIT_<nonce>`` frame, NOT the
# native ``--pipe`` exit.


def _sudo_hc() -> HostConfig:
    from core.host_config import MachinectlAuth

    return cast("HostConfig", _FakeHostConfig(MachinectlAuth.SUDO))


# Representative valid typed args for the eleven framed ops.
# The two compose ops with state and docker-manifest-inspect resolve their
# args lazily so the registry/instance fixtures are honored at call time.
def _valid_args_for(op: str) -> list[str]:
    busybox = IMAGE_REGISTRY["busybox_musl"].pinned
    table: dict[str, list[str]] = {
        "auth-probe": [],
        "compose-up": ["demo"],
        "compose-down": ["demo", "--volumes"],
        "compose-ps": ["demo"],
        "compose-ls": [],
        "docker-version": [],
        "docker-info": ["runtimes"],
        "docker-manifest-inspect": [busybox],
        "helper-chown-files": ["/srv/cache", "0644", "1000", "1000", "f1"],
        "helper-mkdir-chown-dirs": ["/srv/cache", "1000", "1000", "d1"],
        "preflight": [],
    }
    return table[op]


_COMPOSE_OPS = {"compose-up", "compose-down", "compose-ps"}
# The eleven FRAMED ops only — the build_invocation / invoke / probe surface.
# The streaming op (``fwd``) is excluded: it is never routed through
# build_invocation (invoke()/probe() reject it); its crossing argv is built by
# proxy_argv and exercised by TestFwdProxyArgv below.
_ALL_OPS = sorted(op.value for op in Op if op is not Op.FWD)


class TestSudoSeparateUserRidesPipe:
    def test_exactly_eleven_framed_ops_exercised(self) -> None:
        # Guard the framed-op surface: exactly the eleven framed ops (incl. the
        # read-only ``preflight`` bundle, excl. the streaming ``fwd``), and every
        # one has representative valid args wired into _valid_args_for.
        assert len(_ALL_OPS) == 11
        assert "fwd" not in _ALL_OPS
        assert all(isinstance(_valid_args_for(op), list) for op in _ALL_OPS)

    @pytest.mark.parametrize("op", _ALL_OPS)
    def test_sudo_emits_sudo_pipe_cmd_prefixed_argv(
        self, op: str, isolated_sandbox_ai_home: Path
    ) -> None:
        if op in _COMPOSE_OPS:
            _seed_instance(isolated_sandbox_ai_home, "demo")
        argv = build_invocation(op, _valid_args_for(op), _sudo_hc())
        # The crossing is the privileged byte-pipe (sudo_pipe_cmd), NOT
        # machinectl shell. Prefix is the 5-token ``sudo systemd-run --pipe``.
        assert argv[:5] == ["sudo", "systemd-run", "-q", "--pipe", "--uid=sandbox"]
        assert argv[5:7] == ["/bin/bash", "-c"]
        assert argv[7].startswith(f"/usr/local/libexec/sandbox-ai/dispatch {op}")
        assert "machinectl" not in argv

    @pytest.mark.parametrize("op", _ALL_OPS)
    def test_operator_rootless_argv_unchanged(
        self, op: str, isolated_sandbox_ai_home: Path
    ) -> None:
        # operator-rootless bypasses the dispatcher entirely: the bare op
        # target-argv with NO crossing prefix and NO dispatcher indirection —
        # untouched by the SUDO routing split.
        if op in _COMPOSE_OPS:
            _seed_instance(isolated_sandbox_ai_home, "demo")
        rootless = _rootless_hc()
        args = _valid_args_for(op)
        argv = build_invocation(op, args, rootless)
        wire = _expand_compose_wire(op, args) if op in _COMPOSE_OPS else args
        assert argv == build_target_argv(op, wire, rootless)
        for token in argv:
            assert "sudo" not in token
            assert "systemd-run" not in token
            assert "machinectl" not in token
            assert "/usr/local/libexec/sandbox-ai/dispatch" not in token

    def test_compose_op_wire_expansion_survives_the_pipe_swap(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        # Explicit Q6 wire-flag expansion check on the SUDO-pipe path: the
        # inner payload carries the named-flag wire form (the compose op is the
        # one path that expands typed args before crossing).
        _seed_instance(isolated_sandbox_ai_home, "demo")
        proj = compose_project_name("demo")
        inst_dir = isolated_sandbox_ai_home / "instances" / "demo"
        argv = build_invocation("compose-up", ["demo"], _sudo_hc())
        assert argv[7] == (
            f"/usr/local/libexec/sandbox-ai/dispatch compose-up demo "
            f"--project {proj} "
            f"--env-file {inst_dir / '.sandbox.env'} "
            f"--compose-file {inst_dir / 'docker' / 'compose.yml'}"
        )


class TestSudoPipePathKeepsFramedTrue:
    """invoke()/probe() keep ``framed=True`` on the SUDO byte-pipe path (D3)."""

    def test_invoke_runs_framed_true_on_sudo_pipe_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        hc = _sudo_hc()
        invoke("auth-probe", [], hc, timeout=15)
        # The SUDO crossing rides sudo_pipe_cmd …
        cmd = cast("list[str]", captured["cmd"])
        assert cmd[:5] == ["sudo", "systemd-run", "-q", "--pipe", "--uid=sandbox"]
        # … and STILL runs framed=True (the inner exit is recovered from the
        # dispatcher frame, not the native --pipe exit — F-064/D3).
        assert cast("dict[str, object]", captured["kwargs"]) == {
            "framed": True,
            "timeout": 15,
        }

    def test_recovered_nonzero_exit_surfaces_and_stdout_is_marker_free(
        self,
    ) -> None:
        # Observable framing contract the SUDO-pipe path inherits (it runs the
        # SAME Executor.run(..., framed=True)): a local argv that simulates the
        # dispatcher emitting a begin/exit frame around real op output is run
        # through the real Executor. A non-zero recovered exit must RAISE, and
        # the returned/raised stdout must be free of the __SANDBOX_BEGIN_ /
        # __SANDBOX_EXIT_ framing markers.
        from core.executor import Executor

        nonce = "deadbeefcafef00d"
        script = (
            f"echo __SANDBOX_BEGIN_{nonce}; "
            "echo real-op-line; "
            f"echo __SANDBOX_EXIT_{nonce}_7"
        )
        with pytest.raises(SandboxExecutionError) as excinfo:
            Executor().run(["/bin/bash", "-c", script], framed=True)
        msg = str(excinfo.value)
        # The recovered non-zero exit (7) surfaces in the fault message …
        assert "exit status 7" in msg
        # … the genuine op output is preserved …
        assert "real-op-line" in msg
        # … and the framing markers are stripped from the surfaced output.
        assert "__SANDBOX_BEGIN_" not in msg
        assert "__SANDBOX_EXIT_" not in msg

    def test_recovered_zero_exit_returns_marker_free_stdout(self) -> None:
        # The success arm of the same observable contract: a framed exit 0
        # returns CompletedProcess whose stdout carries the op output WITHOUT
        # the begin/exit markers.
        from core.executor import Executor

        nonce = "0123456789abcdef"
        script = (
            f"echo __SANDBOX_BEGIN_{nonce}; "
            "echo hello-from-op; "
            f"echo __SANDBOX_EXIT_{nonce}_0"
        )
        cp = Executor().run(["/bin/bash", "-c", script], framed=True)
        assert cp.returncode == 0
        assert "hello-from-op" in cp.stdout
        assert "__SANDBOX_BEGIN_" not in cp.stdout
        assert "__SANDBOX_EXIT_" not in cp.stdout


# ─── probe(): typed non-raising wrapper (Q8) ────────────────────────────────


class TestProbeOutcome:
    def test_is_frozen(self) -> None:
        import dataclasses

        outcome = ProbeOutcome(ok=True, timed_out=False, stdout="x", message="")
        field_name = "ok"
        with pytest.raises(dataclasses.FrozenInstanceError):
            # Frozen dataclass — the runtime setattr path is rejected. The
            # attribute name is a variable so the assertion exercises the real
            # frozen guard rather than a statically-rewritable attribute.
            setattr(outcome, field_name, False)


class TestProbe:
    def _fake_hc(self) -> HostConfig:
        from core.host_config import MachinectlAuth

        return cast("HostConfig", _FakeHostConfig(MachinectlAuth.SUDO))

    def test_success_returns_ok_with_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, "24.0.7\n", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        out = probe("docker-version", [], self._fake_hc(), timeout=15)
        assert out == ProbeOutcome(ok=True, timed_out=False, stdout="24.0.7\n", message="")
        # Success path: ``message`` is empty (no failure context to surface).
        assert out.message == ""

    def test_non_timeout_failure_returns_not_ok_not_timed_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def fake_run(self: object, cmd: list[str], **kwargs: object) -> object:
            err = SandboxExecutionError("[FATAL] boom")
            err.__cause__ = subprocess.CalledProcessError(returncode=1, cmd="x")
            raise err

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        out = probe("auth-probe", [], self._fake_hc())
        assert out == ProbeOutcome(ok=False, timed_out=False, stdout="", message="[FATAL] boom")
        # Failure path: ``message`` carries the ``str(SandboxExecutionError)``
        # so probe-style callers can restore informative diagnostics.
        assert out.message == "[FATAL] boom"

    def test_timeout_failure_sets_timed_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def fake_run(self: object, cmd: list[str], **kwargs: object) -> object:
            err = SandboxExecutionError("[FATAL] timed out")
            err.__cause__ = subprocess.TimeoutExpired(cmd="dispatch", timeout=10)
            raise err

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        out = probe("auth-probe", [], self._fake_hc(), timeout=10)
        assert out == ProbeOutcome(ok=False, timed_out=True, stdout="", message="[FATAL] timed out")
        # Failure path (timeout): ``message`` still carries the error text.
        assert out.message == "[FATAL] timed out"

    def test_validation_error_propagates_not_swallowed(self) -> None:
        # probe() only catches SandboxExecutionError; a pre-boundary
        # DispatchValidationError still raises (it is not an outcome).
        with pytest.raises(DispatchValidationError):
            probe("docker-info", ["bogus-preset"], self._fake_hc())


# ─── operator-rootless local invocation seam (C-003 Milestone C) ────────────
#
# When docker_execution_mode == operator-rootless the Go dispatcher is
# bypassed: build_invocation returns the bare op argv, invoke runs it locally
# with framed=False (native exit), emits a journald audit before the spawn,
# and normalizes stdout via the same shared helper the separate-user path uses.


def _rootless_hc() -> HostConfig:
    from core.host_config import DockerExecutionMode, MachinectlAuth

    return cast(
        "HostConfig",
        _FakeHostConfig(MachinectlAuth.SUDO, DockerExecutionMode.OPERATOR_ROOTLESS),
    )


class TestOperatorRootlessBuildInvocation:
    def test_deterministic_op_emits_bare_argv(self) -> None:
        # auth-probe is a deterministic op (no host_config / state needed). In
        # rootless mode build_invocation returns the op's target-argv directly.
        argv = build_invocation("auth-probe", [], _rootless_hc())
        assert argv == build_target_argv("auth-probe", [], _rootless_hc())
        assert argv[:2] == ["/bin/bash", "-c"]
        # No crossing prefix, no dispatcher indirection.
        for token in argv:
            assert "sudo" not in token
            assert "machinectl" not in token
            assert "systemd-run" not in token
            assert "/usr/local/libexec/sandbox-ai/dispatch" not in token

    def test_compose_op_emits_bare_argv_with_wire_expansion(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        # The compose op still runs the Q6 wire-expansion (resolving
        # project/compose-file/env-file from dev context) before the pure
        # builder — only the crossing prefix is dropped.
        _seed_instance(isolated_sandbox_ai_home, "demo")
        rootless = _rootless_hc()
        argv = build_invocation("compose-up", ["demo"], rootless)
        wire = _expand_compose_wire("compose-up", ["demo"])
        assert argv == build_target_argv("compose-up", wire, rootless)
        assert argv[:2] == ["/bin/bash", "-c"]
        for token in argv:
            assert "machinectl" not in token
            assert "/usr/local/libexec/sandbox-ai/dispatch" not in token
        # The inner string is the real local `docker compose ... up` command.
        assert argv[2].startswith("TERM=dumb NO_COLOR=1")
        assert " up -d --build --wait" in argv[2]

    def test_separate_user_build_invocation_crosses_dispatcher(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        # Same compose op in separate-user mode still routes through the Go
        # dispatcher with a bare `dispatch <op>` payload. _sudo_hc() is
        # SUDO, so the crossing rides the privileged byte-pipe (C-009 D2),
        # NOT machinectl shell.
        _seed_instance(isolated_sandbox_ai_home, "demo")
        argv = build_invocation("compose-up", ["demo"], _sudo_hc())
        assert argv[:5] == ["sudo", "systemd-run", "-q", "--pipe", "--uid=sandbox"]
        assert argv[5:7] == ["/bin/bash", "-c"]
        assert argv[7].startswith("/usr/local/libexec/sandbox-ai/dispatch compose-up demo")


class TestOperatorRootlessInvoke:
    def test_non_zero_local_exit_raises_and_probe_branches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(self: object, cmd: list[str], **kwargs: object) -> object:
            raise SandboxExecutionError("[FATAL] local boom")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        with pytest.raises(SandboxExecutionError):
            invoke("auth-probe", [], _rootless_hc())
        out = probe("auth-probe", [], _rootless_hc())
        assert out == ProbeOutcome(
            ok=False, timed_out=False, stdout="", message="[FATAL] local boom"
        )

    def test_local_timeout_discriminated_by_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def fake_run(self: object, cmd: list[str], **kwargs: object) -> object:
            err = SandboxExecutionError("[FATAL] local timed out")
            err.__cause__ = subprocess.TimeoutExpired(cmd="docker", timeout=10)
            raise err

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        out = probe("auth-probe", [], _rootless_hc(), timeout=10)
        assert out.ok is False
        assert out.timed_out is True

    def test_validation_runs_before_local_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import Mock

        run_mock = Mock()
        monkeypatch.setattr("core.dispatch.Executor.run", run_mock)
        with pytest.raises(DispatchValidationError):
            invoke("docker-info", ["bogus-preset"], _rootless_hc())
        run_mock.assert_not_called()

    def test_runs_framed_false_with_native_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        rootless = _rootless_hc()
        result = invoke("auth-probe", [], rootless, timeout=15)
        assert result.returncode == 0
        assert captured["cmd"] == build_invocation("auth-probe", [], rootless)
        assert cast("dict[str, object]", captured["kwargs"]) == {
            "framed": False,
            "timeout": 15,
        }


class TestOperatorRootlessStdoutNormalization:
    _RAW = "line1\r\n\x1b[31mred\x1b[0m\n\n\n\ntail\r\n"

    def test_stdout_normalized_identically_in_rootless_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from core.executor import normalize_captured_output

        raw = self._RAW

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, raw, "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        out = invoke("auth-probe", [], _rootless_hc())
        assert out.stdout == normalize_captured_output(raw)
        assert "\r" not in out.stdout
        assert "\x1b[" not in out.stdout
        assert "\n\n\n" not in out.stdout

    def test_probe_inherits_rootless_normalization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from core.executor import normalize_captured_output

        raw = self._RAW

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, raw, "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        out = probe("auth-probe", [], _rootless_hc())
        assert out.ok is True
        assert out.stdout == normalize_captured_output(raw)

    def test_executor_default_framed_false_path_not_globally_altered(self) -> None:
        # The normalization lives in core.dispatch, NOT in Executor.run's
        # default framed=False path — a direct Executor call returns raw output.
        # (ANSI + 3+-newline runs are chosen as the probe: text-mode universal
        # newlines already fold CRLF, so CR is not a faithful raw witness, but
        # ANSI escapes and blank-line runs survive iff no normalization runs.)
        from core.executor import Executor

        raw = "\x1b[31mred\x1b[0m\n\n\n\ntail\n"
        result = Executor().run(["printf", "%s", raw], framed=False)
        assert "\x1b[31m" in result.stdout
        assert "\n\n\n" in result.stdout


class TestOperatorRootlessAudit:
    def test_audit_emitted_before_subprocess_with_op_and_instance(
        self, isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess
        from unittest.mock import Mock

        _seed_instance(isolated_sandbox_ai_home, "demo")
        manager = Mock()

        def fake_emit(
            op: str, args: object, target_argv: object, instance: str
        ) -> None:
            manager.emit(op, list(cast("list[str]", args)), instance)

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            manager.run(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.emit_op_audit", fake_emit)
        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        invoke("compose-up", ["demo"], _rootless_hc())
        names = [c[0] for c in manager.mock_calls]
        assert names.index("emit") < names.index("run")
        emit_call = next(c for c in manager.mock_calls if c[0] == "emit")
        assert emit_call.args[0] == "compose-up"
        assert emit_call.args[1] == ["demo"]
        assert emit_call.args[2] == "demo"

    def test_deterministic_op_audit_has_empty_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        captured: dict[str, object] = {}

        def fake_emit(
            op: str, args: object, target_argv: object, instance: str
        ) -> None:
            captured["op"] = op
            captured["instance"] = instance

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.emit_op_audit", fake_emit)
        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        invoke("auth-probe", [], _rootless_hc())
        assert captured["op"] == "auth-probe"
        assert captured["instance"] == ""

    def test_separate_user_mode_emits_no_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess
        from unittest.mock import Mock

        emit_mock = Mock()

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.emit_op_audit", emit_mock)
        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        invoke("auth-probe", [], _sudo_hc())
        emit_mock.assert_not_called()


class TestOperatorRootlessPreflightNonce:
    """H-1 local path: on operator-rootless there is no dispatcher to mint the
    nonce, so ``_invoke_with_nonce`` mints one locally and injects
    ``__PFNONCE=<nonce>; `` onto the bundle inner — AFTER the clean-argv audit —
    then surfaces it so the parser can bind the markers."""

    def test_preflight_mints_and_injects_pfnonce_onto_inner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import re as _re
        import subprocess

        captured: dict[str, object] = {}

        def fake_run(self: object, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured["argv"] = list(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.emit_op_audit", lambda *a, **k: None)
        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        cp, nonce = _invoke_with_nonce("preflight", [], _rootless_hc())
        assert cp.returncode == 0
        # A 16-hex-char nonce was minted and surfaced.
        assert nonce is not None and _re.fullmatch(r"[0-9a-f]{16}", nonce)
        # The bash inner (argv[-1]) was prefixed with ``__PFNONCE=<nonce>; `` so
        # the ${__PFNONCE} markers expand to it at shell time.
        inner = cast("list[str]", captured["argv"])[-1]
        assert inner.startswith(f"__PFNONCE={nonce}; ")
        assert "${__PFNONCE}" in inner  # the template token is still present pre-expansion

    def test_clean_argv_audited_before_nonce_injection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        audited: dict[str, object] = {}

        def fake_emit(op: str, args: object, target_argv: object, instance: str) -> None:
            audited["argv"] = list(cast("list[str]", target_argv))

        def fake_run(self: object, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.emit_op_audit", fake_emit)
        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        _invoke_with_nonce("preflight", [], _rootless_hc())
        # journald logged the CLEAN argv — no minted nonce leaked into the audit.
        audited_inner = cast("list[str]", audited["argv"])[-1]
        assert "__PFNONCE=" not in audited_inner

    def test_non_preflight_op_mints_no_nonce(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def fake_run(self: object, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.emit_op_audit", lambda *a, **k: None)
        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        _cp, nonce = _invoke_with_nonce("auth-probe", [], _rootless_hc())
        assert nonce is None

    def test_healthy_local_preflight_parses_all_five_segments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OP-ROOTLESS 5-SEGMENT REGRESSION GUARD: a healthy local preflight (the
        # Python-minted nonce path) parses all five segments ok, so start does
        # NOT false-abort on the 64/64-green operator-rootless path. We simulate
        # the shell by expanding ${__PFNONCE} against the injected assignment.
        import re as _re
        import subprocess

        def fake_run(self: object, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            inner = cmd[-1]
            m = _re.match(r"__PFNONCE=([0-9a-f]{16}); ", inner)
            assert m is not None
            n = m.group(1)
            names = (
                "auth-probe",
                "docker-version",
                "docker-info-security-options",
                "docker-info-runtimes",
                "compose-ls",
            )
            stdout = "\n".join(
                f"__PREFLIGHT_Q_{n}_{name}__\nbody-{name}\n__PREFLIGHT_RC_{n}_{name}_0__" for name in names
            )
            return subprocess.CompletedProcess(cmd, 0, stdout, "")

        monkeypatch.setattr("core.dispatch.emit_op_audit", lambda *a, **k: None)
        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        outcome = probe("preflight", [], _rootless_hc())
        per = parse_preflight_outcome(outcome)
        assert len(per) == 5
        assert all(o.ok for o in per.values())
        assert per["compose-ls"].stdout == "body-compose-ls"


# ─── compile_dispatcher(): offline reproducible compile recipe ──────────────


_GOLANG_PINNED = IMAGE_REGISTRY["golang_alpine"].pinned


def _fake_binary_b64() -> str:
    """A captured-stdout payload: base64 of a fake ELF (what the crossing emits)."""
    import base64

    return base64.b64encode(b"\x7fELF-fake-binary").decode("ascii")


class TestDispatchSourceB64:
    """``_dispatch_source_b64()``: the gzip+base64 source-tar producer."""

    def test_decodes_to_a_tar_with_the_full_source_tree(self) -> None:
        import base64
        import io
        import tarfile

        from core.dispatch import _DISPATCH_SOURCE_ENTRIES, _dispatch_source_b64

        b64 = _dispatch_source_b64()
        # ASCII, base64 alphabet only — safe to interpolate into a single-
        # quoted shell literal (no shell metacharacters).
        assert b64.isascii()
        assert set(b64) <= set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
        )
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64)), mode="r:gz") as tar:
            names = set(tar.getnames())
        # The full source tree is present, including the fixtures/ corpus the
        # in-container `go test ./...` consumes for the C-e parity check.
        assert "main.go" in names
        assert "main_test.go" in names
        assert "go.mod" in names
        assert "go.sum" in names
        assert any(n == "vendor" or n.startswith("vendor/") for n in names)
        assert "vendor/modules.txt" in names
        assert "fixtures/target_argv_cases.json" in names
        # Exactly the declared top-level entries (hermetic — no stray files).
        top = {n.split("/", 1)[0] for n in names}
        assert top == set(_DISPATCH_SOURCE_ENTRIES)

    def test_is_byte_deterministic_and_carries_fixture_content(self) -> None:
        import base64
        import io
        import tarfile

        from core.dispatch import _dispatch_source_b64

        # Two producer calls yield byte-identical output (mtime/uid/gid/mode
        # normalised) — the embedded payload contributes nothing host- or
        # time-specific to the V7/J reproducibility contract.
        assert _dispatch_source_b64() == _dispatch_source_b64()
        with tarfile.open(
            fileobj=io.BytesIO(base64.b64decode(_dispatch_source_b64())), mode="r:gz"
        ) as tar:
            member = tar.extractfile("fixtures/target_argv_cases.json")
            assert member is not None
            assert member.read() == _FIXTURE_PATH.read_bytes()
            info = tar.getmember("fixtures/target_argv_cases.json")
            assert info.mtime == 0
            assert info.uid == 0
            assert info.gid == 0


class TestCompilePayload:
    """``_compile_payload()``: the ephemeral-build-dir crossed bash payload."""

    def _payload(self) -> str:
        from core.dispatch import _compile_payload

        return _compile_payload(_GOLANG_PINNED, "QUJDPT0=")

    def test_payload_mktemps_under_per_user_runtime_dir(self) -> None:
        payload = self._payload()
        # Ephemeral per-call build dir under the lingering daemon user's
        # per-user runtime dir /run/user/<uid> (created by systemd-logind
        # independent of any login session, so reachable under the
        # PAM-skipping pipe_cmd crossing where $XDG_RUNTIME_DIR is unset). A
        # fail-closed [ -d "$RD" ] guard makes an absent runtime dir exit
        # non-zero.
        assert 'RD="/run/user/$(id -u)"' in payload
        assert (
            '[ -d "$RD" ] || { echo "sandbox-ai: per-user runtime dir $RD absent '
            '(is the daemon user lingering? sister-change L5 enables linger)" 1>&2; exit 1; }'
            in payload
        )
        assert 'DIR="$(mktemp -d "$RD/sandbox-ai-build-XXXXXX")"' in payload
        # The guard precedes the mktemp (fail-closed before any work).
        assert payload.index('[ -d "$RD" ]') < payload.index('mktemp -d "$RD/')

    def test_payload_arms_exit_trap_before_any_work(self) -> None:
        payload = self._payload()
        assert "trap 'rm -rf \"$DIR\"' EXIT;" in payload
        # The trap is armed BEFORE the source-decode, docker run, and capture.
        assert payload.index("trap 'rm -rf") < payload.index("base64 -d")
        assert payload.index("trap 'rm -rf") < payload.index("docker run")
        assert payload.index("trap 'rm -rf") < payload.index('base64 -w0 "$DIR/dispatch"')

    def test_payload_decodes_embedded_source_into_the_build_dir(self) -> None:
        payload = self._payload()
        assert "printf %s 'QUJDPT0=' | base64 -d | tar -xz -C \"$DIR\" 1>&2;" in payload

    def test_payload_runs_offline_pinned_single_docker_run(self) -> None:
        payload = self._payload()
        assert payload.count("docker run") == 1
        assert "--network none" in payload
        assert _GOLANG_PINNED in payload
        assert "@sha256:" in _GOLANG_PINNED
        # The mutable tag form must NOT appear.
        assert "golang:1.23-alpine " not in payload
        assert f"{IMAGE_REGISTRY['golang_alpine'].ref}:" not in payload
        # Bind-src is the ephemeral $DIR (NOT a host-named build dir); the
        # container always sees the fixed mount target so -trimpath keeps the
        # build location out of the binary (location-neutral reproducibility).
        assert '--mount type=bind,src="$DIR",dst=/build' in payload
        assert "--workdir /build" in payload

    def test_payload_delivers_goflags_into_the_container_only(self) -> None:
        payload = self._payload()
        # Vendored-deps GOFLAGS via `docker run --env` (into the build
        # container), NOT a host-side prefix that never reaches the build.
        assert "--env GOFLAGS=-mod=vendor" in payload
        assert "GOFLAGS=-mod=vendor docker run" not in payload
        assert payload.index("--env GOFLAGS=-mod=vendor") < payload.index(_GOLANG_PINNED)

    def test_payload_runs_test_then_build_in_one_container(self) -> None:
        from core.dispatch import _COMPILE_INNER

        # C-e: `go test ./...` strictly precedes `go build`, joined by `&&` in
        # ONE docker run — a parity failure fails go test, the && short-
        # circuits, no binary is produced.
        assert _COMPILE_INNER == (
            "go test ./... && "
            "go build -trimpath -ldflags '-s -w' -o /build/dispatch ."
        )
        payload = self._payload()
        assert "go test ./..." in payload
        assert "go build" in payload
        assert "/bin/sh -c " in payload
        assert payload.index("go test ./...") < payload.index("go build")
        assert "-trimpath" in payload
        assert "-s -w" in payload
        assert "-o /build/dispatch ." in payload

    def test_payload_redirects_chatter_to_stderr_and_emits_only_binary(self) -> None:
        payload = self._payload()
        # docker/go chatter -> stderr (genuinely distinct under pipe_cmd's
        # real byte pipe, no PTY where stdout ≡ stderr); stdout carries ONLY
        # the binary base64, as the LAST thing the payload does.
        assert "tar -xz -C \"$DIR\" 1>&2;" in payload
        assert "/bin/sh -c " in payload
        assert " 1>&2; " in payload
        assert payload.rstrip().endswith('base64 -w0 "$DIR/dispatch"')


class TestCompileDispatcher:
    """Group 4: the docker-based offline reproducible compile recipe.

    All tests mock ``Executor.run`` — no real docker/pipe_cmd is executed.
    The signature is ``compile_dispatcher(output_path, host_config)``: the
    build dir is derived inside the crossing (the lingering daemon user's
    /run/user/$(id -u), reachable under the PAM-skipping pipe_cmd crossing
    where $XDG_RUNTIME_DIR is unset)
    and never supplied/seen host-side; the built binary returns over captured
    stdout as ``base64 -w0`` and the host decodes + writes ``output_path``.
    The crossing is :func:`~core.host_config.pipe_cmd` (binary-frame transport,
    no PTY) — ``Executor().run`` is called WITHOUT ``sentinel=True`` because
    ``systemd-run --pipe`` propagates the inner exit (``check=True`` raises).
    """

    def _fake_hc(self) -> HostConfig:
        from core.host_config import MachinectlAuth

        return cast("HostConfig", _FakeHostConfig(MachinectlAuth.SUDO))

    def _capture_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[str, dict[str, object]]:
        import subprocess

        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, _fake_binary_b64(), "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        compile_dispatcher(str(tmp_path / "out"), self._fake_hc())
        cmd = cast("list[str]", captured["cmd"])
        # The whole embed-source/build/capture script is the bash -c payload.
        return cmd[-1], captured

    def test_crosses_boundary_via_pipe_cmd_binary_frame_transport(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, captured = self._capture_cmd(tmp_path, monkeypatch)
        cmd = cast("list[str]", captured["cmd"])
        # Crossed via pipe_cmd (binary-frame transport, no PTY) — NOT
        # machinectl_cmd. pipe_cmd takes only the unprivileged docker user
        # (auth-mode-independent: no machinectl auth in the prefix).
        assert cmd[:4] == ["systemd-run", "-q", "--pipe", "--uid=sandbox"]
        assert cmd[4:6] == ["/bin/bash", "-c"]
        # No machinectl/sudo anywhere in the crossing.
        assert "machinectl" not in cmd
        assert "sudo" not in cmd

    def test_payload_is_the_ephemeral_embed_capture_script(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inner, _ = self._capture_cmd(tmp_path, monkeypatch)
        # The crossed payload is exactly _compile_payload(...) over the real
        # source tar — the embedded literal must be the actual base64 source.
        from core.dispatch import _compile_payload, _dispatch_source_b64

        assert inner == _compile_payload(_GOLANG_PINNED, _dispatch_source_b64())
        assert 'RD="/run/user/$(id -u)"' in inner
        assert '[ -d "$RD" ] ||' in inner
        assert 'mktemp -d "$RD/sandbox-ai-build-XXXXXX"' in inner
        assert "trap 'rm -rf \"$DIR\"' EXIT;" in inner
        assert "--network none" in inner
        assert '--mount type=bind,src="$DIR",dst=/build' in inner
        assert inner.rstrip().endswith('base64 -w0 "$DIR/dispatch"')

    def test_compile_crosses_via_unprivileged_pipe_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from core.host_config import MachinectlAuth

        # compile_dispatcher crosses via the unprivileged ``pipe_cmd`` byte-pipe
        # (the multi-MB binary frame demands it) — the systemd-run prefix with
        # no sudo and no machinectl.
        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, _fake_binary_b64(), "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        hc = cast("HostConfig", _FakeHostConfig(MachinectlAuth.SUDO))
        compile_dispatcher(str(tmp_path / "out"), hc)
        cmd = cast("list[str]", captured["cmd"])
        assert cmd[:4] == ["systemd-run", "-q", "--pipe", "--uid=sandbox"]
        assert "machinectl" not in cmd
        assert "sudo" not in cmd

    def test_successful_compile_writes_decoded_binary_mode_0755(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import base64
        import stat
        import subprocess

        output_path = tmp_path / "out" / "dispatch"
        output_path.parent.mkdir()
        # The crossing returns ONLY base64 of the built binary on stdout.
        # pipe_cmd has no PTY (no onlcr \r), but the host .strip()s any
        # trailing whitespace/newline before decoding regardless — assert
        # that robustness by appending stray \r\n here.
        stdout = base64.b64encode(b"\x7fELF-real").decode("ascii") + "\r\n"

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout, "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        compile_dispatcher(str(output_path), self._fake_hc())
        assert output_path.read_bytes() == b"\x7fELF-real"
        assert stat.S_IMODE(output_path.stat().st_mode) == 0o755

    def test_failure_raises_and_writes_no_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-zero `go test` (fixture drift), build failure, or absent
        # /run/user/$(id -u) (the [ -d "$RD" ] guard): the sterile Executor
        # raises SandboxExecutionError
        # BEFORE the decode+write, so output_path is never created.
        output_path = tmp_path / "out" / "dispatch"

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> object:
            raise SandboxExecutionError(
                "[FATAL] Sandbox Execution Fault: Inner command failed with exit status 1."
            )

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        with pytest.raises(SandboxExecutionError):
            compile_dispatcher(str(output_path), self._fake_hc())
        assert not output_path.exists()

    def test_run_uses_no_sentinel_pipe_cmd_propagates_inner_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, _fake_binary_b64(), "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        compile_dispatcher(str(tmp_path / "out"), self._fake_hc())
        # pipe_cmd (systemd-run --pipe) propagates the inner /bin/bash -c exit,
        # so the crossing runs with the DEFAULT sentinel=False (no sentinel
        # echo): Executor's check=True raises on any non-zero exit. The call
        # site passes no sentinel kwarg and no positional sentinel arg at all.
        kwargs = cast("dict[str, object]", captured["kwargs"])
        assert "sentinel" not in kwargs
        assert kwargs.get("sentinel", False) is False
        assert cast("tuple[object, ...]", captured["args"]) == ()


# ─── parse_preflight_outcome (C-009 4.5a) ────────────────────────────────────


_TEST_PFNONCE = "feedface00c0ffee"


def _synthetic_bundle(segments: list[tuple[str, str, int]], *, nonce: str = _TEST_PFNONCE) -> str:
    """Build a preflight-bundle stdout from (name, segment-stdout, rc) triples.

    Mirrors ``_preflight_inner``'s on-the-wire shape EXACTLY: per query a begin
    marker line, the (stderr-merged) segment, then the per-query RC marker, with
    every marker bound to ``nonce`` (H-1) — the same nonce the caller sets on the
    synthetic outcome's ``preflight_nonce`` so :func:`parse_preflight_outcome`
    can verify it.
    """
    parts = []
    for name, body, rc in segments:
        parts.append(f"__PREFLIGHT_Q_{nonce}_{name}__\n{body}\n__PREFLIGHT_RC_{nonce}_{name}_{rc}__")
    return "\n".join(parts)


def _bundle_outcome(segments: list[tuple[str, str, int]], *, nonce: str = _TEST_PFNONCE) -> ProbeOutcome:
    """A healthy-crossing preflight :class:`ProbeOutcome` carrying ``nonce``."""
    return ProbeOutcome(
        ok=True, timed_out=False, stdout=_synthetic_bundle(segments, nonce=nonce), message="", preflight_nonce=nonce
    )


class TestParsePreflightOutcome:
    def test_all_ok_splits_each_segment(self) -> None:
        outcome = _bundle_outcome(
            [
                ("auth-probe", "ok", 0),
                ("docker-version", "29.5.3", 0),
                ("docker-info-security-options", "[name=rootless]", 0),
                ("docker-info-runtimes", '{"sandbox-ai-runsc": {}}', 0),
                ("compose-ls", "[]", 0),
            ]
        )
        per = parse_preflight_outcome(outcome)
        assert set(per) == {
            "auth-probe",
            "docker-version",
            "docker-info-security-options",
            "docker-info-runtimes",
            "compose-ls",
        }
        assert all(o.ok for o in per.values())
        assert per["docker-version"].stdout == "29.5.3"
        assert per["docker-info-runtimes"].stdout == '{"sandbox-ai-runsc": {}}'
        assert all(o.message == "" for o in per.values())

    def test_one_query_nonzero_rc_is_not_ok(self) -> None:
        outcome = _bundle_outcome(
            [
                ("auth-probe", "ok", 0),
                ("docker-version", "29.5.3", 0),
                ("docker-info-security-options", "[name=rootless]", 0),
                ("docker-info-runtimes", "Cannot connect to the Docker daemon", 1),
                ("compose-ls", "[]", 0),
            ]
        )
        per = parse_preflight_outcome(outcome)
        failed = per["docker-info-runtimes"]
        assert failed.ok is False
        assert failed.stdout == ""  # not-ok segments surface no stdout
        assert "docker-info-runtimes" in failed.message
        assert "1" in failed.message
        # the other segments are unaffected (`;`-isolation)
        assert per["auth-probe"].ok is True
        assert per["compose-ls"].ok is True

    def test_missing_segment_is_not_ok(self) -> None:
        # ``compose-ls`` segment entirely absent (a truncated / garbled bundle).
        outcome = _bundle_outcome(
            [
                ("auth-probe", "ok", 0),
                ("docker-version", "29.5.3", 0),
                ("docker-info-security-options", "[name=rootless]", 0),
                ("docker-info-runtimes", "{}", 0),
            ]
        )
        per = parse_preflight_outcome(outcome)
        assert per["compose-ls"].ok is False
        assert per["compose-ls"].stdout == ""
        assert "compose-ls" in per["compose-ls"].message
        assert "missing" in per["compose-ls"].message

    def test_garbled_rc_token_is_not_ok(self) -> None:
        # RC marker present but the recovered exit code is not an int.
        bundle = f"__PREFLIGHT_Q_{_TEST_PFNONCE}_auth-probe__\nok\n__PREFLIGHT_RC_{_TEST_PFNONCE}_auth-probe_X__"
        outcome = ProbeOutcome(ok=True, timed_out=False, stdout=bundle, message="", preflight_nonce=_TEST_PFNONCE)
        per = parse_preflight_outcome(outcome)
        assert per["auth-probe"].ok is False
        assert "unparseable" in per["auth-probe"].message

    def test_timed_out_propagates_to_every_segment(self) -> None:
        # A whole-crossing timeout has no bundle; every segment is missing AND
        # carries the whole-crossing timed_out flag (per-query timeout is not
        # meaningful in a bundle).
        outcome = ProbeOutcome(ok=False, timed_out=True, stdout="", message="timed out")
        per = parse_preflight_outcome(outcome)
        assert all(o.timed_out for o in per.values())
        assert all(not o.ok for o in per.values())

    def test_uses_real_inner_marker_format(self) -> None:
        # Guard the SSOT contract: a bundle built by interpolating the REAL
        # ``_preflight_inner`` marker positions parses cleanly. We render the
        # inner, locate its echo markers, and confirm the parser's marker
        # derivation matches what the op emits (no hand-typed marker drift).
        inner = _preflight_inner()
        # the inner contains begin+rc echo commands per query; assert the parser
        # finds every query the inner names.
        for name in (
            "auth-probe",
            "docker-version",
            "docker-info-security-options",
            "docker-info-runtimes",
            "compose-ls",
        ):
            assert f"echo __PREFLIGHT_Q_${{__PFNONCE}}_{name}__" in inner
        outcome = _bundle_outcome([(name, "x", 0) for name in (
            "auth-probe",
            "docker-version",
            "docker-info-security-options",
            "docker-info-runtimes",
            "compose-ls",
        )])
        per = parse_preflight_outcome(outcome)
        assert all(o.ok for o in per.values())


class TestParsePreflightNonceBinding:
    """H-1: per-query verdicts are bound to the per-crossing nonce.

    Untrusted op output cannot forge a verdict by echoing a byte-perfect marker
    copy — it cannot learn the nonce — and the parser is uniformly fail-closed
    (nonce-absent, sequential scan, reject-duplicate).
    """

    _NAMES = (
        "auth-probe",
        "docker-version",
        "docker-info-security-options",
        "docker-info-runtimes",
        "compose-ls",
    )

    def test_nonce_bound_markers_match_and_split(self) -> None:
        # A bundle whose markers carry the SAME nonce the outcome surfaces parses
        # cleanly with every verdict recovered (positive nonce-bound match).
        outcome = _bundle_outcome([(n, f"body-{n}", 0) for n in self._NAMES])
        per = parse_preflight_outcome(outcome)
        assert all(o.ok for o in per.values())
        assert per["docker-version"].stdout == "body-docker-version"

    def test_nonce_absent_fails_every_query_closed(self) -> None:
        # No surfaced nonce (the local/framed path did not mint/recover one): no
        # marker we emit can be trusted, so every query is not-ok regardless of
        # the bundle bytes. Build the blob with a plausible-but-unverifiable nonce.
        bundle = _synthetic_bundle([(n, "ok", 0) for n in self._NAMES], nonce="deadbeefdeadbeef")
        outcome = ProbeOutcome(ok=True, timed_out=False, stdout=bundle, message="", preflight_nonce=None)
        per = parse_preflight_outcome(outcome)
        assert all(not o.ok for o in per.values())
        assert all("nonce absent" in o.message for o in per.values())

    def test_forged_marker_without_nonce_is_not_matched(self) -> None:
        # FORGE-REJECTION (the H-1 vector): the ``auth-probe`` query FAILS (rc=1)
        # and its segment stdout contains a byte-perfect copy of the
        # ``docker-rootless`` PASS markers — BUT spelled with the WRONG (fixed,
        # pre-H-1) marker form that omits the per-crossing nonce. Because the
        # parser derives every marker from ``outcome.preflight_nonce``, the forged
        # bytes are NOT recognised: the real (nonce-bound) docker-info-security
        # verdict is the only one matched, and the forgery cannot flip a verdict.
        #
        # Pre-fix verification protocol (CLAUDE.md): against the pre-H-1 parser
        # (fixed marker form ``__PREFLIGHT_Q_<name>__`` with NO nonce), the forged
        # ``docker-info-security-options`` markers in the attacker-controlled
        # auth-probe segment WERE recognised as a real occurrence of that query —
        # so the parser read the forged ``[name=rootless]`` / ``RC_0`` and the
        # ``docker-info-security-options`` verdict flipped FAIL→PASS (observed:
        # ``per["docker-info-security-options"].ok`` was True with stdout
        # ``[name=rootless]``). Binding every marker to ``outcome.preflight_nonce``
        # makes the forged nonce-less bytes unrecognised, so the REAL (FAIL)
        # verdict stands — proving the test catches the verdict-forgery vector.
        forged = (
            "__PREFLIGHT_Q_docker-info-security-options__\n"
            "[name=rootless]\n"
            "__PREFLIGHT_RC_docker-info-security-options_0__"
        )
        segments = [
            ("auth-probe", forged, 1),  # attacker-controlled failing segment
            ("docker-version", "29.5.3", 0),
            ("docker-info-security-options", "[name=seccomp]", 1),  # the REAL verdict: FAIL
            ("docker-info-runtimes", "{}", 0),
            ("compose-ls", "[]", 0),
        ]
        outcome = _bundle_outcome(segments)
        per = parse_preflight_outcome(outcome)
        # The forged nonce-less marker did NOT register as a second occurrence of
        # the real (nonce-bound) marker, so the query is unambiguous AND its REAL
        # verdict (FAIL) stands — the forgery did not flip it to PASS.
        assert per["docker-info-security-options"].ok is False
        assert per["docker-info-security-options"].stdout == ""
        # The attacker's own (failing) auth-probe segment is faithfully not-ok.
        assert per["auth-probe"].ok is False

    def test_duplicate_nonce_bound_marker_is_ambiguous(self) -> None:
        # Layer 2: even a correctly-nonce'd marker, if it appears twice, makes the
        # query ambiguous (a daemon that echoed the live nonce back). The parser
        # rejects it rather than guess which occurrence is authoritative.
        nonce = _TEST_PFNONCE
        good = _synthetic_bundle([(n, "ok", 0) for n in self._NAMES], nonce=nonce)
        dup_begin = f"__PREFLIGHT_Q_{nonce}_auth-probe__"
        blob = good + "\n" + dup_begin + "\n"
        outcome = ProbeOutcome(ok=True, timed_out=False, stdout=blob, message="", preflight_nonce=nonce)
        per = parse_preflight_outcome(outcome)
        assert per["auth-probe"].ok is False
        assert "more than once" in per["auth-probe"].message

    def test_sequential_scan_does_not_skip_to_a_later_marker(self) -> None:
        # Layer 1: a query's begin marker is searched strictly AFTER the prior
        # query's RC index. A correctly-nonce'd begin marker for ``compose-ls``
        # planted INSIDE the auth-probe segment (before auth-probe's RC) must not
        # let the scan jump ahead and mis-attribute the later real compose-ls.
        nonce = _TEST_PFNONCE
        planted = f"__PREFLIGHT_Q_{nonce}_compose-ls__\nSPOOFED\n__PREFLIGHT_RC_{nonce}_compose-ls_0__"
        segments = [
            ("auth-probe", planted, 0),
            ("docker-version", "29.5.3", 0),
            ("docker-info-security-options", "[name=rootless]", 0),
            ("docker-info-runtimes", "{}", 0),
            ("compose-ls", "REAL", 0),
        ]
        outcome = _bundle_outcome(segments)
        per = parse_preflight_outcome(outcome)
        # The planted compose-ls marker is a SECOND occurrence ⇒ Layer 2 flags the
        # query ambiguous rather than letting the spoof win.
        assert per["compose-ls"].ok is False
        assert "more than once" in per["compose-ls"].message
