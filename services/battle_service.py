from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PendingPhoto, User
from services.economy import add_reputation, add_wallet
from services.gadget_service import list_equipped_gadgets, roll_gadget_effect, roll_gadget_wearout
from services.inventory_service import add_item
from services.loot_tables import rand_range
from services.patrol_service import (
    CRIME_LEVEL_DECAY_RANGE,
    get_equipped_camera,
    roll_donation,
    roll_hazard,
)

# Round count is rolled per-battle (see start_battle) instead of fixed, for pacing
# variety — but it's picked once up front and shown to the player from round 1, so
# there's never hidden information mid-fight, only variety between fights.
ROUND_RANGE = [5, 6, 7]
BASELINE_ROUNDS = 3  # what the base_hp values below, and every other balance number, were tuned against

# How much extra enemy HP (beyond straight difficulty scaling) each round beyond
# BASELINE_ROUNDS adds, per tier: hp_ratio = 1 + slope * (num_rounds - BASELINE_ROUNDS).
# More rounds means less variance (law of large numbers), which cuts both ways: it
# pulls bronze (already averaging *above* its required per-round damage rate) toward
# an even higher win rate, and pulls gold (already averaging *below* its rate) toward
# an even lower one. Naive proportional HP scaling (just num_rounds/BASELINE_ROUNDS)
# doesn't correct for that — empirically verified via binary search across the full
# level range that these two slopes are what it actually takes to hold each tier's
# win rate steady as round count changes, not just scale HP with round count.
ROUND_HP_SLOPE = {"crime_bronze": 0.36, "crime_gold": 0.27}

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
}

ATTACK_HIT_CHANCE = 0.75
ATTACK_DAMAGE = {"crime_bronze": [10, 18], "crime_gold": [12, 22]}
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


def start_battle(
    outcome_key: str,
    difficulty: float,
    starting_suit_integrity: int,
    base_xp: int,
    base_cash: int,
    available_gadgets: list[tuple[str, str]],
) -> BattleState:
    stats = ENEMY_STATS[outcome_key]
    combat_difficulty = _combat_difficulty(difficulty)

    num_rounds = random.choice(ROUND_RANGE)
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
        enemy_name=random.choice(stats["names"]),
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


def _enemy_counter(state: BattleState, incoming_multiplier: float = 1.0) -> int:
    if random.random() >= state.enemy_hit_chance:
        return 0
    dmg = rand_range(state.enemy_damage_range)
    return round(dmg * incoming_multiplier)


def _apply_counter(state: BattleState, dmg: int) -> None:
    if dmg > 0:
        state.total_suit_damage += dmg
        state.hits_taken += 1


def resolve_attack(state: BattleState) -> str:
    dmg_range = state.attack_damage_range
    comboed = state.combo_ready
    state.combo_ready = False

    hit_chance = COMBO_HIT_CHANCE if comboed else ATTACK_HIT_CHANCE
    if random.random() < hit_chance:
        dmg = rand_range(dmg_range)
        if comboed:
            dmg = round(dmg * COMBO_DAMAGE_MULTIPLIER)
        state.enemy_hp = max(0, state.enemy_hp - dmg)
        template = random.choice(COMBO_HIT_LINES if comboed else ATTACK_HIT_LINES)
        hit_line = template.format(dmg=dmg)
    else:
        hit_line = random.choice(ATTACK_MISS_LINES)

    if state.enemy_hp <= 0:
        return hit_line

    counter = _enemy_counter(state)
    _apply_counter(state, counter)
    enemy = state.enemy_name.capitalize()
    if counter:
        counter_line = random.choice(ENEMY_HIT_LINES).format(enemy=enemy, dmg=counter)
    else:
        counter_line = random.choice(ENEMY_WHIFF_LINES).format(enemy=enemy)
    return hit_line + counter_line


def resolve_evade(state: BattleState) -> str:
    state.combo_ready = True
    counter = _enemy_counter(state, EVADE_DAMAGE_MULTIPLIER)
    _apply_counter(state, counter)
    base = random.choice(EVADE_GRAZE_LINES).format(dmg=counter) if counter else random.choice(EVADE_CLEAN_LINES)
    return f"{base} {random.choice(COMBO_SETUP_LINES)}"


async def resolve_gadget(session: AsyncSession, user_id: int, state: BattleState, gadget_key: str) -> str:
    effect = await roll_gadget_effect(session, user_id, gadget_key)
    broken = await roll_gadget_wearout(session, user_id, state.difficulty, gadget_key)
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
            _apply_counter(state, counter)
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
            _apply_counter(state, counter)
            if counter:
                lines.append(f"{state.enemy_name.capitalize()} hits back, -{counter}% suit.")

    if broken:
        lines.append(f"Your {broken} gives out from the strain.")

    return " ".join(lines)


async def finalize_battle(session: AsyncSession, user: User, state: BattleState) -> BattleReport:
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

    item_found = None
    drop_chance = min(0.9, stats["base_drop_chance"] + state.scavenge_bonus)
    if user.suit_integrity < SCAVENGE_URGENCY_THRESHOLD:
        urgency = (SCAVENGE_URGENCY_THRESHOLD - user.suit_integrity) / SCAVENGE_URGENCY_THRESHOLD
        drop_chance = min(0.9, drop_chance + urgency * SCAVENGE_URGENCY_BONUS_MAX)
    if random.random() < drop_chance:
        await add_item(session, user.discord_id, stats["component_key"], 1)
        item_found = stats["component_key"]

    if cash:
        await add_wallet(session, user, cash, reason=f"patrol_battle:{state.outcome_key}")
    await add_reputation(session, user, xp)

    donation_flavor = None
    donation_cash = 0
    donation = roll_donation()
    if donation is not None:
        donation_cash = rand_range(donation["cash"])
        await add_wallet(session, user, donation_cash, reason=f"donation:{donation['key']}")
        donation_flavor = donation["flavor"]

    user.crime_level = max(0, user.crime_level - rand_range(CRIME_LEVEL_DECAY_RANGE))

    hazard_flavor = None
    hazard_cash = 0
    hazard = await roll_hazard(session, user.discord_id)
    if hazard is not None:
        hazard_cash = rand_range(hazard["cash"])
        await add_wallet(session, user, hazard_cash, reason=f"hazard:{hazard['key']}")
        hazard_flavor = hazard["flavor"]

    await session.commit()

    return BattleReport(
        won_clean=won_clean,
        xp_gained=xp,
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
    )
