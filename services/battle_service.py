from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PendingPhoto, User
from services.economy import add_reputation, add_wallet, next_boss_gate_level
from services.gadget_service import roll_gadget_effect, roll_gadget_wearout
from services.inventory_service import add_item
from services.loot_tables import rand_range
from services.patreon_service import (
    GATED_ITEM_KEYS,
    TIER_RANK_ARACHNID,
    TIER_RANK_NONE,
    TIER_RANK_SYMBIOTE,
    tier_badge,
)
from services.patrol_service import (
    BIOMORPHIC_WEBBING_CASH_CHANCE,
    BIOMORPHIC_WEBBING_CASH_RANGE,
    CAMERA_ITEM_KEY,
    CRIME_LEVEL_DECAY_RANGE_LOSS,
    CRIME_LEVEL_DECAY_RANGE_WIN,
    bump_photo_quality,
    camera_tier_stats,
    get_effective_camera,
    roll_donation,
    roll_hazard,
)
from services.server_perks import NO_PERKS, ServerPerks
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

# Higher Integrity (community server Level 5 perk) — middle of the locked 25-35% range.
#
# Applied to incoming suit damage, NOT by raising the 100-point integrity cap: that cap is
# hardcoded in five places including repair_suit(), and lifting it was rejected outright
# (GAME_DESIGN.md 9.5). Taking less damage reaches the same felt outcome — patrols before a
# repair — without touching a number the rest of the economy reads as fixed.
#
# Crime-tier patrols only. A boss fight's damage budget is what makes the gate a gate, so
# suit_damage_multiplier stays 1.0 there; see suit_damage_multiplier_for() below.
HIGHER_INTEGRITY_DAMAGE_REDUCTION = 0.30


def suit_damage_multiplier_for(perks: ServerPerks, is_boss: bool) -> float:
    if is_boss or not perks.higher_integrity:
        return 1.0
    return 1 - HIGHER_INTEGRITY_DAMAGE_REDUCTION

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
#
# These are three of FOUR rolls, not the whole perk. The fourth is the ambient scavenge
# in services/biomorphic_service.py (added 2026-08-24), which fires during /tutoring,
# /ally visit and /bugle submit — it lives in its own leaf module because patrol_service
# imports ally_service, so ally_service can never import back through here.
BIOMORPHIC_WEBBING_COMPONENT_CHANCE = 0.20  # only rolled if the normal drop_chance roll missed
BIOMORPHIC_WEBBING_PHOTO_CHANCE = 0.20  # only rolled if a camera's equipped and a photo was already banked

# Sonic Dampener was removed on 2026-08-24. It was a +30% incoming-damage penalty scoped
# to a single boss out of twenty ("the Shocker"), which made it invisible to almost every
# player almost all the time — and the tier's own cost is now the combat override below,
# which fires on every kind of fight. Don't reintroduce a per-boss drawback: all 20 named
# bosses share one identical stat block, so there's no attack-type system to hang a
# thematically broader version on, which is exactly what made this one so narrow.

# Venom Blast (Symbiote+ perk) — the counter-damage the absorbed hit is paid back as,
# as a multiple of one ordinary Attack roll. 2x is what the player-facing copy promises
# ("twice as hard" / "pays it back double"), so this is a number the copy is bound to,
# not a free tuning knob — moving it means rewriting VENOM_BLAST_LINES and the
# /patreon perks line too. Named rather than left inline (it was a bare `* 2` until
# 2026-08-22) so the sim in scratch/combat_sim.py can sweep it, and so the one number
# GAME_DESIGN quotes is greppable.
#
# Measured, not asserted: because it multiplies attack_damage_range, which scales with
# difficulty on the same curve as enemy HP, the blast is worth a near-constant ~32% of
# the boss's health bar from bracket 1 through the level-100 cap. That self-scaling is
# the entire reason it's a multiple of an attack roll instead of a flat figure.
VENOM_BLAST_DAMAGE_MULTIPLIER = 2

