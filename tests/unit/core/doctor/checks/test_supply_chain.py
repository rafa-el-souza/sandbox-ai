"""Tests for core.doctor.checks.supply_chain.

Covers `check_image_digests` IMAGE_REGISTRY pin verification.

Q7/Q8 (M6b): both the pinned-digest stale check AND the best-effort
tag-drift check now route through ``core.dispatch.probe`` (the tag ref is a
member of ``{pin.pinned}`` union ``{pin.tagged}``); the check branches on the typed
``ProbeOutcome`` rather than a ``try/except SandboxExecutionError`` +
``__cause__`` discrimination, and the module no longer imports
``machinectl_cmd``.
"""

from __future__ import annotations

from typing import Any

from core.dispatch import ProbeOutcome


def _ok(stdout: str = "{}") -> ProbeOutcome:
    return ProbeOutcome(ok=True, timed_out=False, stdout=stdout, message="")


def _fail() -> ProbeOutcome:
    return ProbeOutcome(ok=False, timed_out=False, stdout="", message="[FATAL] manifest inspect failed")


def _timeout() -> ProbeOutcome:
    return ProbeOutcome(ok=False, timed_out=True, stdout="", message="[FATAL] manifest inspect timed out")


def test_module_exposes_image_digests_check() -> None:
    from core.doctor.checks import supply_chain

    assert set(supply_chain.__all__) == {"check_image_digests"}


def test_public_re_export_resolves_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import supply_chain

    assert doctor_pkg.check_image_digests is supply_chain.check_image_digests


def test_module_does_not_import_machinectl_cmd() -> None:
    """Q7 (M6b): the tag-drift call routes through the op, so the file drops
    its ``machinectl_cmd`` import entirely — this unblocks the Group 8
    convention meta-test for ``supply_chain.py``."""
    from core.doctor.checks import supply_chain

    assert not hasattr(supply_chain, "machinectl_cmd")


class TestCheckImageDigests:
    def test_all_digests_resolvable_pass(self, monkeypatch: Any) -> None:
        from core.doctor import check_image_digests

        captured: dict[str, Any] = {}

        def capture(op: str, args: Any, host_config: Any, **kwargs: Any) -> ProbeOutcome:
            captured["op"] = op
            captured["timeout"] = kwargs.get("timeout")
            return _ok()

        monkeypatch.setattr("core.dispatch.probe", capture)
        result = check_image_digests("sandbox", None)
        assert result.status == "pass"
        assert "8" in result.detail
        # docker-manifest-inspect op per IMAGE_REGISTRY pin (pinned + tagged).
        assert captured["op"] == "docker-manifest-inspect"
        assert captured["timeout"] == 2

    def test_stale_digest_detected_fail(self, monkeypatch: Any) -> None:
        from core.doctor import check_image_digests
        from core.hydration import IMAGE_REGISTRY

        keys = list(IMAGE_REGISTRY.keys())

        def selective_probe(op: str, args: Any, host_config: Any, **kwargs: Any) -> ProbeOutcome:
            if IMAGE_REGISTRY[keys[0]].digest in args[0]:
                return _fail()
            return _ok()

        monkeypatch.setattr("core.dispatch.probe", selective_probe)
        result = check_image_digests("sandbox", None)
        assert result.status == "fail"
        assert keys[0] in result.detail

    def test_timeout_returns_skip(self, monkeypatch: Any) -> None:
        from core.doctor import check_image_digests

        monkeypatch.setattr("core.dispatch.probe", lambda *a, **k: _timeout())
        result = check_image_digests("sandbox", None)
        assert result.status == "skip"
        assert "registry unreachable" in result.detail.lower()

    def test_tag_drift_reports_warn(self, monkeypatch: Any) -> None:
        from core.doctor import check_image_digests
        from core.hydration import IMAGE_REGISTRY

        keys = list(IMAGE_REGISTRY.keys())

        def selective_probe(op: str, args: Any, host_config: Any, **kwargs: Any) -> ProbeOutcome:
            ref = args[0]
            # Pinned refs (digest form) all resolve cleanly.
            if "@sha256:" in ref:
                return _ok()
            # Tag refs: the first registry entry drifted; the rest match.
            if ref == IMAGE_REGISTRY[keys[0]].tagged:
                return _ok(
                    '{"digest": "sha256:'
                    "0000000000000000000000000000000000000000000000000000000000000000"
                    '"}'
                )
            for key in keys[1:]:
                pin = IMAGE_REGISTRY[key]
                if ref == pin.tagged:
                    return _ok(f'{{"digest": "{pin.digest}"}}')
            return _ok()

        monkeypatch.setattr("core.dispatch.probe", selective_probe)
        result = check_image_digests("sandbox", None)
        assert result.status == "pass"
        assert "tag drift detected" in result.detail
        assert keys[0] in result.detail

    def test_tag_drift_json_decode_error(self, monkeypatch: Any) -> None:
        from core.doctor import check_image_digests

        def probe_side(op: str, args: Any, host_config: Any, **kwargs: Any) -> ProbeOutcome:
            if "@sha256:" in args[0]:
                return _ok()
            return _ok("NOT-JSON{{")

        monkeypatch.setattr("core.dispatch.probe", probe_side)
        result = check_image_digests("sandbox", None)
        assert result.status == "pass"
        assert "tag drift detected" not in result.detail

    def test_tag_drift_probe_failure_ignored(self, monkeypatch: Any) -> None:
        """The tag-drift probe is best-effort: a failed/timed-out tag probe is
        silently ignored (no drift recorded, overall verdict still pass)."""
        from core.doctor import check_image_digests

        def probe_side(op: str, args: Any, host_config: Any, **kwargs: Any) -> ProbeOutcome:
            if "@sha256:" in args[0]:
                return _ok()
            return _timeout()  # tag-drift probe times out → best-effort skip

        monkeypatch.setattr("core.dispatch.probe", probe_side)
        result = check_image_digests("sandbox", None)
        assert result.status == "pass"
        assert "tag drift detected" not in result.detail

    def test_user_threaded_into_host_config(self, monkeypatch: Any) -> None:
        from core.doctor import check_image_digests
        from core.host_config import MachinectlAuth

        captured: dict[str, Any] = {}

        def capture(op: str, args: Any, host_config: Any, **kwargs: Any) -> ProbeOutcome:
            captured["host_config"] = host_config
            return _ok()

        monkeypatch.setattr("core.dispatch.probe", capture)
        check_image_digests("sandbox", None)

        assert captured["host_config"].host.docker_unprivileged_user == "sandbox"
        assert captured["host_config"].host.machinectl_authentication == MachinectlAuth.SUDO
