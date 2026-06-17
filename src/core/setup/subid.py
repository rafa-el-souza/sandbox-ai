# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Subid (``/etc/subuid`` + ``/etc/subgid``) constants, whole-file reader, and
free-block allocator.

Single-sources the subid layout facts that were previously duplicated as bare
``100000`` literals and two ``_MIN_SUBID_RANGE`` definitions (``host_batch`` +
``l2_host_prereqs``):

- :data:`SUBID_BASE` — the *scan seed* for the free-block picker. It is NOT an
  assumption that "subids start at 100000": fedora ships ``524288:65536``, so an
  existing entry can sit anywhere. The picker *scans* upward from this seed; it
  never assumes the seed is occupied or free.
- :data:`SUBID_RANGE_SIZE` — the *allocation size* of each newly minted block.
- :data:`MIN_SUBID_RANGE` — the *validation floor* (``l2`` refuses to shrink an
  existing entry below this). It is a distinct concept from the allocation size
  even though they coincide at 65536 today. The invariant
  ``MIN_SUBID_RANGE <= SUBID_RANGE_SIZE`` must hold: a block the picker mints is
  always at least the validation floor wide.

The free-block picker selects the lowest ``SUBID_BASE + k*SUBID_RANGE_SIZE``
block (k = 0, 1, 2, ...) that overlaps NO existing entry across the union of all
users' ``/etc/subuid`` + ``/etc/subgid`` ranges. The same picked block is the
single seam consumed by both the real append (setup's ``_apply_subid`` / ``l2``)
and the remediation preview — there is no second hand-typed literal.
"""

from __future__ import annotations

from pathlib import Path

# The standard rootless subuid/subgid layout shadow-mapped by Docker.
# SUBID_BASE is the picker's scan SEED only (never a "subids start here"
# assumption); SUBID_RANGE_SIZE is the allocation size of a freshly minted block.
SUBID_BASE = 100000
SUBID_RANGE_SIZE = 65536

# The validation floor: l2 refuses to shrink an existing entry below this. A
# distinct concept from SUBID_RANGE_SIZE (the allocation size) — they merely
# coincide at 65536 today. Invariant: MIN_SUBID_RANGE <= SUBID_RANGE_SIZE.
MIN_SUBID_RANGE = SUBID_RANGE_SIZE

# Path seams for the whole-file reader — overridable in tests via monkeypatch,
# mirroring core.host_config's per-user parser constants. The whole-file reader
# enumerates EVERY user's ranges (cross-user occupancy), which the per-user
# parsers cannot express; these constants point at the same canonical files.
_SUBUID_PATH: Path = Path("/etc/subuid")
_SUBGID_PATH: Path = Path("/etc/subgid")

__all__ = [
    "MIN_SUBID_RANGE",
    "SUBID_BASE",
    "SUBID_RANGE_SIZE",
    "pick_free_subid_block",
    "ranges_overlap",
    "read_all_subid_ranges",
    "read_all_subid_ranges_by_user",
]


def ranges_overlap(a_start: int, a_size: int, b_start: int, b_size: int) -> bool:
    """``True`` iff ``[a_start, a_start+a_size)`` overlaps ``[b_start, b_start+b_size)``.

    Two half-open ranges overlap iff ``a < b+nb and b < a+na``.
    """
    return a_start < b_start + b_size and b_start < a_start + a_size


def _all_users_ranges_by_user(path: Path) -> list[tuple[str, int, int]]:
    """Enumerate ``(user, start, count)`` for EVERY user in a subid file.

    Unlike :func:`core.host_config.parse_subuid_for_user`, this does NOT filter
    by user — it returns the union of all users' ranges (carrying the owning
    user label) so cross-user occupancy can be computed by user identity.
    """
    try:
        content = path.read_text()
    except FileNotFoundError:
        return []
    ranges: list[tuple[str, int, int]] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 3:
            continue
        user, start_s, count_s = parts
        try:
            ranges.append((user, int(start_s), int(count_s)))
        except ValueError:
            continue
    return ranges


def read_all_subid_ranges_by_user() -> list[tuple[str, int, int]]:
    """Return ``(user, start, count)`` for ALL users across both subid files.

    A mockable whole-file seam that PRESERVES the owning user label — required
    for cross-user overlap detection, which must distinguish a foreign user's
    range from the subject user's own even when the two share an identical
    ``(start, count)`` value (the pre-F-071 footgun: every operator got an
    identical ``100000:65536`` block, so a value-only dedup would mask the
    overlap). The picker, which only needs value-occupancy, derives its
    value-only view via :func:`read_all_subid_ranges`.
    """
    ranges = _all_users_ranges_by_user(_SUBUID_PATH)
    ranges.extend(_all_users_ranges_by_user(_SUBGID_PATH))
    return ranges


def read_all_subid_ranges() -> list[tuple[int, int]]:
    """Return the union of ALL users' ``(start, count)`` ranges across both files.

    The value-only view (user label dropped) consumed by the free-block PICKER,
    which cares only about which value-ranges are occupied, not by whom. Derived
    from :func:`read_all_subid_ranges_by_user` so the two seams never diverge.
    """
    return [(start, count) for _user, start, count in read_all_subid_ranges_by_user()]


def pick_free_subid_block(
    existing: list[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    """Pick the lowest free ``(start, SUBID_RANGE_SIZE)`` block.

    Scans ``SUBID_BASE + k*SUBID_RANGE_SIZE`` (k = 0, 1, 2, ...) and returns the
    first block overlapping no entry in ``existing`` (defaulting to the live
    union from :func:`read_all_subid_ranges`). ``SUBID_BASE`` is the scan seed,
    not an assumption that the base is free — an occupied base shifts the pick
    up by one ``SUBID_RANGE_SIZE`` increment.
    """
    occupied = read_all_subid_ranges() if existing is None else existing
    k = 0
    while True:
        start = SUBID_BASE + k * SUBID_RANGE_SIZE
        if not any(
            ranges_overlap(start, SUBID_RANGE_SIZE, o_start, o_size)
            for o_start, o_size in occupied
        ):
            return start, SUBID_RANGE_SIZE
        k += 1
