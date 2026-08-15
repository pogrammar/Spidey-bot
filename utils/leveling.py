"""Reputation leveling curve. Replaces the old flat 100-XP-per-level rule with an
accelerating one (each level costs 12% more XP than the last), so climbing gets
meaningfully harder at higher levels instead of staying constant forever.

Deliberately has zero imports from db/ or services/ — db.models.User.reputation_level
needs this directly, and services/db can't be imported from the model layer without
a circular import.
"""

from __future__ import annotations

import bisect

BASE_LEVEL_XP = 100  # XP for level 1 -> 2, matches the old flat cost exactly
LEVEL_GROWTH = 1.12  # each subsequent level costs 12% more than the one before it

# Covers any level a player could realistically reach through play. Both lookup
# functions below still extend past this on demand (e.g. an admin manually setting
# a huge XP value for testing) rather than erroring out — this ceiling just keeps
# the common case O(log n) instead of recomputing the curve from scratch every call.
_MAX_PRECOMPUTED_LEVEL = 100


def _build_thresholds() -> list[int]:
    """thresholds[i] = cumulative XP required to reach level i + 1 (thresholds[0]
    is level 1's requirement, which is always 0)."""
    thresholds = [0]
    total = 0
    for level in range(1, _MAX_PRECOMPUTED_LEVEL):
        total += round(BASE_LEVEL_XP * LEVEL_GROWTH ** (level - 1))
        thresholds.append(total)
    return thresholds


_LEVEL_THRESHOLDS = _build_thresholds()


def level_for_xp(xp: int) -> int:
    """Reputation level for a given XP total — the highest level whose threshold
    is <= xp. Extends past the precomputed table for XP totals beyond it (e.g. an
    admin-set test value), rather than capping out."""
    if xp <= _LEVEL_THRESHOLDS[-1]:
        return bisect.bisect_right(_LEVEL_THRESHOLDS, xp)
    level = _MAX_PRECOMPUTED_LEVEL
    total = _LEVEL_THRESHOLDS[-1]
    while True:
        total += round(BASE_LEVEL_XP * LEVEL_GROWTH ** (level - 1))
        if total > xp:
            return level
        level += 1


def xp_for_level(level: int) -> int:
    """Minimum cumulative XP required to be at the given level (level 1 = 0 XP).
    Extends past the precomputed table for levels beyond it."""
    level = max(level, 1)
    if level - 1 < len(_LEVEL_THRESHOLDS):
        return _LEVEL_THRESHOLDS[level - 1]
    total = _LEVEL_THRESHOLDS[-1]
    for lvl in range(_MAX_PRECOMPUTED_LEVEL, level):
        total += round(BASE_LEVEL_XP * LEVEL_GROWTH ** (lvl - 1))
    return total