# The suit integrity at or below which the Venom Blast button ARMS. Boss fights only.
#
# This used to be an interception threshold: the blast fired automatically when an incoming
# hit would have left integrity here or lower. As of 2026-08-24 it is a player-deployed
# button instead (cogs/patrol_cog.py VenomBlastButton, resolve_venom_blast below) — it sits
# greyed out beside Evade reading "Not Charged" until integrity crosses this line, and then
# the player chooses the round to spend it on. The owner asked for the deploy to be a
# decision rather than a rescue, and this constant is what decides *when they get the
# decision*, not what happens.
#
# The number is inherited from the automatic version's tuning, and it survived a re-measure
# under the button. Swept over {0,15,25,35,50,65,80} at four seeds x 60k gadget-free boss
# fights per cell (scratch/check_venom_trigger.py) with the harness pressing the round the
# button arms. Greedy is a floor rather than an estimate — a human can hold it for a round
# they expect to be worse, which is information the sim doesn't have:
#
#   arming bar        0%     15%     25%     35%     50%     65%     80%
#   b10 win        2.44%   9.79%  13.91%  17.35%  19.41%  19.76%  19.75%
#   b1 arms         0.0%   14.6%   28.9%   44.4%   68.6%   86.4%   97.0%
#   b10 median rd     -      5.0     4.0     4.0     3.0     3.0     3.0
#   b10 on rd 1-2     -     0.0%    0.0%    2.3%   13.4%   23.3%   23.2%
#
# (b10 no-Patreon baseline under the same policy: 3.93%.)
#
# Two things changed shape when interception became arming. The U-shape the automatic version
# showed is GONE — it was an artifact of the charge spending itself on whichever hit happened
# to cross the line, and win rate now rises monotonically to about 50% and then flattens
# (50->65 is +0.35% against a 3-sigma band of 0.41%: noise). And the optimum moved to 65%,
# which the shipped 25% gives up 5.85 points of bracket-10 win rate to.
#
# Those 5.85 points are bought deliberately, and the bottom two rows are what buys them. At
# 25% the button lights up on median round 4 of 10 and NEVER in the first two rounds, so it
# reads as a late-fight decision. At 50% and above, a seventh to a quarter of fights hand it
# over before the fight has a shape, which makes it a rotation piece: press it, then fight.
# The owner asked for a decision rather than a rescue, and an opening move is neither.
#
# One more thing to know before touching this: the button is a much weaker perk than the
# automatic version was, and no arming bar can buy that back. The old code returned damage=0
# on the hit that would have taken integrity to the line — a once-per-fight death save. At the
# same 25% bar it put bracket-10 win rate at 60.86%; the button puts it at 13.91%, against a
# 3.93% unsubscribed floor. That comparison spans the override going 0.10 -> 0.30 as well, but
# the override is the small term here: at bar 0%, where the button never arms, Symbiote wins
# 2.44% against that same 3.93% floor, so 0.30 is costing about 1.5 points and the rest of the
# gap is the mechanic. Closing it would take giving the press a defensive component, which is
# a different mechanic, not a different number here.
VENOM_BLAST_TRIGGER_INTEGRITY = 25

