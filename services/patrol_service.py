from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import InventoryItem, User
from services.ally_service import (
    LOW_HAPPINESS_THRESHOLD,
    get_current_happiness,
    reputation_xp_multiplier,
)
from services.economy import BOSS_LEVEL_INTERVAL, add_reputation, add_wallet, next_boss_gate_level
from services.inventory_service import remove_item
from services.loot_tables import LOOT_TABLES, rand_range, weighted_choice
from services.patreon_service import TIER_RANK_ARACHNID, TIER_RANK_NONE, TIER_RANK_SYMBIOTE

PATROL_COOLDOWN_SECONDS = 30
CAMERA_ITEM_KEY = "camera"
CAMERA_SILVER_ITEM_KEY = "camera_silver"
# Ordered lowest to highest tier — get_equipped_camera picks the best-tier row if
# more than one is somehow equipped, and shop_service unequips the rest of this
# family whenever one is bought, so only one is ever actually equipped in practice.
CAMERA_FAMILY_KEYS = [CAMERA_ITEM_KEY, CAMERA_SILVER_ITEM_KEY]

# Camera tiers (Arachnid+ Patreon-exclusive past the base camera, gated in
# shop_service.ARACHNID_GATED_ITEM_KEYS) — break_chance_reduction multiplies down
# the existing break-chance formula in battle_service.finalize_battle;
# quality_bump_chance is an independent roll to bump the banked photo's quality up
# one tier (bronze->silver->gold, capped at gold). Silver's bump is exactly 0.60 —
# "3 in 5 times" is copy the subscriber is told outright, so it's a promise rather
# than an interior balance knob: don't nudge it for tuning, change the promise first
# (GAME_DESIGN.md §6.2). Break-chance reduction (-70%) stays a tuned number.
CAMERA_TIER_STATS = {
    CAMERA_ITEM_KEY: {"break_chance_reduction": 0.0, "quality_bump_chance": 0.0},
    CAMERA_SILVER_ITEM_KEY: {"break_chance_reduction": 0.70, "quality_bump_chance": 0.60},
}
PHOTO_QUALITY_ORDER = ["bronze", "silver", "gold"]
CRIME_LEVEL_WEIGHT_BONUS = 0.3  # each point of crime_level nudges crime-outcome odds up
COMBAT_READY_PATROLS_WEIGHT_BONUS = 15  # Arachnid+ Patreon perk — see _roll_patrol_outcome

# Biomorphic Webbing (Symbiote+ Patreon perk) — passive bonus cash chance, applies
# to both non-combat patrols (below) and combat encounters (battle_service.py
# imports these two). Component/photo bonus rolls are combat-only and live in
# battle_service.py, since non-combat patrols don't have either system at all.
BIOMORPHIC_WEBBING_CASH_CHANCE = 0.25
BIOMORPHIC_WEBBING_CASH_RANGE = [15, 35]
# /tutoring is the only thing that raises crime_level and /patrol is the only thing
# that lowers it — one lever each, which is what keeps the meter legible. The drain
# is sized for per-MINUTE parity against tutoring's rise, not per-action: tutoring
# adds +8..15 while blocking patrol for 2 minutes, and patrol's cooldown is 30s, so
# one tutoring session costs the same wall-clock as ~4 patrols and should take about
# 4 patrols to clear. Sizing these 1:1 per action instead would drain crime ~4x
# faster than it builds, pinning the meter at 0 — which would quietly switch off both
# things crime_level actually drives: the combat-odds bonus below and the >=70
# reputation-XP penalty in economy.add_reputation.
CRIME_LEVEL_DECAY_RANGE = [2, 3]  # patrolling calms the city back down — non-combat outcomes

# Combat outcomes use a bigger split instead of the flat range above — actually
# stopping a crime should calm the city more than showing up and losing. Uniform
# across every crime tier and boss fights alike (see battle_service.finalize_battle)
# — no tier-specific bump, since boss wins are rare enough that a special case for
# them would barely ever be felt next to routine crime-tier wins/losses. A combat
# patrol also eats more wall-clock than a non-combat one, so the bigger clear is
# what keeps it time-fair rather than being a pure reward.
CRIME_LEVEL_DECAY_RANGE_WIN = [4, 6]
CRIME_LEVEL_DECAY_RANGE_LOSS = [1, 3]

