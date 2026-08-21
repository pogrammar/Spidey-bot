from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PendingPhoto, User
from services.economy import add_reputation, add_wallet, next_boss_gate_level
from services.gadget_service import list_equipped_gadgets, roll_gadget_effect, roll_gadget_wearout
from services.inventory_service import add_item
from services.loot_tables import rand_range
from services.patreon_service import TIER_RANK_ARACHNID, TIER_RANK_NONE, TIER_RANK_SYMBIOTE
from services.patrol_service import (
    BIOMORPHIC_WEBBING_CASH_CHANCE,
    BIOMORPHIC_WEBBING_CASH_RANGE,
    CRIME_LEVEL_DECAY_RANGE_LOSS,
    CRIME_LEVEL_DECAY_RANGE_WIN,
    get_equipped_camera,
    roll_donation,
    roll_hazard,
)
from utils.icons import emoji
from utils.leveling import xp_for_level

# Round count is rolled per-battle (see start_battle) instead of fixed, for pacing
# variety — but it's picked once up front and shown to the player from round 1, so
# there's never hidden information mid-fight, only variety between fights.
ROUND_RANGE = [5, 6, 7]
BASELINE_ROUNDS = 3  # what the base_hp values below, and every other balance number, were tuned against

# Boss fights don't roll a round count — always exactly 10, no variance, so the
# fight itself reads as a real set-piece rather than a longer crime encounter.
BOSS_ROUND_COUNT = 10

# How much extra enemy HP (beyond straight difficulty scaling) each round beyond
# BASELINE_ROUNDS adds, per tier: hp_ratio = 1 + slope * (num_rounds - BASELINE_ROUNDS).
# More rounds means less variance (law of large numbers), which cuts both ways: it
# pulls bronze (already averaging *above* its required per-round damage rate) toward
# an even higher win rate, and pulls gold (already averaging *below* its rate) toward
# an even lower one. Naive proportional HP scaling (just num_rounds/BASELINE_ROUNDS)
# doesn't correct for that — empirically verified via binary search across the full
# level range that these two slopes are what it actually takes to hold each tier's
# win rate steady as round count changes, not just scale HP with round count.
# crime_silver's slope (0.315) is a straight linear interpolation between bronze and
# gold, not independently binary-searched like those two were — close enough to hold
# win rate roughly steady across round counts, but revisit if silver's win rate drifts
# noticeably as round count varies. "boss" is 0 — round count never varies for boss
# fights (always BOSS_ROUND_COUNT), so there's no variance for this slope to correct
# for; base_hp below was binary-searched directly against the fixed 10-round fight.
ROUND_HP_SLOPE = {"crime_bronze": 0.36, "crime_silver": 0.315, "crime_gold": 0.27, "boss": 0.0}

ENEMY_STATS = {
    "crime_bronze": {
        "names": [
            "a small-time crook",
            "a couple of muggers",
            "a low-level thug",
            "a jumpy bodega robber",
            "a guy with a crowbar and bad ideas",
        ],
        "base_hp": 28,
        "base_damage": [4, 9],
        "base_hit_chance": 0.5,
        "photo_quality": "bronze",
        "component_key": "spandex_fabric",
        "base_drop_chance": 0.25,
    },
    "crime_silver": {
        "names": [
            "an armed shopkeeper's nightmare",
            "a shakedown crew",
            "a knife-happy car-jacker",
            "a jumpy pawn-shop robber",
            "a gang enforcer having a bad night",
        ],
        "base_hp": 38,
        "base_damage": [6, 12],
        "base_hit_chance": 0.52,
        "photo_quality": "silver",
        "component_key": "micro_electronics",
        "base_drop_chance": 0.15,
    },
    "crime_gold": {
        "names": [
            "a Sable mercenary",
            "an armed crew",
            "serious trouble",
            "one of Fisk's enforcers",
            "someone way better-equipped than they should be",
        ],
        "base_hp": 50,
        "base_damage": [8, 16],
        "base_hit_chance": 0.55,
        "photo_quality": "gold",
        "component_key": "micro_electronics",
        "base_drop_chance": 0.3,
    },
    # Boss fights are triggered directly (see begin_boss_patrol / at_boss_gate),
    # never rolled from the patrol table — "names" isn't used, start_battle gets an
    # explicit enemy_name override instead (the roster in patrol_service.py, picked
    # deterministically by which gate is active).
    #
    # Unlike every other tier, suit integrity IS your HP here — hitting 0% mid-fight
    # is an immediate loss (see PatrolBattleView._advance's "suit_depleted" branch in
    # cogs/patrol_cog.py), not just cosmetic damage. That makes enemy hit_chance and
    # damage a direct survival threat, not only a DPS-race backdrop, which is the
    # main lever that makes these genuinely hard rather than a bigger crime_gold.
    # Binary-searched (see scratch/boss_tune2.py) against the fixed 10-round fight
    # (BOSS_ROUND_COUNT) with ALL owned gadgets usable (not just the 2-equipped
    # loadout — see resolve_gadget's all_owned flag below) and a policy that uses
    # defensive gadgets (web_shooters, concussion_burst) proactively rather than
    # just round-robining everything — a real "use the right gadget at the right
    # time" playstyle matters a lot here. At the frozen max difficulty (level 100 —
    # see patrol_service.boss_difficulty_level): a run with zero gadgets is
    # essentially unwinnable (~0-1%) past the first boss, a realistic
    # gadget-for-your-bracket loadout at upgrade level 1 sits around 17-25%, and a
    # fully-owned, fully-upgraded kit lands ~70-75% — hard, but clearly worth
    # investing in. The very first boss (bracket 1, only 2 gadgets unlocked yet) is
    # noticeably softer at ~58-60% regardless of upgrade level, since there's only
    # so much kit available that early.
    "boss": {
        "names": ["a real threat"],
        "base_hp": 100,
        "base_damage": [12, 22],
        "base_hit_chance": 0.58,
        "photo_quality": "gold",
        "component_key": "micro_electronics",
        "base_drop_chance": 0.4,
    },
}