# The Symbiote tier's own always-on cost (GAME_DESIGN.md §9.3). It is now the tier's ONLY
# unconditional drawback — the Sonic Dampener that used to share the job was deleted on
# 2026-08-24 (see its tombstone above), and the ally-decay penalty is inherited from
# Arachnid rather than being Symbiote's own. So this one mechanic has to carry the whole
# "the bond costs you something" side of the tier, which is why it is priced this carefully.
#
# Scoped to Evade and Gadget presses, and NOT to Attack. Attack is already aggression:
# there's nothing for the suit to override, and a player who only ever attacks is already
# fighting the way it wants — so the mechanic that overrides restraint correctly never
# fires for them.
#
# Nor to the Venom Blast button (resolve_venom_blast, added 2026-08-24), for a different
# reason: the blast IS the symbiote. A suit that hijacks its own signature move to throw a
# punch instead is incoherent, and it would mean the one button a player had to survive down
# to 25% integrity to unlock could be eaten 30% of the time. Don't "complete" the override's
# coverage by adding it there.
#
# Gadgets were exempt until 2026-08-24, on the reasoning that they are scarce and carry
# durability, so swallowing one "reads as a lost item rather than a lost impulse". That
# objection was answered rather than overruled: the hijack lives at the very TOP of
# resolve_gadget, above roll_gadget_effect and roll_gadget_wearout, so a hijacked press
# costs you the round but never the gadget. You lose the effect and the tempo, not the gear.
# Don't move that branch below either roll — billing durability for a button the suit
# wouldn't let you press is exactly the thing that kept gadgets exempt for two years.
#
# Extending it there is also what makes this a real cost outside boss fights, which it
# previously wasn't. In a crime patrol suit integrity is cosmetic (it only bills you for
# repairs afterward), so losing an Evade's damage reduction costs almost nothing — but
# losing a gadget's damage costs the same there as anywhere. At an unchanged 0.10 the crime
# cost went from -0.51% to -2.36% purely from adding the gadget surface.
#
# The rate is 0.30, set by the owner on 2026-08-24 ("make it so that the suit overtakes 30%
# of the time"). It was chosen against an estimate, and then re-measured — the measurement
# came out roughly twice the estimate, so what's recorded below is the measurement.
#
# Full curve, bracket-10 boss, `scratch/combat_sim.py override`, 40k fights per cell against
# the identical fight with the override off. Two policies because the gadget hijack landed
# the same day and the two shapes price completely differently:
#
#   rate      evade only    +3 gadget presses    crime gold (gadget)
#   0.05         -0.97%             -5.48%              -1.40%
#   0.10         -2.16%            -10.59%              -2.36%
#   0.15         -3.28%            -15.91%              -3.73%
#   0.20         -4.64%            -20.16%              -4.87%
#   0.30         -6.82%            -29.02%              -6.88%   <- shipped
#
# It is very nearly linear in the rate, at about -1% of boss win rate per point of override
# under the gadget policy. Any future "let's try 0.25" can be read straight off that slope
# instead of re-running the sweep.
#
# The -6.30%/-8.76% figures this comment used to quote for 0.15/0.20 are gone: they predate
# both the gadget hijack and the manual Venom Blast press, and they don't correspond to
# either column above. Don't resurrect them from git history.
#
# WHAT 0.30 COSTS, NET, AND THE BAR IT BREAKS. This file previously rejected 0.15 on the
# rule that a paying subscriber must not win less often than an unsubscribed player with a
# full gadget kit. `combat_sim.py package` says 0.30 breaks that rule in boss fights:
#
#   boss bracket    1        5        10       20
#   no Patreon    99.99%   89.65%   40.56%   34.56%
#   Symbiote      98.94%   77.80%   34.15%   31.21%
#   delta         -1.05%  -11.86%   -6.41%   -3.35%     (bracket 1 is a ceiling artifact)
#
#   crime bronze / silver / gold:  +3.83% / +20.26% / +42.81%
#
# So the shipped tier is strongly net-positive in crime patrols and net-negative in boss
# fights at brackets 5, 10 and 20. That is a deliberate, informed choice by the owner and
# not a bug to quietly fix — but it IS the thing to check first if boss-fight complaints
# ever arrive, and the number to move is this one. Every point of override is worth about
# 1% of boss win rate; the tier goes net-positive at every bracket somewhere around 0.10,
# which is what it shipped at for two years.
#
# WHERE THAT COST ACTUALLY LANDS: on gadget users, almost entirely. The table above prices
# boss fights under a policy that presses three gadgets per fight, which is the shape the
# hijack punishes hardest — a hijacked press forfeits an effect that would have negated the
# counter outright, where a hijacked Evade forfeits only a damage reduction. Re-measured
# gadget-free (scratch/check_venom_trigger.py, 240k fights per cell), the same tier at the
# same rate is net POSITIVE almost everywhere:
#
#   boss bracket        1        5       10       20
#   no Patreon      97.34%   26.32%    3.93%    2.37%
#   Symbiote        97.05%   52.86%   13.91%   11.44%
#   delta           -0.30%  +26.54%   +9.99%   +9.07%
#
# and the override's own cost at bracket 10 there is about 1.5 points, against 29 under the
# gadget policy. Same rate, same resolvers — the difference is entirely what the player is
# pressing. So "0.30 is net-negative in boss fights" is true of a gadget-heavy player and
# false of everyone else. The bracket-1 -0.30% is the one real shortfall in this table rather
# than a ceiling artifact: those fights are short enough that the button arms in only 28.9%
# of them while the override is on for all of them.
#
# The practical consequence for anyone tuning this later: if boss complaints arrive, narrowing
# the hijack back toward Evade-only is a smaller and better-targeted lever than dropping the
# rate for everybody, because the rate is not where the asymmetry lives.
#
# Two caveats on the table, in opposite directions. combat_sim models gadget presses
# synthetically with no fumble rate (see its docstring), which OVERSTATES the hijack's cost
# — a real gadget sometimes fumbles into a plain attack anyway. And its Venom Blast policy
# presses the button the round it arms, which UNDERSTATES the manual perk's value, since a
# real player can save the charge for a round they expect to be lethal.
#
# Visibility at 0.30: a mean of 1.20 hijacks per bracket-10 boss fight, with at least one in
# 78.8% of them — so only 21% of fights pass without the suit taking a round off the player.
# At the old 0.10 that was 0.43 per fight and 37.2% of fights, i.e. nearly two thirds saw
# nothing at all, which is the invisibility that started this whole change. These figures are
# not approximations: it's the real constant rolled by the real resolvers.
#
# Two shapes were priced and rejected outright, both worth recording so they don't get
# re-proposed. Making the override hit *harder* (a guaranteed 1.5x, borrowing the combo
# constants) turns it into a straight buff at every rate (+3.16% boss, +10.41% crime at
# 0.15) — the user asked for a cost, and that isn't one. Adding a suit tear on top of
# that moves crime-tier win rate by exactly 0.00%, for the same cosmetic-integrity reason
# above. That's the structural reason this is a plain attack: in crime patrols defense is
# worthless, so *any* override toward aggression that also improves the swing is
# free-to-positive there.
SYMBIOTE_OVERRIDE_CHANCE = 0.30

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
    # Higher Integrity, resolved once when the fight starts and carried on the state
    # rather than passed to _apply_counter. Two reasons: every resolve_* helper feeds
    # that one sink and none of them would otherwise need to know about perks, and a
    # fight has to keep the multiplier it began with even if the player's roles change
    # mid-battle.
    suit_damage_multiplier: float = 1.0
    bonus_xp: int = 0
    bonus_cash: int = 0
    scavenge_bonus: float = 0.0
    hits_taken: int = 0
    broken_gadget_keys: set[str] = field(default_factory=set)
    gadgets_broken: list[str] = field(default_factory=list)  # names, for the final report
    combo_ready: bool = False  # set by a successful Evade, consumed by the next Attack
    venom_blast_used: bool = False  # once-per-boss-fight charge — see resolve_venom_blast
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
    # Perk attribution (GAME_DESIGN.md §9). None of these change what the player
    # earned — they exist so the result card can name the perk that fired instead of
    # the player just seeing a quietly better number and having no way to tell the
    # subscription did anything. photo_quality_before_bump is only set when the bump
    # actually changed the tier (a gold photo can't go higher), so the pair is always
    # safe to render together.
    photo_quality_bumped: bool = False
    photo_quality_before_bump: str | None = None
    biomorphic_photo: bool = False
    biomorphic_component: bool = False
    biomorphic_cash: int = 0
    # Which camera actually took the shot (patrol_service.EffectiveCamera), so the result
    # card can show the body they own instead of always drawing the beat-up 35mm. Resolved
    # here rather than in the cog because it needs the session and the live tier check.
    # Defaulted to the stock camera for the no-photo path, where nothing reads them.
    camera_item_key: str = CAMERA_ITEM_KEY
    camera_label: str = "Camera"