# every patrol burns web fluid to get around — no vials on hand means paying cash
# instead (store-bought substitute fluid, worse and pricier), which is what actually
# makes /lab brew worth running rather than a side activity nobody needs
WEB_FLUID_ITEM_KEY = "web_fluid_vial"
WEB_FLUID_PER_PATROL = 1
NO_WEB_FLUID_TAX_RANGE = [20, 40]

# neglecting an ally makes their related "Parker Luck" hazard more likely to fire
HAZARD_ALLY_KEY = {"aunt_may_flowers": "aunt_may", "mj_birthday_gift": "mj"}
NEGLECT_HAZARD_MULTIPLIER = 2.5

# higher reputation level = tougher patrols: more suit damage, more XP, more wear on
# gear. Keeps the game from getting purely easier as you progress. 1.0x at level 1,
# +5% per level after that (level 11 -> 1.5x, level 21 -> 2.0x). Shared with
# battle_service.py, which scales enemy stats off the same curve.
DIFFICULTY_PER_LEVEL = 0.05


def difficulty_multiplier(level: int) -> float:
    return 1 + DIFFICULTY_PER_LEVEL * (level - 1)


# Named threats guarding each boss gate, in order — one every 5 reputation levels,
# level 5 through level 100. Cycles back to the top with a "Round N" suffix if a
# save ever climbs past the whole roster (see boss_difficulty_level below for why
# that can happen without the fight getting any harder). Boss encounters are
# triggered directly (see begin_boss_patrol), never rolled from the weighted patrol
# table, so there's no xp/cash range here — reward is a flat level-up + cash on a
# clean win, handled by battle_service.finalize_battle.
BOSS_ROSTER = [
    "Chameleon",
    "Hammerhead",
    "Tombstone",
    "the Shocker",
    "the Scorpion",
    "the Vulture",
    "the Lizard",
    "Rhino",
    "Morbius",
    "Kraven",
    "Electro",
    "Sandman",
    "Hobgoblin",
    "Mysterio",
    "Mister Negative",
    "Doctor Octopus",
    "the Green Goblin",
    "Venom",
    "Carnage",
    "the Sinister Six",
]

# Boss fight difficulty stops climbing once you're past the last named boss (level
# 100) — leveling itself never stops, but nothing makes the fight itself any harder
# past this point. Nothing in the UI ever announces the cap; it's silent.
MAX_NAMED_BOSS_BRACKET = len(BOSS_ROSTER)
MAX_BOSS_DIFFICULTY_LEVEL = BOSS_LEVEL_INTERVAL * MAX_NAMED_BOSS_BRACKET