ATTACK_HIT_CHANCE = 0.75
ATTACK_DAMAGE = {"crime_bronze": [10, 18], "crime_silver": [11, 20], "crime_gold": [12, 22], "boss": [12, 22]}

# Flat, guaranteed reward for beating a boss (never rolled from the fight itself,
# unlike crime-tier XP/cash) — scaled by the same difficulty curve as everything
# else, so a level-20 boss pays more than the level-5 one.
BOSS_CASH_RANGE = [300, 500]
# Enemy HP scales faster than attack damage (90% of the rate) — deliberate, winning
# still gets *harder* at higher levels. What changed: raw reputation-level difficulty
# used to feed straight into enemy HP/damage uncapped, which meant gold crimes hit a
# real wall around level 25-30 and stayed at ~0% win rate all the way to level 100 —
# not "hard," mathematically closed off regardless of gear. COMBAT_DIFFICULTY_* below
# soft-caps the difficulty used for win/loss math specifically (enemy HP, enemy
# damage/hit-chance, attack scaling) so the curve keeps climbing past the threshold
# but flattens toward the ceiling instead of running away forever. Only affects combat
# stats — gadget wearout and camera-break odds still key off the raw, uncapped value.
ATTACK_DAMAGE_DIFFICULTY_SCALE = 0.90
COMBAT_DIFFICULTY_SOFT_CAP_THRESHOLD = 2.5
COMBAT_DIFFICULTY_SOFT_CAP_CEILING = 3.2


def _combat_difficulty(raw_difficulty: float) -> float:
    if raw_difficulty <= COMBAT_DIFFICULTY_SOFT_CAP_THRESHOLD:
        return raw_difficulty
    excess = raw_difficulty - COMBAT_DIFFICULTY_SOFT_CAP_THRESHOLD
    max_excess = COMBAT_DIFFICULTY_SOFT_CAP_CEILING - COMBAT_DIFFICULTY_SOFT_CAP_THRESHOLD
    return COMBAT_DIFFICULTY_SOFT_CAP_THRESHOLD + max_excess * (1 - math.exp(-excess / max_excess))
EVADE_DAMAGE_MULTIPLIER = 0.25  # incoming damage reduced to 25% if you're still caught evading

# Evade doesn't deal damage itself, but it reads the enemy's rhythm — the next Attack
# you throw is a guaranteed hit for bonus damage. Makes Evade->Attack a real second
# path to winning instead of Attack being the only button that matters for victory.
COMBO_HIT_CHANCE = 1.0
COMBO_DAMAGE_MULTIPLIER = 1.5

CLEAN_WIN_XP_BONUS = [5, 12]
CLEAN_WIN_CASH_BONUS = [10, 25]

# suit-integrity-based scavenge urgency, same curve as the old single-roll patrol
SCAVENGE_URGENCY_THRESHOLD = 50
SCAVENGE_URGENCY_BONUS_MAX = 0.25

# walking into a fight already at 0% integrity means no protection at all
UNPROTECTED_INJURY_RANGE = [20, 50]
UNPROTECTED_CAMERA_BREAK_BONUS = 0.15

