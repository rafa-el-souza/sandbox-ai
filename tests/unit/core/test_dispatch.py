"""Tests for the typed dispatcher orchestration scaffold (Milestone 1).

This milestone scaffolds the op enum + OpSpec wiring; validators, builders,
and ``invoke`` are stubs that raise ``NotImplementedError``. These tests pin
the enum surface and the ``invoke`` signature (load-bearing for later
milestones) and cover every stub branch for the 100% gate.
"""

import inspect
from typing import TYPE_CHECKING, cast

import pytest
from core.dispatch import (
    OP_SPECS,
    Op,
    OpSpec,
    _unimplemented_builder,
    _unimplemented_validator,
    invoke,
)

if TYPE_CHECKING:
    from core.host_config import HostConfig

# The exact ten wire names the dispatcher accepts as argv[1] (spec "Typed Op
# Surface"). This set is the contract; any drift fails the build.
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


class TestOpEnum:
    def test_op_enum_has_exactly_ten_members(self) -> None:
        assert len(list(Op)) == 10

    def test_op_enum_values_match_expected_wire_names(self) -> None:
        assert {op.value for op in Op} == EXPECTED_OP_VALUES

    @pytest.mark.parametrize("wire_name", sorted(EXPECTED_OP_VALUES))
    def test_each_wire_name_round_trips_via_strenum(self, wire_name: str) -> None:
        # StrEnum: Op(value) resolves and the member compares equal to its str.
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
        assert spec.min_args == 0
        assert spec.max_args is None

    def test_op_spec_is_frozen(self) -> None:
        spec = OP_SPECS[Op.AUTH_PROBE]
        # Frozen dataclass: attribute assignment is rejected at runtime. Drive
        # setattr through a non-constant attribute name so neither the linter
        # (B010) nor the type checker flags the deliberately-illegal mutation
        # (no suppression directive needed).
        attr = "name"
        with pytest.raises(AttributeError):
            setattr(spec, attr, "mutated")


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
        # `from __future__ import annotations` stringifies the annotation;
        # resolving via get_type_hints would also try to resolve the
        # TYPE_CHECKING-only HostConfig param and raise NameError, so assert
        # the raw stringified return annotation directly.
        sig = inspect.signature(invoke)
        assert sig.return_annotation == "subprocess.CompletedProcess[bytes]"


class TestStubsRaiseNotImplemented:
    @pytest.mark.parametrize("op", list(Op))
    def test_validator_stub_raises(self, op: Op) -> None:
        validator = _unimplemented_validator(op)
        with pytest.raises(NotImplementedError, match=op.value):
            validator(["some", "args"])

    @pytest.mark.parametrize("op", list(Op))
    def test_builder_stub_raises(self, op: Op) -> None:
        builder = _unimplemented_builder(op)
        host_config = cast("HostConfig", object())
        with pytest.raises(NotImplementedError, match=op.value):
            builder(["some", "args"], host_config)

    @pytest.mark.parametrize("op", list(Op))
    def test_wired_op_spec_validator_raises(self, op: Op) -> None:
        with pytest.raises(NotImplementedError):
            OP_SPECS[op].validate([])

    @pytest.mark.parametrize("op", list(Op))
    def test_wired_op_spec_builder_raises(self, op: Op) -> None:
        host_config = cast("HostConfig", object())
        with pytest.raises(NotImplementedError):
            OP_SPECS[op].build_target_argv([], host_config)

    def test_invoke_raises_not_implemented(self) -> None:
        host_config = cast("HostConfig", object())
        with pytest.raises(NotImplementedError, match="scaffold stub"):
            invoke(Op.AUTH_PROBE, [], host_config, timeout=5.0)

    def test_invoke_accepts_str_op_form(self) -> None:
        # The contract states invoke accepts the str wire value too; the stub
        # still raises but the str path must reach the body.
        host_config = cast("HostConfig", object())
        with pytest.raises(NotImplementedError):
            invoke("compose-up", ["inst"], host_config)
