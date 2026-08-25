from __future__ import annotations

import datetime
import json
import random
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Item, User
from services.cooldowns import format_remaining, get_remaining_seconds, set_cooldown
from services.economy import add_reputation, add_wallet
from services.gadget_service import MAX_UPGRADE_LEVEL, list_usable_gadgets
from services.inventory_service import add_item
from services.loot_tables import rand_range, weighted_choice
from utils.icons import item_label

DAILY_COMMAND_KEY = "daily"
DAILY_COOLDOWN_SECONDS = 24 * 60 * 60
# Miss this window after a claim and the streak resets — a real "come back tomorrow
# or lose it" stake, not just a bigger number that never has any risk attached.
STREAK_BREAK_HOURS = 48

REWARDS_PATH = Path(__file__).resolve().parent.parent / "data" / "daily_rewards.json"
with open(REWARDS_PATH, encoding="utf-8") as f:
    DAILY_REWARDS = json.load(f)


@dataclass
class DailyStatus:
    streak: int
    longest_streak: int
    can_claim: bool
    seconds_until_claim: float
    at_risk: bool  # already claimed, but streak resets if not claimed again before it breaks
    next_milestone: int | None
    days_to_next_milestone: int | None


@dataclass
class DailyClaimResult:
    streak: int
    longest_streak: int
    streak_continued: bool  # False if this claim started a fresh streak (missed a day)
    cash_gained: int
    xp_gained: int
    bonus_key: str
    bonus_flavor: str
    milestone_label: str | None = None
    milestone_cash: int = 0
    milestone_xp: int = 0


def _milestone_keys() -> list[int]:
    return sorted(int(k) for k in DAILY_REWARDS["milestones"].keys())


async def get_streak_status(session: AsyncSession, user: User) -> DailyStatus:
    remaining = await get_remaining_seconds(session, user.discord_id, DAILY_COMMAND_KEY)

    at_risk = False
    if user.daily_last_claimed is not None and remaining <= 0 and user.daily_streak > 0:
        hours_since = (datetime.datetime.utcnow() - user.daily_last_claimed).total_seconds() / 3600
        at_risk = hours_since < STREAK_BREAK_HOURS

    next_milestone = next((m for m in _milestone_keys() if m > user.daily_streak), None)

    return DailyStatus(
        streak=user.daily_streak,
        longest_streak=user.daily_longest_streak,
        can_claim=remaining <= 0,
        seconds_until_claim=remaining,
        at_risk=at_risk,
        next_milestone=next_milestone,
        days_to_next_milestone=(next_milestone - user.daily_streak) if next_milestone else None,
    )


async def _grant_free_gadget_upgrade(session: AsyncSession, user: User) -> str | None:
    """Bumps a random eligible equipped gadget up one level, no cash cost. Returns
    its name, or None if nothing's equipped/eligible (caller decides the fallback).

    Uses the *usable* list so a streak reward can't be spent upgrading a gadget the
    player's lapsed subscription has switched off — the reward names the gadget it
    upgraded, and naming an inert one reads as the reward having been wasted."""
    equipped = await list_usable_gadgets(session, user.discord_id)
    eligible = [g for g in equipped if g.upgrade_level < MAX_UPGRADE_LEVEL]
    if not eligible:
        return None
    target = random.choice(eligible)
    target.upgrade_level += 1
    item = await session.get(Item, target.item_key)
    return item_label(target.item_key, item.name) if item else target.item_key


