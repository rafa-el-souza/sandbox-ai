# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the :mod:`core.json_types` strict-typing boundary alias.

``json_types`` is a single PEP 695 ``type`` alias with no runtime logic. Every
production importer pulls it under ``TYPE_CHECKING`` (it is used only in
annotations), so without this module its body would never execute at runtime —
these tests import it concretely to exercise the alias statement and pin the
public surface.
"""

from __future__ import annotations

from typing import TypeAliasType

import core.json_types as json_types
from core.json_types import JsonValue


def test_json_value_alias_is_exported() -> None:
    """The module exposes exactly the ``JsonValue`` alias as its public API."""
    assert json_types.__all__ == ["JsonValue"]
    assert hasattr(json_types, "JsonValue")


def test_json_value_is_a_type_alias() -> None:
    """``JsonValue`` is a PEP 695 ``type`` alias naming its own name."""
    alias: object = JsonValue
    assert isinstance(alias, TypeAliasType)
    assert alias.__name__ == "JsonValue"


def test_json_value_value_is_the_six_json_kinds_union() -> None:
    """The alias resolves to the closed recursive union over the JSON kinds.

    Accessing ``__value__`` forces evaluation of the alias body (the lazily
    evaluated right-hand side), covering the alias statement at runtime.
    """
    rendered = str(JsonValue.__value__)
    for kind in ("dict", "list", "str", "int", "float", "bool", "None"):
        assert kind in rendered