# "The boss is waiting for you" flavor, themed per villain — shown both when a
# fight's about to start and when the player's turned away for not being at 100%
# suit integrity yet. Keyed by the roster name above.
BOSS_FLAVOR: dict[str, list[str]] = {
    "Chameleon": [
        "Someone's been walking around Queens wearing your face. Chameleon wants a word — in person, for once.",
        "A dozen witnesses swear they saw you somewhere you weren't. Chameleon's done pretending to be anyone else tonight.",
        "He's stopped hiding. Chameleon's waiting on a rooftop off Roosevelt, and this time he's just himself.",
    ],
    "Hammerhead": [
        "Hammerhead's called a meeting at the old rail yard, and you're the only invite that matters.",
        "A steel skull and a Prohibition-era grudge — Hammerhead's holding down a warehouse on the docks, waiting.",
        "The old families don't forget. Hammerhead's parked outside a shuttered social club, and he's not leaving without a fight.",
    ],
    "Tombstone": [
        "Tombstone's cleared out a whole block downtown. He's waiting in the middle of it, arms crossed.",
        "Word is Tombstone's taken over a parking garage on 9th — and he's expecting company.",
        "Pale, huge, and patient — Tombstone's standing in an empty lot like he owns the block. He might.",
    ],
    "the Shocker": [
        "Shocker's cracking a vault downtown, gauntlets humming, and he already knows you're coming.",
        "An armored car's been flipped on its side outside a bank — Shocker's standing on top of it, waiting.",
        "The vibrations start before you even land. Shocker's rigged the whole block to shake.",
    ],
    "the Scorpion": [
        "He's been tailing you for weeks. Scorpion's done following — he's waiting on the fire escape outside your window.",
        "A tail whips into view before you even see him. Scorpion's picked the rooftop, and he's picked you.",
        "Gargan's finally stopped playing PI. Scorpion's coiled on a water tower, waiting for you to land.",
    ],
    "the Vulture": [
        "Toomes has circled the same junkyard for three nights straight. Vulture's waiting on the scrap heap.",
        "Something huge is circling above the bridge cables. Vulture's not hiding tonight.",
        "A rooftop full of stolen tech and one very old, very patient man. Vulture's waiting for you up there.",
    ],
    "the Lizard": [
        "Something's moving through the sewers under ESU, and it used to be a person. Lizard's waiting below.",
        "Dr. Connors' lab is trashed, and something with claws left through the vents. It's waiting in the tunnels.",
        "The reptile house at the zoo went quiet an hour ago. Lizard's in there now, and it's not an animal problem.",
    ],
    "Rhino": [
        "A construction site on 3rd is a pile of rubble, and Rhino's standing in the middle of it, breathing hard.",
        "He put a bus through a storefront just to get your attention. Rhino's waiting in the wreckage.",
        "The ground's still shaking. Rhino charged straight through six blocks and stopped right where you'd find him.",
    ],
    "Morbius": [
        "A blood bank's been cleaned out overnight. Morbius is waiting in the dark storage room, still hungry.",
        "Something pale and fast has been seen on hospital rooftops. Morbius doesn't need an invitation.",
        "The night shift at Metro-General won't go back in. Morbius is still down in the basement.",
    ],
    "Kraven": [
        "Kravinoff's turned Central Park into his hunting ground, and he's been tracking you all week.",
        "A trophy wall's missing one name. Kraven's waiting in the treeline, rifle down, hands empty — he wants this fair.",
        "He's been calling it 'the hunt of a lifetime' to anyone who'll listen. Kraven's waiting, and he means it.",
    ],
    "Electro": [
        "Every light on the block just died at once. Electro's standing in the dark substation, glowing.",
        "A blackout's spreading from Midtown outward, and it's got a source. Electro's at the center of it.",
        "The power grid's screaming. Electro's perched on a transformer station, arcing like a storm cloud.",
    ],
    "Sandman": [
        "A construction site collapsed into itself — into him. Sandman's waiting in the rubble, grinning.",
        "The beach at Coney Island is one giant Flint Marko right now. Sandman's not going anywhere.",
        "Half a concrete mixer's worth of sand just stood up and started walking. Sandman's waiting downtown.",
    ],
    "Hobgoblin": [
        "A gala on Fifth Avenue just got crashed by something on a glider. Hobgoblin's circling the rooftop.",
        "Kingsley's dropped the fashion-designer act. Hobgoblin's waiting on the penthouse terrace, pumpkin bombs in hand.",
        "Every window on the 40th floor just blew out at once. Hobgoblin's up there, laughing about it.",
    ],
    "Mysterio": [
        "Green fog's rolling through an empty film set, and none of it makes sense. Mysterio's waiting inside it.",
        "Every mirror on the block is showing something different. Mysterio's behind the glass somewhere.",
        "A 'movie shoot' downtown has no crew, no cameras, and one very theatrical man waiting in the smoke.",
    ],
    "Mister Negative": [
        "The shelter on Pell Street just went dark, lights and all. Mister Negative's waiting behind the counter.",
        "Chinatown's gone quiet in a way that means something. Li's stopped smiling — Mister Negative hasn't.",
        "A charity dinner turned into something else entirely. Mister Negative's still sitting at the head table.",
    ],
    "Doctor Octopus": [
        "ESU's reactor wing is glowing where it shouldn't be. Doctor Octopus is waiting with all four arms out.",
        "Four steel tentacles just tore through a lab wall. Otto's not interested in hiding anymore.",
        "Something's humming under Oscorp's old annex, and it's got a very particular, very angry inventor attached to it.",
    ],
    "the Green Goblin": [
        "Every light in Oscorp Tower is on at 3am. Osborn's waiting at the top, glider idling.",
        "A pumpkin bomb went off over Midtown just to get your attention. Green Goblin's circling, waiting for you to look up.",
        "Norman's stopped hiding behind the company. Green Goblin's on the tower ledge, and he's not smiling.",
    ],
    "Venom": [
        "Something black and wrong is crawling up the side of the Daily Bugle. Venom's waiting on the roof.",
        "Eddie Brock's byline hasn't run in weeks. What's wearing him now is waiting in the alley behind the building.",
        "A shape with too many teeth just introduced itself to a mugger and left nothing behind. Venom's still out there.",
    ],
    "Carnage": [
        "The precinct's in lockdown and it's already too late. Carnage is loose, and he's not hiding it.",
        "Blood-red and laughing, Cletus tore out of holding twenty minutes ago. Carnage doesn't do quiet.",
        "There's no negotiating with this one. Carnage is waiting exactly where he wants to be found.",
    ],
    "the Sinister Six": [
        "They've all shown up at once — every name you've ever put down, standing in the same place. This is different.",
        "It was never six separate problems. Tonight, it's one very bad night, all at once.",
        "Every rooftop around you has someone on it. The Sinister Six aren't waiting to ambush you — they're waiting for you to notice.",
    ],
}


