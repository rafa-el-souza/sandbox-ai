# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural type for ``json.load``/``json.loads`` results.

``json``'s stdlib stubs type their decode entrypoints as ``Any``, which erases
the shape of parsed data and forces every downstream ``isinstance`` narrowing to
collapse to ``dict[Unknown, Unknown]`` under ``--strict``. Annotating a parse
result as :data:`JsonValue` (a closed recursive union over the six JSON kinds)
gives the boundary a precise, fully-known type: ``isinstance(x, dict)`` then
narrows to ``dict[str, JsonValue]`` (JSON object keys are always ``str``) and
``isinstance(x, list)`` to ``list[JsonValue]`` — no ``Unknown`` leaks, no
``cast`` over the data. It only *names* the contract the decoder already
guarantees; it never reshapes or revalidates the parsed value.
"""

from __future__ import annotations

type JsonValue = dict[str, JsonValue] | list[JsonValue] | str | int | float | bool | None

__all__ = ["JsonValue"]