# Enhanced Strength (Arachnid+ Patreon perk) — bonus Attack damage, crime-tier
# patrols only. Boss fights excluded for the same reason Enhanced Resilience (the
# earlier, now-replaced version of this perk) excluded them: that difficulty curve
# is tuned around full-strength numbers, and buffing damage output there risks the
# same balance fragility the Venom Blast tuning already ran into. 30% matches the
# same convention every other perk in this set uses.
ENHANCED_STRENGTH_DAMAGE_BONUS = 0.3

# Biomorphic Webbing (Symbiote+ Patreon perk) — passive scavenge boost across
# cash/components/photos, applies to any combat encounter (crime tiers and boss
# fights alike). Three independent rolls rather than one shared roll, since the
# copy promises "coins, photos, AND parts" — meant to feel like occasional small
# extras, not a guaranteed bonus every fight. Cash chance/range live in
# patrol_service.py since the same bonus also applies to non-combat patrols.
BIOMORPHIC_WEBBING_COMPONENT_CHANCE = 0.20  # only rolled if the normal drop_chance roll missed
BIOMORPHIC_WEBBING_PHOTO_CHANCE = 0.20  # only rolled if a camera's equipped and a photo was already banked

# Sonic Dampener (Symbiote drawback, not a perk) — scoped to just "the Shocker"
# (see patrol_service.py's BOSS_ROSTER), the only one of the 20 named bosses that
# thematically fits, since all 20 mechanically share one identical stat block —
# no per-boss attack-type system exists to gate a broader "any sonic boss"
# version. +30% mirrors Enhanced Resilience's -30% as the same-shape opposite,
# a real cost for the tier rather than pure upside.
SONIC_DAMPENER_BOSS_NAME = "the Shocker"
SONIC_DAMPENER_DAMAGE_INCREASE = 0.3

# Round-by-round flavor — randomized per line so repeated battles don't read identical
# every time. `{dmg}` / `{enemy}` get filled in where present.
ATTACK_HIT_LINES = [
    "You land a hit for {dmg} damage.",
    "Clean connect — {dmg} damage.",
    "Right on target, {dmg} damage.",
    "You tag them for {dmg} damage.",
    "Web-assisted haymaker, {dmg} damage.",
    "You thread the needle for {dmg} damage.",
    "Straight out of a J. Jonah Jameson nightmare — {dmg} damage.",
    "That's gonna leave a mark, {dmg} damage.",
    "Aunt May would not approve — {dmg} damage.",
    "Textbook takedown, {dmg} damage.",
    "You barely even aimed and still land {dmg} damage.",
    "Parker Luck takes the day off — {dmg} damage.",
    "That one's going in the scrapbook, {dmg} damage.",
    "You catch them mid-monologue for {dmg} damage.",
]
ATTACK_MISS_LINES = [
    "You swing and miss.",
    "They slip the punch.",
    "Wide open, and you still miss.",
    "Your shot goes wide.",
    "Parker Luck strikes again.",
    "You trip over literally nothing.",
    "Your web-shooter picks now to hiccup.",
    "Swing and a miss — this isn't baseball, Peter.",
    "You overthink it and pay for it.",
    "That one's for the blooper reel.",
    "You lunge, they're already gone.",
    "Full commitment, zero contact.",
    "You hit everything except them.",
    "Somewhere, J. Jonah Jameson is laughing.",
]
COMBO_HIT_LINES = [
    "You capitalize on the opening for {dmg} damage!",
    "You saw that coming — {dmg} damage, clean.",
    "Perfect timing, {dmg} damage.",
    "They never saw it coming — {dmg} damage.",
    "Called shot, {dmg} damage!",
    "You read them like a comic book — {dmg} damage.",
    "That's what the setup was for — {dmg} damage!",
    "Picture-perfect follow-through, {dmg} damage.",
    "You cash in the opening for {dmg} damage.",
    "No hesitation — {dmg} damage, right on cue.",
    "They walked right into it, {dmg} damage.",
    "Textbook combo, {dmg} damage — Aunt May would still worry.",
    "You make it look easy, {dmg} damage.",
    "Punch line delivered — {dmg} damage.",
]
ENEMY_HIT_LINES = [
    " {enemy} hits back, -{dmg}% suit.",
    " {enemy} catches you, -{dmg}% suit.",
    " {enemy} gets a piece of you, -{dmg}% suit.",
    " {enemy} clips you good, -{dmg}% suit.",
    " {enemy} makes you regret that, -{dmg}% suit.",
    " {enemy} finds a gap in the stitching, -{dmg}% suit.",
    " {enemy} tags the homemade suit, -{dmg}% suit.",
    " {enemy} isn't playing fair, -{dmg}% suit.",
    " {enemy} gets a lucky shot in, -{dmg}% suit.",
    " {enemy} makes you earn this one, -{dmg}% suit.",
    " {enemy} reminds you why the suit matters, -{dmg}% suit.",
]
ENEMY_WHIFF_LINES = [
    " {enemy} whiffs.",
    " {enemy} swings and misses.",
    " {enemy} can't connect.",
    " {enemy} grabs air.",
    " {enemy} overcommits and pays for it.",
    " {enemy} doesn't even come close.",
    " {enemy} trips over their own feet.",
    " {enemy} needs to lay off the trash talk.",
    " {enemy} swings at a ghost.",
    " {enemy} looks embarrassed about that one.",
    " {enemy} really thought that one would land.",
]
EVADE_CLEAN_LINES = [
    "You dodge clean — no damage taken.",
    "Not even close — you're already gone.",
    "You're a blur — nothing lands.",
    "Spider-sense earns its keep — nothing lands.",
    "You're not even there anymore.",
    "Gone before they finish the swing.",
    "You make it look like slow motion for them.",
    "Not a scratch — textbook evasion.",
    "You're three steps ahead the whole time.",
    "They swing at where you used to be.",
    "Web-slinger reflexes, zero damage.",
]
EVADE_GRAZE_LINES = [
    "You dodge clear but catch a graze, -{dmg}% suit.",
    "Mostly clear, but they clip you, -{dmg}% suit.",
    "You slip most of it — still stings, -{dmg}% suit.",
    "Almost a clean dodge — almost, -{dmg}% suit.",
    "You get most of the way clear, -{dmg}% suit.",
    "Close, but not close enough, -{dmg}% suit.",
    "You dodge the worst of it, -{dmg}% suit.",
    "Nearly a perfect read — nearly, -{dmg}% suit.",
    "You shave off most of the impact, -{dmg}% suit.",
]
COMBO_SETUP_LINES = [
    "You read their rhythm — next Attack is a sure thing.",
    "You've got their timing now — next Attack lands clean.",
    "You clock their pattern — next Attack won't miss.",
    "You've got them figured out — next Attack is a lock.",
    "Their next move is already telegraphed — next Attack connects.",
    "You bank the intel — next Attack is guaranteed.",
    "Homework pays off — next Attack lands clean.",
    "You've seen this pattern before — next Attack is a sure thing.",
    "You're already reading their next move — next Attack won't miss.",
]
GADGET_FUMBLE_LINES = [
    "Your gadget fumbles — no effect this round.",
    "Jammed — nothing happens.",
    "Bad timing, the gadget doesn't fire.",
    "Tony Stark would be embarrassed for you — nothing happens.",
    "The gadget picks the worst possible moment to malfunction.",
    "Homemade tech, homemade problems — no effect this round.",
    "You fumble the trigger — nothing happens.",
    "Not today, apparently — the gadget stays quiet.",
    "Some assembly required, apparently — nothing fires.",
]


