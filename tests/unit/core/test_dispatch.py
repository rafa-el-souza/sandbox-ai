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
from core.dispatch import (
    OP_SPECS,
    DispatchValidationError,
    Op,
    OpSpec,
    ProbeOutcome,
    _expand_compose_wire,
    build_target_argv,
    compile_dispatcher,
    invoke,
    probe,
    validate_args,
)
from core.exceptions import SandboxExecutionError
from core.hydration import IMAGE_REGISTRY

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
    def test_op_enum_has_exactly_ten_members(self) -> None:
        assert len(list(Op)) == 10

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


class TestValidatorsNullary:
    @pytest.mark.parametrize("op", ["auth-probe", "compose-ls", "docker-version"])
    def test_accepts_no_args(self, op: str) -> None:
        validate_args(op, [])

    @pytest.mark.parametrize("op", ["auth-probe", "compose-ls", "docker-version"])
    def test_rejects_any_args(self, op: str) -> None:
        with pytest.raises(DispatchValidationError, match="no arguments"):
            validate_args(op, ["unexpected"])

    @pytest.mark.parametrize("op", ["auth-probe", "compose-ls", "docker-version"])
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


# ─── Target Argv Construction Per Op (fixture is the single source) ─────────


class TestTargetArgvFixture:
    def test_fixture_covers_all_ten_ops(self) -> None:
        # Q6 (3.3b): the fixture is keyed on each op's WIRE form. For the seven
        # deterministic ops that is the typed args; for the three compose ops
        # it is the post-expansion named-flag form. Keyed this way every op's
        # target argv is a pure function of its wire inputs, so all ten ops
        # live in the one shared fixture and the Python<->Go lockstep covers
        # compose. (The operator-side <inst>-><operands> resolution stays
        # dynamically tested below — it depends on a seeded instance.)
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
        from core.compose import compose_project_name

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
        from core.compose import compose_project_name

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


# ─── invoke(): validate -> expand -> machinectl_cmd + bash -c -> Executor ──


class _FakeHostSettings:
    docker_unprivileged_user = "sandbox"

    def __init__(self, auth: object) -> None:
        self.machinectl_authentication = auth


