from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from services.inventory_service import add_item
from services.patreon_service import TIER_RANK_SYMBIOTE, tier_badge
from utils.icons import emoji

# Biomorphic Webbing's out-of-combat half (GAME_DESIGN.md §9.3, Symbiote+ only).
#
# The perk's combat half already lived in battle_service (a bonus component, a bonus
# photo) and patrol_service (bonus cash), which meant that outside a patrol the perk was
# indistinguishable from plain Organic Webbing — it saved you vials and did nothing else.
# This is the other half: the webbing keeps working while Peter is doing ordinary Peter
# things, and picks up what it passes.
#
# WHY THIS IS ITS OWN MODULE, and not a function in patrol_service: patrol_service imports
# ally_service, so ally_service can never import patrol_service back without a cycle — and
# an ally visit is one of the three activities that needs this roll. This module imports
# only inventory_service (itself a true leaf), patreon_service and utils.icons, so all
# three callers can reach it from anywhere in the graph. Keep it that way; importing any
# activity service from here would recreate exactly the cycle it exists to avoid.
#
# 0.30 as of 2026-08-25, up from the 0.20 this shipped at on 2026-08-24, on the owner's
# explicit instruction ("we need to up the percentage a lil bit"). Two things that changes,
# both deliberate:
#
# This is now the perk's HIGHEST rate — above the cash roll's 0.25 and the two combat rolls'
# 0.20 — where it used to be tied for lowest. The original argument for 0.20 was that one
# recognisable rate across all four rolls is easier to explain, and that argument was
# already false when it was written (the cash roll has always been 0.25). What makes the
# ordering defensible rather than merely instructed: the two 0.20 combat rolls are the ones
# with extra preconditions on top of the rate — the component only rolls if the base drop
# already missed, and the photo needs a camera equipped AND a photo already banked — while
# this one fires on every completed activity with nothing else asked of it. A flat rate
# across rolls with unequal preconditions was never the same odds anyway.
#
# It also makes the fastest farm faster, which is worth knowing but is not a cash exploit.
# `/ally visit` with no gift is free, has no cooldown, and its busy lock bottoms out at
# MIN_VISIT_SECONDS (30s) once the ally is happy — so ~120 rolls/hour is reachable by
# clicking, and at 0.30 that's ~36 components/hour instead of ~24. What bounds it is the
# other end: a repair consumes exactly ONE Spandex Fabric (plus one Micro-Electronics past
# suit_service.ELECTRONICS_THRESHOLD) regardless of how much integrity is missing, and
# there is no NPC buyback anywhere in the game — surplus components can only be listed on
# the player market (market_service). So this rate buys a Symbiote subscriber repair
# self-sufficiency and adds supply to a player-to-player market. It does not mint money,
# and the $80/$150 figures in data/items.json are shop *buy* prices, not payouts.
AMBIENT_SCAVENGE_CHANCE = 0.30

# Weighted to match the economy already in data/items.json, whose own descriptions call
# Spandex Fabric the ordinary patrol scavenge and Micro-Electronics the "rarer" one. The
# 3:1 split puts them at 22.5% and 7.5% of any given activity, and the price gap ($80 vs
# $150) means the rarer roll is also the more valuable one. These are the only two
# components in the game, and the two the perk is specified against: what the suit needs to
# repair. The weights are the split, not the rates — they were unchanged by the 0.20 → 0.30
# move above, which is why the per-item numbers in this comment shifted on their own. Any
# future rate change moves them again; re-derive rather than trusting the text.
AMBIENT_SCAVENGE_TABLE = [
    ("spandex_fabric", "Spandex Fabric", 3),
    ("micro_electronics", "Micro-Electronics", 1),
]

# Per-activity flavor. The line has to say *where* it happened, or a component silently
# appearing in the inventory reads as a bug rather than a perk — the same failure the
# combat component roll had before 2026-08-22, when it was byte-identical to an ordinary
# drop. Keyed by the activity constants below so a caller can't invent a key that has no
# copy: _flavor_for raises on an unknown activity rather than rendering an empty line.
ACTIVITY_TUTORING = "tutoring"
ACTIVITY_ALLY_VISIT = "ally_visit"
ACTIVITY_BUGLE = "bugle"

ACTIVITY_FLAVOR = {
    ACTIVITY_TUTORING: [
        "The webbing lifted it off a workbench on the way out and didn't mention it.",
        "Something came back from the study session that didn't go in with you.",
        "You find it in a pocket you didn't reach into. The suit is not sorry.",
    ],
    ACTIVITY_ALLY_VISIT: [
        "The webbing helped itself to something on the walk over.",
        "It picked this up somewhere between their place and yours.",
        "You didn't stop once on the way. The suit did.",
    ],
    ACTIVITY_BUGLE: [
        "The webbing went through the bullpen while you waited on Jonah.",
        "Something followed you out of the Bugle. The suit isn't saying what from.",
        "It lifted this off a desk on the way past. Nobody saw a thing.",
    ],
}


@dataclass
class AmbientScavenge:
    """One component the webbing picked up during a non-combat activity."""

    item_key: str
    item_name: str
    flavor: str


def _flavor_for(activity: str) -> str:
    try:
        return random.choice(ACTIVITY_FLAVOR[activity])
    except KeyError:
        raise ValueError(
            f"no ambient-scavenge flavor for activity {activity!r} — add it to "
            f"ACTIVITY_FLAVOR rather than letting the perk fire with no explanation"
        ) from None


async def roll_ambient_scavenge(
    session: AsyncSession, user_id: int, tier_rank: int, activity: str
) -> AmbientScavenge | None:
    """Roll Biomorphic Webbing's ambient pickup for one non-combat activity.

    Returns None both when the player isn't Symbiote and when the roll simply missed —
    callers treat those identically, so there is no way for a non-subscriber to tell from
    the output that a roll happened at all.

    `activity` must be one of the ACTIVITY_* constants; an unknown one raises, because a
    component appearing with no line explaining it is the exact bug this perk had before.

    Commits, because inventory_service.add_item commits. Call it AFTER the activity's own
    commit, not before: this is a bonus on top of a completed activity, so a failure here
    must never be able to roll back the session/visit/submission the player actually did.
    """
    if tier_rank < TIER_RANK_SYMBIOTE:
        return None
    if random.random() >= AMBIENT_SCAVENGE_CHANCE:
        return None

    keys = [key for key, _name, _weight in AMBIENT_SCAVENGE_TABLE]
    weights = [weight for _key, _name, weight in AMBIENT_SCAVENGE_TABLE]
    item_key = random.choices(keys, weights=weights, k=1)[0]
    item_name = next(name for key, name, _w in AMBIENT_SCAVENGE_TABLE if key == item_key)

    await add_item(session, user_id, item_key, 1)
    return AmbientScavenge(item_key=item_key, item_name=item_name, flavor=_flavor_for(activity))


def scavenge_subtext(scavenge: AmbientScavenge, tier_rank: int) -> str:
    """The `-#` subtext line all three activities render, so they can't drift apart.

    Attribution follows the rule in GAME_DESIGN.md §9: the perk's own glyph leads (which
    perk fired) and the tier badge trails (whose subscription paid for it). Both degrade
    to nothing if the emoji hasn't been uploaded, so this is safe to interpolate unguarded.
    """
    glyph = emoji("biomorphic_webbing")
    lead = f"{glyph} " if glyph else ""
    badge = tier_badge(tier_rank)
    trail = f" {badge}" if badge else ""
    item = f"**{scavenge.item_name}**"
    return f"\n-# {lead}{scavenge.flavor} +1 {item}{trail}"
