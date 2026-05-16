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
    build_target_argv,
    invoke,
    validate_args,
)

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
        sig = inspect.signature(invoke)
        assert sig.return_annotation == "subprocess.CompletedProcess[bytes]"

    def test_invoke_raises_not_implemented(self) -> None:
        # Milestone 2 does NOT implement invoke()'s body; the M1 contract holds.
        hc = cast("HostConfig", object())
        with pytest.raises(NotImplementedError, match="scaffold stub"):
            invoke(Op.AUTH_PROBE, [], hc, timeout=5.0)

    def test_invoke_accepts_str_op_form(self) -> None:
        hc = cast("HostConfig", object())
        with pytest.raises(NotImplementedError):
            invoke("compose-up", ["inst"], hc)


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
    def test_accepts_full_image_ref(self) -> None:
        validate_args("docker-manifest-inspect", [_BUSYBOX_REF])

    def test_accepts_pathish_image_ref(self) -> None:
        validate_args(
            "docker-manifest-inspect",
            ["registry.example.io/lib/golang@sha256:" + "a" * 64],
        )

    def test_rejects_bare_digest(self) -> None:
        # Spec scenario: "Validator rejects bare-digest arg to
        # docker-manifest-inspect"
        with pytest.raises(DispatchValidationError, match=r"<name>@"):
            validate_args("docker-manifest-inspect", ["sha256:" + "a" * 64])

    def test_rejects_short_digest(self) -> None:
        with pytest.raises(DispatchValidationError):
            validate_args("docker-manifest-inspect", ["busybox@sha256:" + "a" * 32])

    def test_rejects_uppercase_hex(self) -> None:
        with pytest.raises(DispatchValidationError):
            validate_args("docker-manifest-inspect", ["busybox@sha256:" + "A" * 64])

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
    def test_fixture_covers_all_deterministic_builder_ops(self) -> None:
        # The fixture pins the args-pure ops (compose-* depend on runtime
        # registry/dev-username state and are exercised dynamically below).
        ops_in_fixture = {cast("str", c["op"]) for c in _load_fixture()}
        assert ops_in_fixture == {
            "auth-probe",
            "compose-ls",
            "docker-version",
            "docker-info",
            "docker-manifest-inspect",
            "helper-chown-files",
            "helper-mkdir-chown-dirs",
        }

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


class TestComposeBuilders:
    def test_compose_up_byte_faithful(
        self, isolated_sandbox_ai_home: Path, host_config: HostConfig
    ) -> None:
        from core.compose import compose_project_name

        _seed_instance(isolated_sandbox_ai_home, "demo")
        argv = build_target_argv("compose-up", ["demo"], host_config)
        proj = compose_project_name("demo")
        inst_dir = isolated_sandbox_ai_home / "instances" / "demo"
        compose_yml = inst_dir / "docker" / "compose.yml"
        env = inst_dir / ".sandbox.env"
        assert argv == [
            "/bin/bash",
            "-c",
            f"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain "
            f"COMPOSE_PROJECT_NAME={proj} docker compose -f {compose_yml} "
            f"--ansi never --env-file {env} up -d --build --wait",
        ]

    def test_compose_down_no_volumes(
        self, isolated_sandbox_ai_home: Path, host_config: HostConfig
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        argv = build_target_argv("compose-down", ["demo"], host_config)
        assert argv[2].endswith(" down")
        assert " -v" not in argv[2]

    def test_compose_down_with_volumes_appends_v_flag(
        self, isolated_sandbox_ai_home: Path, host_config: HostConfig
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        argv = build_target_argv("compose-down", ["demo", "--volumes"], host_config)
        assert argv[2].endswith(" down -v")

    def test_compose_ps_byte_faithful(
        self, isolated_sandbox_ai_home: Path, host_config: HostConfig
    ) -> None:
        from core.compose import compose_project_name

        _seed_instance(isolated_sandbox_ai_home, "demo")
        argv = build_target_argv("compose-ps", ["demo"], host_config)
        proj = compose_project_name("demo")
        inst_dir = isolated_sandbox_ai_home / "instances" / "demo"
        compose_yml = inst_dir / "docker" / "compose.yml"
        env = inst_dir / ".sandbox.env"
        assert argv == [
            "/bin/bash",
            "-c",
            f"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME={proj} "
            f"docker compose -f {compose_yml} "
            f"--env-file {env} "
            f"--ansi never ps --format json",
        ]

    def test_compose_files_include_postgres_extra(
        self, isolated_sandbox_ai_home: Path, host_config: HostConfig
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo", pg=True)
        argv = build_target_argv("compose-up", ["demo"], host_config)
        assert "docker/extras/db-postgres.yml" in argv[2]

    def test_compose_files_include_firecrawl_extra(
        self, isolated_sandbox_ai_home: Path, host_config: HostConfig
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo", fc=True)
        argv = build_target_argv("compose-up", ["demo"], host_config)
        assert "docker/extras/mcp-firecrawl.yml" in argv[2]

    def test_compose_unregistered_instance_raises(
        self, isolated_sandbox_ai_home: Path, host_config: HostConfig
    ) -> None:
        with pytest.raises(DispatchValidationError, match="no sandbox instance"):
            build_target_argv("compose-up", ["nonexistent"], host_config)