def boss_name(bracket: int) -> str:
    """bracket = user.boss_clears + 1 — which gate is currently active."""
    idx = (bracket - 1) % len(BOSS_ROSTER)
    cycle = (bracket - 1) // len(BOSS_ROSTER)
    name = BOSS_ROSTER[idx]
    return name if cycle == 0 else f"{name} (Round {cycle + 1})"


def boss_flavor_lines(bracket: int) -> list[str]:
    idx = (bracket - 1) % len(BOSS_ROSTER)
    return BOSS_FLAVOR[BOSS_ROSTER[idx]]


def boss_difficulty_level(user: User) -> int:
    """The reputation level boss combat stats scale against — same as the gate
    level, except it stops climbing past MAX_BOSS_DIFFICULTY_LEVEL (level 100).
    Leveling and boss_clears keep incrementing normally past that; only the fight
    itself stays frozen at its hardest, so very high accounts don't run into
    unwinnable fights."""
    return min(next_boss_gate_level(user), MAX_BOSS_DIFFICULTY_LEVEL)


@dataclass
class PatrolResult:
    """Covers the non-combat outcomes (nothing / scenic). Crime encounters are
    handled by services/battle_service.py's interactive flow instead — see
    cogs/patrol_cog.py for how the two connect."""

    outcome_key: str
    flavor: str
    xp_gained: int = 0
    cash_gained: int = 0
    crime_level: int = 0
    crime_level_delta: int = 0
    hazard_flavor: str | None = None
    hazard_cash: int = 0
    web_fluid_used: bool = False
    web_fluid_tax: int = 0
    organic_webbing_active: bool = False
    # Whether the vial-free patrol above is the Biomorphic grade rather than the
    # Organic one. Purely a naming/attribution distinction — Biomorphic is what
    # Organic grows into, so the vial-free behaviour is identical and this only
    # decides which perk the card credits. Never true unless organic_webbing_active is.
    biomorphic_webbing_active: bool = False
    ally_xp_bonus: bool = False
    difficulty_level: int = 1
    # How much of cash_gained came from Biomorphic Webbing (Symbiote+ perk), so the
    # card can attribute it instead of folding it invisibly into one bigger number —
    # the perk-visibility rule in GAME_DESIGN.md §9. 0 means the roll didn't fire.
    biomorphic_cash: int = 0


@dataclass
class PatrolStart:
    """What every /patrol call resolves first, before branching on outcome type."""

    outcome: dict
    flavor: str
    web_fluid_used: bool
    web_fluid_tax: int
    organic_webbing_active: bool
    biomorphic_webbing_active: bool
    difficulty: float
    xp_multiplier: float