def start_battle(
    outcome_key: str,
    difficulty: float,
    starting_suit_integrity: int,
    base_xp: int,
    base_cash: int,
    available_gadgets: list[tuple[str, str]],
    enemy_name: str | None = None,
    suit_damage_multiplier: float = 1.0,
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
        suit_damage_multiplier=suit_damage_multiplier,
    )


def _tier_tag(tier_rank: int) -> str:
    """The badge that rides along on a paid perk (or a paid drawback) as it fires,
    space-prefixed for appending to a line. Empty below Arachnid.

    It's the *player's* tier, not the tier the perk originates from. The attribution
    rule in GAME_DESIGN.md §9 is emoji-only — the tier's name never appears in battle
    copy — so the badge is the entire signal for whose subscription is talking, and
    showing an Arachnid badge to a Symbiote subscriber tells them their own perk
    belongs to somebody else. Static catalogs (the shop) do the opposite and list
    every badge that clears the gate; see tier_requirement_badges."""
    badge = tier_badge(tier_rank)
    return f" {badge}" if badge else ""


def _perk_glyph(icon_key: str) -> str:
    """The perk's own glyph, space-suffixed so it can lead a line. Empty if that emoji
    hasn't been uploaded — same "a miss renders without it, never an error" contract as
    emoji() itself, so call sites interpolate it unguarded.

    Pairs with _tier_tag, which trails. The glyph says *which perk* fired; the badge says
    *whose subscription*. Until 2026-08-24 the badge did both jobs, which meant every
    Symbiote perk announced itself with the same character and none of them said which
    one it was — a Venom Blast and a bonus component carried byte-identical attribution.
    The perk's name still isn't spelled out in battle copy; the prose already names it."""
    glyph = emoji(icon_key)
    return f"{glyph} " if glyph else ""


