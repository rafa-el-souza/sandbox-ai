# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``core.compose``.

Per task 7.4 of the ``instance-workspace-model`` change, exercises:

* dev-username sanitization for ``.``, ``_``, mixed case, leading/trailing
  non-alphanumerics
* the 30-character instance-name cap (enforced in ``cli.main._validate_name``,
  asserted here against the resulting container-name length math)
* worst-case container name fits under docker's 64-char limit
"""

from __future__ import annotations

import pytest
from core.compose import compose_project_name, sanitize_dev_username


class TestSanitizeDevUsername:
    def test_lowercases_alphanumeric_passes_through(self) -> None:
        assert sanitize_dev_username("alice") == "alice"

    def test_dot_replaced_with_dash(self) -> None:
        assert sanitize_dev_username("dev.foo") == "dev-foo"

    def test_underscore_replaced_with_dash(self) -> None:
        assert sanitize_dev_username("dev_foo") == "dev-foo"

    def test_mixed_case_lowercased(self) -> None:
        assert sanitize_dev_username("DevFoo") == "devfoo"

    def test_leading_and_trailing_non_alnum_stripped(self) -> None:
        assert sanitize_dev_username("_dev.foo_") == "dev-foo"
        assert sanitize_dev_username("---alice---") == "alice"

    def test_consecutive_specials_collapse_to_dashes(self) -> None:
        # Each non-alphanumeric becomes a single dash; we don't collapse runs.
        assert sanitize_dev_username("a..b") == "a--b"

    def test_collision_dot_vs_underscore_documented(self) -> None:
        # D13 risk: dev.foo and dev_foo sanitize identically.
        # The doctor `compose_project_name_collision` check is the mitigation.
        assert sanitize_dev_username("dev.foo") == sanitize_dev_username("dev_foo")


class TestComposeProjectName:
    def test_format_is_dev_dash_inst(self) -> None:
        assert compose_project_name("myinst", dev_username="alice") == "alice-myinst"

    def test_username_sanitized(self) -> None:
        assert compose_project_name("myinst", dev_username="dev.foo") == "dev-foo-myinst"

    def test_resolves_dev_username_from_pwd_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("core.compose._resolve_dev_username", lambda: "resolved")
        assert compose_project_name("foo") == "resolved-foo"

    def test_empty_sanitized_username_raises(self) -> None:
        # All non-alphanumerics: sanitization strips to empty string.
        with pytest.raises(ValueError, match="sanitizes to empty"):
            compose_project_name("foo", dev_username="...")

    def test_worst_case_container_name_under_64_chars(self) -> None:
        # Worst case per design D13: 16-char dev + 30-char inst cap +
        # "-mcp-firecrawl-1" suffix (compose service + index) must fit in 64.
        # `_INSTANCE_NAME_MAX = 30` is enforced in cli.main._validate_name.
        dev = "a" * 16
        inst = "i" * 30
        project = compose_project_name(inst, dev_username=dev)
        # Compose container name = <project>-<service>-<index>; the longest
        # service in this orchestrator is `mcp-firecrawl` (13 chars).
        worst_container = f"{project}-mcp-firecrawl-1"
        assert len(worst_container) <= 63
