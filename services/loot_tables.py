from __future__ import annotations

import json
import random
from pathlib import Path

LOOT_PATH = Path(__file__).resolve().parent.parent / "data" / "loot_tables.json"

with open(LOOT_PATH, encoding="utf-8") as f:
    LOOT_TABLES = json.load(f)


def weighted_choice(entries: list[dict], weight_key: str = "weight") -> dict:
    total = sum(e[weight_key] for e in entries)
    roll = random.uniform(0, total)
    upto = 0.0
    for entry in entries:
        upto += entry[weight_key]
        if roll <= upto:
            return entry
    return entries[-1]


def rand_range(bounds: list[int]) -> int:
    lo, hi = bounds
    return random.randint(lo, hi)


def rand_float_range(bounds: list[float]) -> float:
    lo, hi = bounds
    return random.uniform(lo, hi)