def _gated_gadget_tag(gadget_key: str, tier_rank: int) -> str:
    """Owning Spider Bots or Electric Webbing at all is the perk, so the badge rides
    along on every use: that button existing is what the subscription paid for. And
    the gate is live — list_usable_gadgets re-checks the tier at use time, so if this
    tag renders, the pledge is current."""
    if gadget_key not in GATED_ITEM_KEYS:
        return ""
    return _tier_tag(tier_rank)


def _enemy_counter(state: BattleState, incoming_multiplier: float = 1.0) -> int:
    if random.random() >= state.enemy_hit_chance:
        return 0
    dmg = rand_range(state.enemy_damage_range)
    return round(dmg * incoming_multiplier)


def _apply_counter(state: BattleState, dmg: int) -> None:
    # The single suit-damage sink for the whole battle, which is why Higher Integrity is
    # applied here and nowhere else. Rounded per hit rather than on the total so the
    # number the round narrates is the number that lands.
    dmg = round(dmg * state.suit_damage_multiplier)
    if dmg > 0:
        state.total_suit_damage += dmg
        state.hits_taken += 1


# The blast's copy. Player-deployed as of 2026-08-24, which meant rewriting all three rather
# than editing them: the old lines ("The symbiote surges up and swallows the hit whole") had
# the bond reacting to an incoming blow, and that is no longer a sentence the player can
# cause — the button is pressed on a round of their choosing, before the enemy rolls
# anything. They also each carried a leading space, because they used to be appended to a hit
# line at two of four call sites; there is one call site now and it owns the whole line.
#
# Every line has to state both halves of what the press bought, because nothing else in the
# round reports them: {dmg} damage dealt, and no counter coming back. A line that mentions
# only the damage reads as a hard attack, and the player never learns the blast was also
# their defense that round.
VENOM_BLAST_LINES = [
    "You stop holding it back. The symbiote unloads for {dmg} damage, and nothing gets through to answer it.",
    "Venom Blast! {dmg} damage tears out of the bond, and whatever they were winding up dies with it.",
    "You let go of the leash — {dmg} damage, and they're in no condition to hit back.",
]

# The override's copy (SYMBIOTE_OVERRIDE_CHANCE). Every line has to make the same thing
# unmistakable: you pressed a button and you are about to read an attack instead. A player
# who can't tell why the button did something else reads it as a bug, and this is a cost
# they're paying for a subscription — it has to be legible as a cost. The emoji tag is
# appended at the call site, per the attribution rule in GAME_DESIGN.md §9.
#
# Two lists because the two hijacked actions read differently: an overridden Dodge is the
# suit refusing to retreat, an overridden gadget is the suit refusing to let Peter solve
# the problem with engineering. Using the Dodge lines for a gadget press ("you move to
# break away") would describe something the player didn't do.
SYMBIOTE_OVERRIDE_LINES = [
    "You move to break away — the suit doesn't. It drives you straight back in.",
    "You go to dodge. The bond has other plans, and the bond is faster.",
    "Your weight shifts to disengage and the symbiote overrules it, hard.",
    "You want distance. It wants contact. It wins.",
    "The dodge never happens — something under the suit decides you're attacking.",
    "You pull back and the suit pulls harder the other way.",
    "Your feet are already moving before you agreed to any of this.",
    "You call for a dodge. The symbiote answers with a lunge.",
]
SYMBIOTE_GADGET_OVERRIDE_LINES = [
    "Your hand goes for the gadget. The suit doesn't see the point.",
    "You reach for your kit and the bond closes your fist for you.",
    "The symbiote has an opinion about tools, and it isn't a good one.",
    "You line up the shot — the suit decides fists are faster.",
    "Something under the suit vetoes the clever plan.",
    "You go for the tech. The bond goes for them.",
    "It doesn't want your gadget. It wants your hands.",
    "The trigger never gets pulled — the suit's already moving.",
]