@dataclass
class BattleState:
    outcome_key: str
    difficulty: float
    enemy_name: str
    enemy_hp: int
    enemy_max_hp: int
    enemy_damage_range: list[int]
    enemy_hit_chance: float
    attack_damage_range: list[int]
    starting_suit_integrity: int
    entered_unprotected: bool
    base_xp: int
    base_cash: int
    available_gadgets: list[tuple[str, str]]  # (item_key, name) — your loadout, up to 2
    round_number: int = 1
    max_rounds: int = BASELINE_ROUNDS
    total_suit_damage: int = 0
    bonus_xp: int = 0
    bonus_cash: int = 0
    scavenge_bonus: float = 0.0
    hits_taken: int = 0
    broken_gadget_keys: set[str] = field(default_factory=set)
    gadgets_broken: list[str] = field(default_factory=list)  # names, for the final report
    combo_ready: bool = False  # set by a successful Evade, consumed by the next Attack
    venom_blast_used: bool = False  # once-per-boss-fight — see _apply_counter_with_venom_blast
    ended: bool = False
    end_reason: str | None = None  # "won" | "rounds_exhausted" | "timeout"
    log: list[str] = field(default_factory=list)

    @property
    def suit_remaining(self) -> int:
        return max(0, self.starting_suit_integrity - self.total_suit_damage)


@dataclass
class BattleReport:
    won_clean: bool
    xp_gained: int
    cash_gained: int
    suit_damage: int
    photo_banked: bool
    photo_quality: str | None
    camera_broke: bool
    item_found: str | None
    gadgets_broken: list[str]
    unprotected_penalty: int
    donation_flavor: str | None
    donation_cash: int
    hazard_flavor: str | None
    hazard_cash: int
    crime_level: int
    crime_level_delta: int = 0
    boss_cash_reward: int = 0
    boss_new_level: int | None = None