async def _begin(session: AsyncSession, user: User, outcome: dict, tier_rank: int = TIER_RANK_NONE) -> PatrolStart:
    # Organic Webbing (Arachnid+ perk) — Peter's body produces its own web fluid
    # now, full stop. Not a chance roll: patrol NEVER touches vial inventory or the
    # no-fluid cash tax for an Arachnid+ subscriber. /lab brew still works exactly
    # as before — vials made that way are free to sell, just never spent by their
    # own patrols. web_fluid_used stays a separate flag from organic_webbing_active
    # since "-1 vial" would be a lie here — no vial was ever touched.
    #
    # At Symbiote the same behaviour is Biomorphic Webbing instead: Organic is its
    # prerequisite and Biomorphic does everything it does plus the bonus rolls below,
    # so this is one perk with two grades, not two perks that stack. Only the name
    # the card credits changes, which is why the flag below is separate and derived.
    organic_webbing_active = tier_rank >= TIER_RANK_ARACHNID
    biomorphic_webbing_active = tier_rank >= TIER_RANK_SYMBIOTE
    if organic_webbing_active:
        web_fluid_used = True
        web_fluid_tax = 0
    else:
        web_fluid_used = await remove_item(session, user.discord_id, WEB_FLUID_ITEM_KEY, WEB_FLUID_PER_PATROL)
        web_fluid_tax = 0
        if not web_fluid_used:
            web_fluid_tax = rand_range(NO_WEB_FLUID_TAX_RANGE)
            await add_wallet(session, user, -web_fluid_tax, reason="patrol:no_web_fluid")

    difficulty = difficulty_multiplier(user.reputation_level)
    flavor = random.choice(outcome["flavor"])
    xp_multiplier = await reputation_xp_multiplier(session, user.discord_id)

    await session.commit()
    return PatrolStart(
        outcome=outcome,
        flavor=flavor,
        web_fluid_used=web_fluid_used,
        organic_webbing_active=organic_webbing_active,
        biomorphic_webbing_active=biomorphic_webbing_active,
        web_fluid_tax=web_fluid_tax,
        difficulty=difficulty,
        xp_multiplier=xp_multiplier,
    )


async def begin_patrol(session: AsyncSession, user: User, tier_rank: int = TIER_RANK_NONE) -> PatrolStart:
    outcome = _roll_patrol_outcome(user.crime_level, tier_rank)
    return await _begin(session, user, outcome, tier_rank)


async def begin_boss_patrol(session: AsyncSession, user: User, tier_rank: int = TIER_RANK_NONE) -> PatrolStart:
    """Same setup cost as a normal patrol (web fluid, ally XP multiplier) but the
    outcome isn't rolled — you're heading straight for the boss guarding your next
    reputation gate, with flavor themed to that specific villain. xp/cash both come
    from the fixed boss-clear reward in finalize_battle, not from a rolled range, so
    they're zeroed here. difficulty is overridden after _begin() to the frozen boss
    curve instead of the player's raw (possibly past-100) reputation level."""
    bracket = user.boss_clears + 1
    outcome = {"key": "boss", "flavor": boss_flavor_lines(bracket), "xp": [0, 0]}
    start = await _begin(session, user, outcome, tier_rank)
    start.difficulty = difficulty_multiplier(boss_difficulty_level(user))
    return start


def compute_base_xp(start: PatrolStart) -> int:
    return round(rand_range(start.outcome["xp"]) * start.xp_multiplier * start.difficulty)