def venom_blast_ready(state: BattleState, tier_rank: int = TIER_RANK_NONE) -> bool:
    """Whether the Venom Blast button should be live this round.

    Four conditions: Symbiote, a boss fight, the charge unspent, and suit integrity at or
    below VENOM_BLAST_TRIGGER_INTEGRITY. The cog renders the button greyed out with a
    "Not Charged" label whenever this is False rather than hiding it, so the player can
    see the thing they're waiting for — a perk that only appears on the rounds it works is
    a perk nobody knows they have.

    Defaults to TIER_RANK_NONE so a caller that forgets to thread the rank gets a dead
    button rather than a free perk, matching every other tier gate in this module.
    """
    return (
        tier_rank >= TIER_RANK_SYMBIOTE
        and state.outcome_key == "boss"
        and not state.venom_blast_used
        and state.suit_remaining <= VENOM_BLAST_TRIGGER_INTEGRITY
    )


def resolve_venom_blast(state: BattleState, tier_rank: int = TIER_RANK_NONE) -> str:
    """Spend the charge. Returns the round's log line, or "" if the blast wasn't armed.

    Until 2026-08-24 the blast fired itself, from inside the counter-damage path: a helper
    called _apply_counter_with_venom_blast intercepted the incoming hit and returned a line
    that *replaced* the counter's, which is why a CounterOutcome dataclass existed at all.
    All of that is gone. The owner asked for a button the player deploys, which changes the
    perk from something that happens to you into a decision, and changes the timing from
    "whichever hit happened to cross the line" to "the round you judged worst".

    What a press does, and why each half:

    - Damage is 2x a normal attack roll and is **not** checked against ATTACK_HIT_CHANCE.
      A once-per-fight charge the player had to survive down to 25% integrity to unlock
      cannot then whiff — that would be the worst button press in the game. Multiplying a
      live attack roll instead of using a flat number keeps it scaling with difficulty for
      free; see VENOM_BLAST_DAMAGE_MULTIPLIER, which is copy-bound to "twice as hard".
    - No counter is rolled at all, the same shape as the group_defense and shock_burst
      gadget effects. This half is inherited from the automatic version, where negating one
      incoming hit *was* the perk, and it's what makes pressing this at low integrity safe
      rather than a gamble — at 25% or less, a round that could take a hit might be the
      last one you get.
    - combo_ready is left untouched. A combo banked by an earlier Evade survives the blast
      and is still waiting for the next Attack; the blast neither spends nor grants one.

    The Symbiote override deliberately does **not** get a shot at this press, unlike Evade
    and gadgets. The blast is the symbiote — a suit that hijacks its own signature move to
    throw a punch instead is incoherent, and the override exists to punish hesitation, which
    is the opposite of what this button is.

    The empty-string return is for a stale press: a disabled button can still be clicked if
    an earlier round's edit is in flight, and the cog turns "" into an ephemeral refusal
    rather than burning a round on nothing.
    """
    if not venom_blast_ready(state, tier_rank):
        return ""

    state.venom_blast_used = True
    dmg = rand_range(state.attack_damage_range) * VENOM_BLAST_DAMAGE_MULTIPLIER
    state.enemy_hp = max(0, state.enemy_hp - dmg)
    return (
        f"{_perk_glyph('venom_blast')}"
        f"{random.choice(VENOM_BLAST_LINES).format(dmg=dmg)}"
        f"{_tier_tag(tier_rank)}"
    )


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
            hit_line += " (Enhanced Strength)" + _tier_tag(tier_rank)
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


