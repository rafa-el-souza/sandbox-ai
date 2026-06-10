"""Single source of truth for host CPU/RAM/size detection.

Imported by both the hydration CPU/RAM clamp and the doctor host-capacity checks
so the two never disagree about what the host actually has.
"""

import os

_SIZE_MULTIPLIERS: dict[str, int] = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
}


def host_cpu_count() -> int:
    """Return the host's available CPU count, floored at 1.

    ``os.cpu_count()`` is the PRIMARY source because Docker validates ``--cpus``
    against the host's *online* CPU count, which is exactly what ``os.cpu_count()``
    reports. ``len(os.sched_getaffinity(0))`` reports this *process's* CPU affinity,
    which is narrower on a cpuset-pinned host (e.g. a process pinned to 2 of 32
    cores) — using it as primary would over-clamp ``--cpus`` below what Docker
    permits. Affinity is therefore only a fallback for the rare case where
    ``os.cpu_count()`` returns ``None``.
    """
    count = os.cpu_count()
    if count is None:
        count = len(os.sched_getaffinity(0))
    return max(count, 1)


def host_ram_bytes() -> int:
    """Return total physical RAM in bytes."""
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def parse_docker_size(value: int | str) -> int:
    """Parse a Docker/Compose size value to integer bytes.

    Accepts an ``int`` (already bytes, returned as-is), a bare numeric string of
    bytes (``"1048576"``), or a suffixed string — case-insensitive, optional
    trailing ``b`` — using Docker's binary multipliers (k=1024, m=1024², g=1024³):
    ``b``, ``k``/``kb``, ``m``/``mb``, ``g``/``gb``. Raises ``ValueError`` on
    unparseable input.

    Validates at the boundary because this parses untrusted-ish data read from a
    rendered config file.
    """
    if isinstance(value, int):
        return value

    text = value.strip().lower()
    if not text:
        raise ValueError("cannot parse empty size value")

    if text.isdigit():
        return int(text)

    for suffix in ("gb", "mb", "kb", "g", "m", "k", "b"):
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            if number.isdigit():
                return int(number) * _SIZE_MULTIPLIERS[suffix]
            break

    raise ValueError(f"cannot parse size value: {value!r}")