async def claim_daily(session: AsyncSession, user: User) -> tuple[bool, str, DailyClaimResult | None]:
    remaining = await get_remaining_seconds(session, user.discord_id, DAILY_COMMAND_KEY)
    if remaining > 0:
        return False, f"Already claimed today. Come back in {format_remaining(remaining)}.", None

    now = datetime.datetime.utcnow()
    if user.daily_last_claimed is None:
        new_streak = 1
        streak_continued = False
    else:
        hours_since = (now - user.daily_last_claimed).total_seconds() / 3600
        if hours_since > STREAK_BREAK_HOURS:
            new_streak = 1
            streak_continued = False
        else:
            new_streak = user.daily_streak + 1
            streak_continued = True

    user.daily_streak = new_streak
    user.daily_longest_streak = max(user.daily_longest_streak, new_streak)
    user.daily_last_claimed = now

    scale_days = min(new_streak - 1, DAILY_REWARDS["streak_scaling_cap_days"])
    cash = rand_range(DAILY_REWARDS["base_cash"]) + round(scale_days * DAILY_REWARDS["cash_per_streak_day"])
    xp = rand_range(DAILY_REWARDS["base_xp"]) + round(scale_days * DAILY_REWARDS["xp_per_streak_day"])

    bonus_entry = weighted_choice(DAILY_REWARDS["bonus_table"])
    bonus_key = bonus_entry["key"]
    bonus_flavor = ""

    if bonus_key == "gift":
        gift_entry = weighted_choice(DAILY_REWARDS["gift_table"])
        gift_item_key = gift_entry["item_key"]
        await add_item(session, user.discord_id, gift_item_key, 1)
        item = await session.get(Item, gift_item_key)
        bonus_flavor = f"A free {item_label(gift_item_key, item.name)} — something for Aunt May or MJ."
    elif bonus_key == "web_fluid":
        qty = rand_range(DAILY_REWARDS["web_fluid_range"])
        await add_item(session, user.discord_id, "web_fluid_vial", qty)
        bonus_flavor = f"{qty}x {item_label('web_fluid_vial', 'Web-Fluid Vial')}, straight from the lab — on the house."
    elif bonus_key == "component":
        component_key = random.choice(DAILY_REWARDS["component_table"])
        qty = rand_range(DAILY_REWARDS["component_qty_range"])
        await add_item(session, user.discord_id, component_key, qty)
        item = await session.get(Item, component_key)
        bonus_flavor = f"{qty}x {item_label(component_key, item.name)} for the workbench."
    elif bonus_key == "cash_jackpot":
        jackpot = rand_range(DAILY_REWARDS["cash_jackpot_range"])
        cash += jackpot
        bonus_flavor = f"Cash bonus — an extra ${jackpot:,}."
    elif bonus_key == "gadget_upgrade":
        upgraded_name = await _grant_free_gadget_upgrade(session, user)
        if upgraded_name:
            bonus_flavor = f"Free gadget upgrade — {upgraded_name} just got stronger, no cost."
        else:
            fallback = rand_range(DAILY_REWARDS["gadget_upgrade_fallback_cash"])
            cash += fallback
            bonus_flavor = f"No gadget to upgrade right now, so here's ${fallback:,} instead."
    elif bonus_key == "collectible":
        await add_item(session, user.discord_id, "unstable_web_fluid", 1)
        bonus_flavor = (
            f"Extremely rare pull: an {item_label('unstable_web_fluid', 'Unstable Web-Fluid')}, "
            "straight into your pocket."
        )
    # "none" -> no bonus this time, just the base reward

    milestone_label = None
    milestone_cash = 0
    milestone_xp = 0
    milestone = DAILY_REWARDS["milestones"].get(str(new_streak))
    if milestone:
        milestone_cash = rand_range(milestone["cash"])
        milestone_xp = rand_range(milestone["xp"])
        cash += milestone_cash
        xp += milestone_xp
        milestone_label = milestone["label"]

        if milestone.get("gift"):
            await add_item(session, user.discord_id, milestone["gift"], 1)
        if milestone.get("components"):
            for key in DAILY_REWARDS["component_table"]:
                await add_item(session, user.discord_id, key, milestone["components"])
        if milestone.get("gadget_upgrade"):
            await _grant_free_gadget_upgrade(session, user)  # best-effort; fine if nothing eligible
        if milestone.get("collectible"):
            await add_item(session, user.discord_id, "unstable_web_fluid", 1)

    await add_wallet(session, user, cash, reason="daily:claim")
    # The actual applied amount, not the pre-penalty/pre-cap roll — a crime penalty
    # or a boss-gate ceiling can both silently shrink this below `xp`.
    actual_xp = await add_reputation(session, user, xp)
    await set_cooldown(session, user.discord_id, DAILY_COMMAND_KEY, DAILY_COOLDOWN_SECONDS)
    await session.commit()

    return (
        True,
        "",
        DailyClaimResult(
            streak=new_streak,
            longest_streak=user.daily_longest_streak,
            streak_continued=streak_continued,
            cash_gained=cash,
            xp_gained=actual_xp,
            bonus_key=bonus_key,
            bonus_flavor=bonus_flavor,
            milestone_label=milestone_label,
            milestone_cash=milestone_cash,
            milestone_xp=milestone_xp,
        ),
    )