def start_battle(
    outcome_key: str,
    difficulty: float,
    starting_suit_integrity: int,
    base_xp: int,
    base_cash: int,
    available_gadgets: list[tuple[str, str]],
    enemy_name: str | None = None,
) -> BattleState:
    stats = ENEMY_STATS[outcome_key]
    combat_difficulty = _combat_difficulty(difficulty)

    num_rounds = BOSS_ROUND_COUNT if outcome_key == "boss" else random.choice(ROUND_RANGE)
    hp_ratio = 1 + ROUND_HP_SLOPE[outcome_key] * (num_rounds - BASELINE_ROUNDS)
    enemy_hp = round(stats["base_hp"] * combat_difficulty * hp_ratio)

    dmg_lo, dmg_hi = stats["base_damage"]
    enemy_damage_range = [max(1, round(dmg_lo * combat_difficulty)), max(2, round(dmg_hi * combat_difficulty))]
    enemy_hit_chance = min(0.8, stats["base_hit_chance"] + (combat_difficulty - 1) * 0.1)

    atk_lo, atk_hi = ATTACK_DAMAGE[outcome_key]
    atk_scale = 1 + (combat_difficulty - 1) * ATTACK_DAMAGE_DIFFICULTY_SCALE
    attack_damage_range = [round(atk_lo * atk_scale), round(atk_hi * atk_scale)]

    return BattleState(
        outcome_key=outcome_key,
        # Raw (uncapped) difficulty — gadget wearout and camera-break odds key off
        # this later and are meant to keep climbing with real level, unlike the
        # win/loss combat stats above which use the soft-capped value.
        difficulty=difficulty,
        enemy_name=enemy_name or random.choice(stats["names"]),
        enemy_hp=enemy_hp,
        enemy_max_hp=enemy_hp,
        enemy_damage_range=enemy_damage_range,
        enemy_hit_chance=enemy_hit_chance,
        attack_damage_range=attack_damage_range,
        starting_suit_integrity=starting_suit_integrity,
        entered_unprotected=starting_suit_integrity <= 0,
        base_xp=base_xp,
        base_cash=base_cash,
        available_gadgets=available_gadgets,
        max_rounds=num_rounds,
    )


# Arachnid+ Patreon perks — proc chances sit in the same range as existing gadget
# base_chance values (0.25-0.55 in gadget_service.py's GADGET_EFFECTS), pulled
# toward the low end since these are always-on and cost no loadout slot, unlike a
# gadget you had to choose to equip.
ELECTRIC_WEBBING_PROC_CHANCE = 0.20
ELECTRIC_WEBBING_BONUS_DAMAGE_RANGE = [8, 15]
# Every perk line below ends with the Arachnid tag — same rule the user set for
# every Arachnid perk: whenever one actually fires, the emoji + "Arachnid" has to
# be visibly attached to it, not just a flavor line that happens to imply it.
ELECTRIC_WEBBING_LINES = [
    " Your webbing crackles and locks them up — no counter.",
    " A jolt of electric webbing fries their footing — no counter.",
    " The web-shock catches them cold — no counter.",
]

SPIDER_BOTS_PROC_CHANCE = 0.20
SPIDER_BOTS_BONUS_DAMAGE_RANGE = [5, 12]
SPIDER_BOTS_LINES = [
    " A spider-bot darts in for {dmg} extra damage.",
    " One of your drones gets a free hit in for {dmg}.",
    " A spider-bot swarms past you for {dmg} bonus damage.",
]


def _arachnid_tag() -> str:
    return f" ({emoji('arachnid') or ''} Arachnid)".replace("  ", " ")


def roll_spider_bots(state: BattleState, tier_rank: int) -> str:
    """Passive, always-on — no action cost, doesn't interact with the counter at
    all, just adds bonus damage if the enemy's still standing. Safe to call after
    any round-ending action (Attack/Evade/Gadget)."""
    if tier_rank < TIER_RANK_ARACHNID or state.enemy_hp <= 0:
        return ""
    if random.random() >= SPIDER_BOTS_PROC_CHANCE:
        return ""
    dmg = rand_range(SPIDER_BOTS_BONUS_DAMAGE_RANGE)
    state.enemy_hp = max(0, state.enemy_hp - dmg)
    return random.choice(SPIDER_BOTS_LINES).format(dmg=dmg) + _arachnid_tag()


def _enemy_counter(state: BattleState, incoming_multiplier: float = 1.0) -> int:
    if random.random() >= state.enemy_hit_chance:
        return 0
    dmg = rand_range(state.enemy_damage_range)
    return round(dmg * incoming_multiplier)


def _apply_counter(state: BattleState, dmg: int) -> None:
    if dmg > 0:
        state.total_suit_damage += dmg
        state.hits_taken += 1