async def finish_noncombat_patrol(
    session: AsyncSession, user: User, start: PatrolStart, tier_rank: int = TIER_RANK_NONE
) -> PatrolResult:
    """Resolves "nothing" and "scenic" outcomes instantly — no battle needed."""
    outcome = start.outcome
    xp = compute_base_xp(start)

    result = PatrolResult(
        outcome_key=outcome["key"],
        flavor=start.flavor,
        web_fluid_used=start.web_fluid_used,
        web_fluid_tax=start.web_fluid_tax,
        organic_webbing_active=start.organic_webbing_active,
        biomorphic_webbing_active=start.biomorphic_webbing_active,
        ally_xp_bonus=start.xp_multiplier > 1.0,
        difficulty_level=user.reputation_level,
    )

    if "cash" in outcome:
        result.cash_gained = rand_range(outcome["cash"])
        if tier_rank >= TIER_RANK_SYMBIOTE and random.random() < BIOMORPHIC_WEBBING_CASH_CHANCE:
            result.biomorphic_cash = rand_range(BIOMORPHIC_WEBBING_CASH_RANGE)
            result.cash_gained += result.biomorphic_cash
        await add_wallet(session, user, result.cash_gained, reason=f"patrol:{outcome['key']}")

    # The actual applied amount, not the pre-penalty/pre-cap roll — a crime penalty
    # or a boss-gate ceiling can both silently shrink this below `xp`.
    result.xp_gained = await add_reputation(session, user, xp)
    crime_decay = rand_range(CRIME_LEVEL_DECAY_RANGE)
    user.crime_level = max(0, user.crime_level - crime_decay)
    result.crime_level_delta = -crime_decay

    hazard = await roll_hazard(session, user.discord_id)
    if hazard is not None:
        rolled_hazard_cash = rand_range(hazard["cash"])
        # add_wallet clamps at 0 and returns what actually happened — a broke
        # player's wallet was never really overdrawn, the display just used to
        # show the raw roll instead of the real (possibly smaller) deduction.
        result.hazard_flavor = hazard["flavor"]
        result.hazard_cash = await add_wallet(session, user, rolled_hazard_cash, reason=f"hazard:{hazard['key']}")

    result.crime_level = user.crime_level
    await session.commit()
    return result


def _roll_patrol_outcome(crime_level: int, tier_rank: int = TIER_RANK_NONE) -> dict:
    """Higher city crime_level (built up by skipping patrol for /tutoring) skews the
    odds toward crime encounters — more photo/donation opportunity, but more risk too.
    Combat-Ready Patrols (Arachnid+ perk) adds a flat bonus on top, same lever, not a
    guarantee — even crime_level maxed at 100 only reaches ~66% combat odds in the
    base game, so a 100% guarantee would exceed what's possible for anyone else by
    design. +15 flat gets a booster to ~55% at crime_level 0, ~72% stacked with a
    maxed crime_level."""
    biased = []
    for entry in LOOT_TABLES["patrol"]:
        weight = entry["weight"]
        if entry["key"] in ("crime_bronze", "crime_silver", "crime_gold"):
            weight += crime_level * CRIME_LEVEL_WEIGHT_BONUS
            if tier_rank >= TIER_RANK_ARACHNID:
                weight += COMBAT_READY_PATROLS_WEIGHT_BONUS
        biased.append({**entry, "weight": weight})
    return weighted_choice(biased)


async def get_equipped_camera(session: AsyncSession, user_id: int) -> InventoryItem | None:
    stmt = select(InventoryItem).where(
        InventoryItem.user_id == user_id,
        InventoryItem.item_key.in_(CAMERA_FAMILY_KEYS),
        InventoryItem.equipped.is_(True),
    )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return None
    return max(rows, key=lambda r: CAMERA_FAMILY_KEYS.index(r.item_key))


def camera_tier_stats(item_key: str) -> dict:
    return CAMERA_TIER_STATS.get(item_key, CAMERA_TIER_STATS[CAMERA_ITEM_KEY])


def bump_photo_quality(quality: str) -> str:
    idx = PHOTO_QUALITY_ORDER.index(quality)
    return PHOTO_QUALITY_ORDER[min(idx + 1, len(PHOTO_QUALITY_ORDER) - 1)]


def roll_donation() -> dict | None:
    for donation in LOOT_TABLES["donations"]:
        if random.random() < donation["chance"]:
            return donation
    return None


async def roll_hazard(session: AsyncSession, user_id: int) -> dict | None:
    for hazard in LOOT_TABLES["hazards"]:
        chance = hazard["chance"]
        ally_key = HAZARD_ALLY_KEY.get(hazard["key"])
        if ally_key is not None:
            happiness = await get_current_happiness(session, user_id, ally_key)
            if happiness < LOW_HAPPINESS_THRESHOLD:
                chance *= NEGLECT_HAZARD_MULTIPLIER
        if random.random() < chance:
            return hazard
    return None
