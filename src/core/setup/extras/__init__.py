# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""``core.setup.extras`` package marker.

The opt-in extras registry + sticky-opt-in predicate live in
:mod:`core.setup.extras.registry`; this package re-exports them so existing
``from core.setup.extras import EXTRAS, ExtraSpec, selected_extras`` importers
keep working unchanged. The concrete extra phase modules (``fapolicyd``,
``aide``) are siblings of ``registry`` in this package.
"""

from __future__ import annotations

from core.setup.extras.registry import EXTRAS, ExtraSpec, selected_extras

__all__ = ["EXTRAS", "ExtraSpec", "selected_extras"]