VENOM_BLAST_LINES = [
    " The symbiote surges up and swallows the hit whole — you blast back twice as hard!",
    " Venom Blast! The blow never lands — the counter alone does more damage than it would've taken.",
    " The bond absorbs everything — and pays it back double.",
]


def _apply_counter_with_venom_blast(state: BattleState, dmg: int, tier_rank: int) -> str:
    """Boss fights only, once per fight. Copy already promises "twice as hard" —
    implementing that literally (2x a normal attack roll) rather than a flat
    number keeps it self-scaling with difficulty instead of needing its own
    separately-tuned constant that could drift out of sync with ATTACK_DAMAGE.

    Also applies Sonic Dampener (Symbiote drawback) before the Venom Blast check,
    so a dampened hit correctly factors into whether Venom Blast would even
    trigger — a real cost, not just cosmetic extra damage after the fact."""
    if tier_rank >= TIER_RANK_SYMBIOTE and dmg > 0 and state.enemy_name == SONIC_DAMPENER_BOSS_NAME:
        dmg = round(dmg * (1 + SONIC_DAMPENER_DAMAGE_INCREASE))

    would_deplete = (
        state.outcome_key == "boss"
        and dmg > 0
        and (state.starting_suit_integrity - state.total_suit_damage - dmg) <= 0
    )
    if tier_rank >= TIER_RANK_SYMBIOTE and not state.venom_blast_used and would_deplete:
        state.venom_blast_used = True
        bonus = rand_range(state.attack_damage_range) * 2
        state.enemy_hp = max(0, state.enemy_hp - bonus)
        return random.choice(VENOM_BLAST_LINES)

    _apply_counter(state, dmg)
    return ""


def resolve_attack(state: BattleState, tier_rank: int = TIER_RANK_NONE) -> str:
    dmg_range = state.attack_damage_range
    comboed = state.combo_ready
    state.combo_ready = False

    hit_chance = COMBO_HIT_CHANCE if comboed else ATTACK_HIT_CHANCE
    landed = random.random() < hit_chance
    if landed:
        dmg = rand_range(dmg_range)
        if comboed:
            dmg = round(dmg * COMBO_DAMAGE_MULTIPLIER)
        # Enhanced Strength (Arachnid+ perk) — crime-tier patrols only, same
        # boss-exclusion reasoning as the perk it replaced (Enhanced Resilience).
        strength_active = tier_rank >= TIER_RANK_ARACHNID and state.outcome_key != "boss"
        if strength_active:
            dmg = round(dmg * (1 + ENHANCED_STRENGTH_DAMAGE_BONUS))
        state.enemy_hp = max(0, state.enemy_hp - dmg)
        template = random.choice(COMBO_HIT_LINES if comboed else ATTACK_HIT_LINES)
        hit_line = template.format(dmg=dmg)
        if strength_active:
            hit_line += " (Enhanced Strength)" + _arachnid_tag()
    else:
        hit_line = random.choice(ATTACK_MISS_LINES)

    if state.enemy_hp <= 0:
        return hit_line

    # Electric Webbing (Arachnid+ perk) — only eligible on a landed hit (it needs
    # contact to shock the target), stuns the enemy so the counter's skipped
    # entirely for the round, same "clean kill skips the counter roll" shape as a
    # finishing blow already gets.
    if landed and tier_rank >= TIER_RANK_ARACHNID and random.random() < ELECTRIC_WEBBING_PROC_CHANCE:
        bonus = rand_range(ELECTRIC_WEBBING_BONUS_DAMAGE_RANGE)
        state.enemy_hp = max(0, state.enemy_hp - bonus)
        hit_line += random.choice(ELECTRIC_WEBBING_LINES) + _arachnid_tag()
        return hit_line

    counter = _enemy_counter(state)
    venom_line = _apply_counter_with_venom_blast(state, counter, tier_rank)
    if venom_line:
        return hit_line + venom_line
    enemy = state.enemy_name.capitalize()
    if counter:
        counter_line = random.choice(ENEMY_HIT_LINES).format(enemy=enemy, dmg=counter)
    else:
        counter_line = random.choice(ENEMY_WHIFF_LINES).format(enemy=enemy)
    return hit_line + counter_line


def resolve_evade(state: BattleState, tier_rank: int = TIER_RANK_NONE) -> str:
    state.combo_ready = True
    counter = _enemy_counter(state, EVADE_DAMAGE_MULTIPLIER)
    venom_line = _apply_counter_with_venom_blast(state, counter, tier_rank)
    if venom_line:
        return venom_line
    base = random.choice(EVADE_GRAZE_LINES).format(dmg=counter) if counter else random.choice(EVADE_CLEAN_LINES)
    return f"{base} {random.choice(COMBO_SETUP_LINES)}"