def resolve_evade(state: BattleState, tier_rank: int = TIER_RANK_NONE) -> str:
    # Symbiote's always-on cost, resolved before anything else so the forfeited Evade is
    # total: no damage reduction on the incoming counter, and — because combo_ready is
    # set below rather than here — no combo banked for next round either. Delegating to
    # resolve_attack rather than duplicating it keeps Enhanced Strength applying normally
    # to the round the suit stole. (Venom Blast used to ride along here too; it's a button
    # now, and a hijacked Evade can't press it for you.)
    #
    # It does still *consume* a combo banked by a previous Evade, which is the one way
    # this cuts in the player's favour: the suit cashes in an opening you were about to
    # waste by dodging again. Deliberate, and already priced into the measured cost.
    if tier_rank >= TIER_RANK_SYMBIOTE and random.random() < SYMBIOTE_OVERRIDE_CHANCE:
        override_line = random.choice(SYMBIOTE_OVERRIDE_LINES) + _tier_tag(tier_rank)
        return f"{override_line} {resolve_attack(state, tier_rank)}"

    state.combo_ready = True
    counter = _enemy_counter(state, EVADE_DAMAGE_MULTIPLIER)
    _apply_counter(state, counter)
    if counter:
        base = random.choice(EVADE_GRAZE_LINES).format(dmg=counter)
    else:
        base = random.choice(EVADE_CLEAN_LINES)
    return f"{base} {random.choice(COMBO_SETUP_LINES)}"


async def resolve_gadget(
    session: AsyncSession, user_id: int, state: BattleState, gadget_key: str, tier_rank: int = TIER_RANK_NONE
) -> str:
    # The override gets first refusal on a gadget press, and it gets it *here* — above
    # roll_gadget_effect and roll_gadget_wearout — so a hijacked press costs you the
    # round but never the gadget. That placement is the whole design: the objection that
    # kept gadgets exempt until 2026-08-24 was that swallowing one "reads as a lost item
    # rather than a lost impulse", and billing durability for a button the suit wouldn't
    # let you press is exactly that. You lose the effect and the tempo, not the gear.
    if tier_rank >= TIER_RANK_SYMBIOTE and random.random() < SYMBIOTE_OVERRIDE_CHANCE:
        override_line = random.choice(SYMBIOTE_GADGET_OVERRIDE_LINES) + _tier_tag(tier_rank)
        return f"{override_line} {resolve_attack(state, tier_rank)}"

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
    lines = [f"{effect.gadget_name}!{_gated_gadget_tag(gadget_key, tier_rank)}"]

    if effect.kind == "negate_damage":
        lines.append("You block the hit entirely — no damage taken.")
    elif effect.kind == "group_defense":
        dmg = round(rand_range(dmg_range) * 1.1)
        state.enemy_hp = max(0, state.enemy_hp - dmg)
        lines.append(f"{dmg} damage dealt, and the counter's fully blocked.")
    elif effect.kind == "shock_burst":
        # Electric Webbing — bonus damage AND the shock keeps the enemy from
        # countering this round at all, same "no counter" shape as group_defense.
        dmg = rand_range(dmg_range) + rand_range(effect.bonus_range or [8, 15])
        state.enemy_hp = max(0, state.enemy_hp - dmg)
        lines.append(f"{dmg} damage dealt — the shock keeps them from countering.")
    else:
        if effect.kind == "bonus_xp":
            dmg = round(rand_range(dmg_range) * 1.4)
        elif effect.kind == "bonus_damage":
            dmg = rand_range(dmg_range) + rand_range(effect.bonus_range or [5, 12])
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
        elif effect.kind == "bonus_damage":
            # This flavor is spider-bot-specific and spider_bots is the only
            # bonus_damage gadget today — keyed rather than unconditional so a
            # second one added later falls back to the plain damage line above
            # instead of silently claiming spider bots did it.
            if gadget_key == "spider_bots":
                lines.append("A spider-bot piled on for the extra damage.")

        if state.enemy_hp > 0:
            counter = _enemy_counter(state)
            _apply_counter(state, counter)
            if counter:
                lines.append(
                    f"{state.enemy_name.capitalize()} hits back, -{counter}% suit."
                )

    if broken:
        lines.append(f"Your {broken} gives out from the strain.")

    return " ".join(lines)