class _FakeHostConfig:
    def __init__(self, auth: object) -> None:
        self.host = _FakeHostSettings(auth)


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
        assert cmd[:4] == ["sudo", "machinectl", "shell", "sandbox@.host"]
        assert cmd[4:6] == ["/bin/bash", "-c"]
        assert cmd[6] == (
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
        assert cmd[6] == "/usr/local/libexec/sandbox-ai/dispatch auth-probe"

    def test_compose_op_expands_wire_form_internally(
        self, isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from core.compose import compose_project_name

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
        assert cmd[6] == (
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
        assert cmd[6].endswith(" --volumes")

    def test_polkit_auth_drops_sudo_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from core.host_config import MachinectlAuth

        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        hc = cast("HostConfig", _FakeHostConfig(MachinectlAuth.POLKIT))
        invoke("auth-probe", [], hc)
        cmd = cast("list[str]", captured["cmd"])
        assert cmd[:3] == ["machinectl", "shell", "sandbox@.host"]

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


# ─── probe(): typed non-raising wrapper (Q8) ────────────────────────────────


class TestProbeOutcome:
    def test_is_frozen(self) -> None:
        import dataclasses

        outcome = ProbeOutcome(ok=True, timed_out=False, stdout="x")
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
        assert out == ProbeOutcome(ok=True, timed_out=False, stdout="24.0.7\n")

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
        assert out == ProbeOutcome(ok=False, timed_out=False, stdout="")

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
        assert out == ProbeOutcome(ok=False, timed_out=True, stdout="")

    def test_validation_error_propagates_not_swallowed(self) -> None:
        # probe() only catches SandboxExecutionError; a pre-boundary
        # DispatchValidationError still raises (it is not an outcome).
        with pytest.raises(DispatchValidationError):
            probe("docker-info", ["bogus-preset"], self._fake_hc())


# ─── compile_dispatcher(): offline reproducible compile recipe ──────────────


_GOLANG_PINNED = IMAGE_REGISTRY["golang_alpine"].pinned


class TestCompileDispatcher:
    """Group 4: the docker-based offline reproducible compile recipe.

    All tests mock ``Executor.run`` — no real docker/machinectl is executed.
    """

    def _fake_hc(self) -> HostConfig:
        from core.host_config import MachinectlAuth

        return cast("HostConfig", _FakeHostConfig(MachinectlAuth.SUDO))

    def test_stages_source_tree_into_build_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        build_dir = tmp_path / "build"

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            # Simulate a successful compile: drop the built binary in place so
            # the post-run copy succeeds.
            (build_dir / "dispatch").write_bytes(b"\x7fELF-fake-binary")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        compile_dispatcher(str(build_dir), str(tmp_path / "out"), self._fake_hc())

        # The full source tree is staged, including the fixtures/ dir (the
        # Python<->Go parity corpus go test ./... consumes for C-e).
        assert (build_dir / "main.go").is_file()
        assert (build_dir / "main_test.go").is_file()
        assert (build_dir / "go.mod").is_file()
        assert (build_dir / "go.sum").is_file()
        assert (build_dir / "vendor").is_dir()
        assert (build_dir / "vendor" / "modules.txt").is_file()
        assert (build_dir / "fixtures").is_dir()
        assert (build_dir / "fixtures" / "target_argv_cases.json").is_file()
        # Staged fixture content matches the shipped source-of-truth fixture.
        assert (
            build_dir / "fixtures" / "target_argv_cases.json"
        ).read_bytes() == _FIXTURE_PATH.read_bytes()

    def _capture_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[str, dict[str, object]]:
        import subprocess

        build_dir = tmp_path / "build"
        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            (build_dir / "dispatch").write_bytes(b"binary")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        compile_dispatcher(str(build_dir), str(tmp_path / "out"), self._fake_hc())
        cmd = cast("list[str]", captured["cmd"])
        # The whole docker invocation is the single bash -c payload.
        return cmd[-1], captured

    def test_invocation_is_offline_single_docker_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inner, captured = self._capture_cmd(tmp_path, monkeypatch)
        # Crossed via machinectl_cmd with the SAME user/auth path invoke() uses.
        cmd = cast("list[str]", captured["cmd"])
        assert cmd[:4] == ["sudo", "machinectl", "shell", "sandbox@.host"]
        assert cmd[4:6] == ["/bin/bash", "-c"]
        # Offline: --network none.
        assert "--network none" in inner
        # Exactly ONE docker run (no second pull/run).
        assert inner.count("docker run") == 1
        # Vendored deps, no network fetch.
        assert "GOFLAGS=-mod=vendor" in inner

    def test_uses_digest_pinned_golang_image_not_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inner, _ = self._capture_cmd(tmp_path, monkeypatch)
        assert _GOLANG_PINNED in inner
        assert "@sha256:" in _GOLANG_PINNED
        # The mutable tag form must NOT appear.
        assert "golang:1.23-alpine " not in inner
        assert f"{IMAGE_REGISTRY['golang_alpine'].ref}:" not in inner

    def test_bind_mounts_build_dir_to_slash_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        build_dir = tmp_path / "build"
        inner, _ = self._capture_cmd(tmp_path, monkeypatch)
        assert f"--mount type=bind,src={build_dir},dst=/build" in inner
        assert "--workdir /build" in inner

    def test_in_container_command_is_test_then_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.dispatch import _COMPILE_INNER

        # C-e: the in-container sequence is exactly `go test ./...` THEN
        # `go build` joined by `&&` in ONE docker run — a fixture-parity
        # failure fails go test, the && short-circuits, no binary is produced.
        assert _COMPILE_INNER == (
            "go test ./... && "
            "go build -trimpath -ldflags '-s -w' -o /build/dispatch ."
        )
        inner, _ = self._capture_cmd(tmp_path, monkeypatch)
        # The exact sequence is passed to the container's `/bin/sh -c` (shell-
        # quoted as one argument); go test strictly precedes go build.
        assert "go test ./..." in inner
        assert "go build" in inner
        assert "/bin/sh -c " in inner
        assert inner.index("go test ./...") < inner.index("go build")
        assert "-trimpath" in inner
        assert "-s -w" in inner
        assert "-o /build/dispatch ." in inner

    def test_polkit_auth_drops_sudo_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from core.host_config import MachinectlAuth

        build_dir = tmp_path / "build"
        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            (build_dir / "dispatch").write_bytes(b"binary")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        hc = cast("HostConfig", _FakeHostConfig(MachinectlAuth.POLKIT))
        compile_dispatcher(str(build_dir), str(tmp_path / "out"), hc)
        cmd = cast("list[str]", captured["cmd"])
        assert cmd[:3] == ["machinectl", "shell", "sandbox@.host"]

    def test_successful_compile_places_binary_at_output_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        build_dir = tmp_path / "build"
        output_path = tmp_path / "out" / "dispatch"
        output_path.parent.mkdir()

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            (build_dir / "dispatch").write_bytes(b"\x7fELF-real")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        compile_dispatcher(str(build_dir), str(output_path), self._fake_hc())
        assert output_path.read_bytes() == b"\x7fELF-real"

    def test_go_test_failure_propagates_and_places_no_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate a non-zero `go test` (e.g. Python<->Go fixture drift): the
        # sterile Executor raises SandboxExecutionError. Per C-e, no binary is
        # produced and none is placed at output_path.
        build_dir = tmp_path / "build"
        output_path = tmp_path / "out" / "dispatch"

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> object:
            raise SandboxExecutionError(
                "[FATAL] Sandbox Execution Fault: Inner command failed with exit status 1."
            )

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        with pytest.raises(SandboxExecutionError):
            compile_dispatcher(str(build_dir), str(output_path), self._fake_hc())
        # No binary anywhere: go test failed -> && short-circuited -> no
        # /build/dispatch -> the post-run copy never ran.
        assert not output_path.exists()
        assert not (build_dir / "dispatch").exists()

    def test_run_uses_sentinel_for_in_container_exit_detection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        build_dir = tmp_path / "build"
        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["kwargs"] = kwargs
            (build_dir / "dispatch").write_bytes(b"binary")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        compile_dispatcher(str(build_dir), str(tmp_path / "out"), self._fake_hc())
        # sentinel=True is what recovers the in-container go test/build exit
        # code through the machinectl PTY (so a drift raises).
        assert cast("dict[str, object]", captured["kwargs"])["sentinel"] is True