async def resolve_gadget(
    session: AsyncSession, user_id: int, state: BattleState, gadget_key: str, tier_rank: int = TIER_RANK_NONE
) -> str:
    all_owned = state.outcome_key == "boss"
    effect = await roll_gadget_effect(session, user_id, gadget_key, all_owned=all_owned)
    broken = await roll_gadget_wearout(session, user_id, state.difficulty, gadget_key, all_owned=all_owned)
    if broken:
        state.broken_gadget_keys.add(gadget_key)
        state.gadgets_broken.append(broken)

    if effect is None:
        # Special ability didn't trigger — falls back to a plain attack instead of
        # wasting the round entirely. Using a gadget should never be worse than just
        # clicking Attack, only sometimes better.
        dmg_range = state.attack_damage_range
        fumble_prefix = random.choice(GADGET_FUMBLE_LINES)
        if random.random() < ATTACK_HIT_CHANCE:
            dmg = rand_range(dmg_range)
            state.enemy_hp = max(0, state.enemy_hp - dmg)
            line = f"{fumble_prefix} Still, you land a basic hit for {dmg} damage."
        else:
            line = f"{fumble_prefix} {random.choice(ATTACK_MISS_LINES)}"

        if state.enemy_hp > 0:
            counter = _enemy_counter(state)
            venom_line = _apply_counter_with_venom_blast(state, counter, tier_rank)
            if venom_line:
                line += venom_line
            else:
                enemy = state.enemy_name.capitalize()
                if counter:
                    line += random.choice(ENEMY_HIT_LINES).format(enemy=enemy, dmg=counter)
                else:
                    line += random.choice(ENEMY_WHIFF_LINES).format(enemy=enemy)

        if broken:
            line += f" Worse, your {broken} gives out."
        return line

    dmg_range = state.attack_damage_range
    lines = [f"{effect.gadget_name}!"]

    if effect.kind == "negate_damage":
        lines.append("You block the hit entirely — no damage taken.")
    elif effect.kind == "group_defense":
        dmg = round(rand_range(dmg_range) * 1.1)
        state.enemy_hp = max(0, state.enemy_hp - dmg)
        lines.append(f"{dmg} damage dealt, and the counter's fully blocked.")
    else:
        if effect.kind == "bonus_xp":
            dmg = round(rand_range(dmg_range) * 1.4)
        else:
            dmg = rand_range(dmg_range)
        state.enemy_hp = max(0, state.enemy_hp - dmg)
        lines.append(f"{dmg} damage dealt.")

        if effect.kind == "bonus_donation":
            bonus = rand_range(effect.cash_range or [20, 40])
            state.bonus_cash += bonus
            lines.append(f"A bystander tips you an extra ${bonus}.")
        elif effect.kind == "scavenge_boost":
            state.scavenge_bonus += effect.magnitude or 0.25
            lines.append("Gear tears loose — better scavenging odds this fight.")
        elif effect.kind == "bonus_xp":
            bonus_xp = max(1, round(state.base_xp * (effect.magnitude or 0.5)))
            state.bonus_xp += bonus_xp
            lines.append("That combo's worth extra reputation.")

        if state.enemy_hp > 0:
            counter = _enemy_counter(state)
            venom_line = _apply_counter_with_venom_blast(state, counter, tier_rank)
            if venom_line:
                lines.append(venom_line)
            elif counter:
                lines.append(f"{state.enemy_name.capitalize()} hits back, -{counter}% suit.")

    if broken:
        lines.append(f"Your {broken} gives out from the strain.")

    return " ".join(lines)