async def finalize_battle(
    session: AsyncSession, user: User, state: BattleState,
    tier_rank: int = TIER_RANK_NONE, perks: ServerPerks = NO_PERKS,
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
    banked_photo_quality = None
    photo_quality_bumped = False
    photo_quality_before_bump = None
    biomorphic_photo = False
    # Effective, not equipped: a Silver/Gold body whose pledge has lapsed keeps taking
    # photos at base-camera stats rather than none at all. Note that it stays breakable,
    # and at the base body's rate, because camera_tier_stats() of the fallback key has no
    # break_chance_reduction. A demoted camera is still earning photos, so it carries the
    # risk that comes with that — unlike a tier-locked gadget, which does nothing and so
    # can't break (see list_usable_gadgets). Making it invulnerable while locked would
    # turn lapsing into its own perk.
    effective_camera = await get_effective_camera(session, user.discord_id, perks)
    camera = effective_camera.row
    if camera is not None:
        tier_stats = camera_tier_stats(effective_camera.item_key)
        banked_photo_quality = stats["photo_quality"]
        if random.random() < tier_stats["quality_bump_chance"]:
            bumped = bump_photo_quality(banked_photo_quality)
            # bump_photo_quality caps at gold, so on a gold-tier encounter the roll can
            # fire and change nothing. Only flag a real tier change — calling that an
            # upgrade on the card would be promising something the player didn't get.
            if bumped != banked_photo_quality:
                photo_quality_before_bump = banked_photo_quality
                banked_photo_quality = bumped
                photo_quality_bumped = True
        session.add(PendingPhoto(user_id=user.discord_id, quality=banked_photo_quality))
        photo_banked = True
        break_chance = 0.06 + 0.05 * state.hits_taken
        if state.entered_unprotected:
            break_chance += UNPROTECTED_CAMERA_BREAK_BONUS
        break_chance = min(0.9, break_chance * state.difficulty)
        break_chance *= 1 - tier_stats["break_chance_reduction"]
        if random.random() < break_chance:
            camera_broke = True
            await session.delete(camera)
        if tier_rank >= TIER_RANK_SYMBIOTE and random.random() < BIOMORPHIC_WEBBING_PHOTO_CHANCE:
            # Same camera took this one, so it gets its own quality-bump roll rather
            # than the raw encounter quality — the lens doesn't stop working for the
            # second shot. Rolled independently instead of copying the first photo's
            # result, so the two can legitimately bank at different qualities.
            bonus_quality = stats["photo_quality"]
            if random.random() < tier_stats["quality_bump_chance"]:
                bonus_quality = bump_photo_quality(bonus_quality)
            session.add(PendingPhoto(user_id=user.discord_id, quality=bonus_quality))
            biomorphic_photo = True

    item_found = None
    biomorphic_component = False
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
        biomorphic_component = True

    biomorphic_cash = 0
    if tier_rank >= TIER_RANK_SYMBIOTE and random.random() < BIOMORPHIC_WEBBING_CASH_CHANCE:
        biomorphic_cash = rand_range(BIOMORPHIC_WEBBING_CASH_RANGE)
        cash += biomorphic_cash

    if cash:
        await add_wallet(session, user, cash, reason=f"patrol_battle:{state.outcome_key}")
    # The actual applied amount, not the pre-penalty/pre-cap roll — a crime penalty
    # or a boss-gate ceiling can both silently shrink this below `xp`.
    actual_xp_gained = await add_reputation(session, user, xp, perks)

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
    hazard = await roll_hazard(session, user.discord_id, perks)
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
        photo_quality=banked_photo_quality,
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
        photo_quality_bumped=photo_quality_bumped,
        photo_quality_before_bump=photo_quality_before_bump,
        biomorphic_photo=biomorphic_photo,
        biomorphic_component=biomorphic_component,
        biomorphic_cash=biomorphic_cash,
        camera_item_key=effective_camera.item_key,
        camera_label=effective_camera.label,
    )
