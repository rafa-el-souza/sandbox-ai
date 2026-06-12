# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from core.hashing import generate_project_hash

if TYPE_CHECKING:
    import pytest


def test_generate_project_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Test MD5 hashing of absolute paths.
    E.g. md5("/home/user/api") -> api-8f3a9e
    """
    monkeypatch.setattr(os.path, "abspath", lambda p: "/home/user/api")

    instance_name = generate_project_hash("api")

    # "api" + "-" + md5("/home/user/api")[:6]
    assert instance_name.startswith("api-")
    assert len(instance_name.split("-")[1]) == 6