async def finalize_battle(
    session: AsyncSession, user: User, state: BattleState, tier_rank: int = TIER_RANK_NONE
) -> BattleReport:
    stats = ENEMY_STATS[state.outcome_key]
    won_clean = state.enemy_hp <= 0 and state.end_reason == "won"

    xp = state.base_xp + state.bonus_xp
    cash = state.base_cash + state.bonus_cash
    if won_clean:
        xp += rand_range(CLEAN_WIN_XP_BONUS)
        cash += rand_range(CLEAN_WIN_CASH_BONUS)

    user.suit_integrity = max(0, user.suit_integrity - state.total_suit_damage)

    unprotected_penalty = 0
    if state.entered_unprotected:
        unprotected_penalty = rand_range(UNPROTECTED_INJURY_RANGE)
        await add_wallet(session, user, -unprotected_penalty, reason="patrol:unprotected_injury")

    photo_banked = False
    camera_broke = False
    camera = await get_equipped_camera(session, user.discord_id)
    if camera is not None:
        session.add(PendingPhoto(user_id=user.discord_id, quality=stats["photo_quality"]))
        photo_banked = True
        break_chance = 0.06 + 0.05 * state.hits_taken
        if state.entered_unprotected:
            break_chance += UNPROTECTED_CAMERA_BREAK_BONUS
        break_chance = min(0.9, break_chance * state.difficulty)
        if random.random() < break_chance:
            camera_broke = True
            await session.delete(camera)
        if tier_rank >= TIER_RANK_SYMBIOTE and random.random() < BIOMORPHIC_WEBBING_PHOTO_CHANCE:
            session.add(PendingPhoto(user_id=user.discord_id, quality=stats["photo_quality"]))

    item_found = None
    drop_chance = min(0.9, stats["base_drop_chance"] + state.scavenge_bonus)
    if user.suit_integrity < SCAVENGE_URGENCY_THRESHOLD:
        urgency = (SCAVENGE_URGENCY_THRESHOLD - user.suit_integrity) / SCAVENGE_URGENCY_THRESHOLD
        drop_chance = min(0.9, drop_chance + urgency * SCAVENGE_URGENCY_BONUS_MAX)
    if random.random() < drop_chance:
        await add_item(session, user.discord_id, stats["component_key"], 1)
        item_found = stats["component_key"]
    elif tier_rank >= TIER_RANK_SYMBIOTE and random.random() < BIOMORPHIC_WEBBING_COMPONENT_CHANCE:
        # "the webbing catches what you'd have missed" — only fires when the base
        # roll above missed, so this never stacks into a guaranteed double-drop.
        await add_item(session, user.discord_id, stats["component_key"], 1)
        item_found = stats["component_key"]

    if tier_rank >= TIER_RANK_SYMBIOTE and random.random() < BIOMORPHIC_WEBBING_CASH_CHANCE:
        cash += rand_range(BIOMORPHIC_WEBBING_CASH_RANGE)

    if cash:
        await add_wallet(session, user, cash, reason=f"patrol_battle:{state.outcome_key}")
    # The actual applied amount, not the pre-penalty/pre-cap roll — a crime penalty
    # or a boss-gate ceiling can both silently shrink this below `xp`.
    actual_xp_gained = await add_reputation(session, user, xp)

    boss_cash_reward = 0
    boss_new_level = None
    if state.outcome_key == "boss" and won_clean:
        # Beating a boss doesn't earn its way through patrol XP — it's a flat
        # promotion to the very next level's floor (no partial progress carried
        # over) plus a guaranteed cash reward, on top of whatever the fight itself
        # already paid out above.
        cleared_gate = next_boss_gate_level(user)
        user.boss_clears += 1
        user.reputation_xp = xp_for_level(cleared_gate + 1)
        boss_new_level = cleared_gate + 1
        boss_cash_reward = round(rand_range(BOSS_CASH_RANGE) * state.difficulty)
        await add_wallet(session, user, boss_cash_reward, reason="patrol_battle:boss_clear")

    donation_flavor = None
    donation_cash = 0
    donation = roll_donation()
    if donation is not None:
        donation_cash = rand_range(donation["cash"])
        await add_wallet(session, user, donation_cash, reason=f"donation:{donation['key']}")
        donation_flavor = donation["flavor"]

    crime_decay = rand_range(CRIME_LEVEL_DECAY_RANGE_WIN if won_clean else CRIME_LEVEL_DECAY_RANGE_LOSS)
    user.crime_level = max(0, user.crime_level - crime_decay)
    crime_level_delta = -crime_decay

    hazard_flavor = None
    hazard_cash = 0
    hazard = await roll_hazard(session, user.discord_id)
    if hazard is not None:
        rolled_hazard_cash = rand_range(hazard["cash"])
        # add_wallet clamps at 0 and returns what actually happened — a broke
        # player's wallet was never really overdrawn, the display just used to
        # show the raw roll instead of the real (possibly smaller) deduction.
        hazard_cash = await add_wallet(session, user, rolled_hazard_cash, reason=f"hazard:{hazard['key']}")
        hazard_flavor = hazard["flavor"]

    await session.commit()

    return BattleReport(
        won_clean=won_clean,
        xp_gained=actual_xp_gained,
        cash_gained=cash,
        suit_damage=state.total_suit_damage,
        photo_banked=photo_banked,
        photo_quality=stats["photo_quality"] if photo_banked else None,
        camera_broke=camera_broke,
        item_found=item_found,
        gadgets_broken=state.gadgets_broken,
        unprotected_penalty=unprotected_penalty,
        donation_flavor=donation_flavor,
        donation_cash=donation_cash,
        hazard_flavor=hazard_flavor,
        hazard_cash=hazard_cash,
        crime_level=user.crime_level,
        crime_level_delta=crime_level_delta,
        boss_cash_reward=boss_cash_reward,
        boss_new_level=boss_new_level,
    )
