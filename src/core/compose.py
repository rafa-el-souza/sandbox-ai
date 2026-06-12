# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Daemon-side compose project name derivation.

Per `instance-registry`'s "Compose Project Name Prefix" requirement, the
operator-facing instance name `<inst>` is mapped to the daemon-side
compose project name `<sanitized(dev-username)>-<inst>`. The prefix
prevents cross-user collisions on the shared rootless docker daemon.
"""

from __future__ import annotations

import os
import pwd
import re

_SANITIZE_RE = re.compile(r"[^a-z0-9]")


def sanitize_dev_username(username: str) -> str:
    return _SANITIZE_RE.sub("-", username.lower()).strip("-")


def _resolve_dev_username() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def compose_project_name(instance_name: str, *, dev_username: str | None = None) -> str:
    """Return the daemon-side compose project name for ``instance_name``.

    ``dev_username`` is injected for testability; production callers leave it
    as ``None`` to resolve the invoking user via ``getpwuid(getuid())``.
    """
    raw = dev_username if dev_username is not None else _resolve_dev_username()
    sanitized = sanitize_dev_username(raw)
    if not sanitized:
        raise ValueError(f"dev username {raw!r} sanitizes to empty string")
    return f"{sanitized}-{instance_name}"
