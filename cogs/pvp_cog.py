import logging
import random

import discord
from discord import Option
from discord.ext import commands

from db.base import async_session
from services.cooldowns import format_remaining, get_remaining_seconds, set_cooldown
from services.economy import get_or_create_user
from services.patreon_service import accent_for_rank, tier_badge
from services.shakedown_service import (
    MIN_TARGET_WALLET,
    SHAKEDOWN_COOLDOWN_SECONDS,
    STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS,
    TARGET_PROTECTION_SECONDS,
    attempt_shakedown,
    count_stealth_protections,
)
from utils.embeds import error_embed
from utils.icons import emoji
from utils.v2_embeds import StaticView

log = logging.getLogger("spidey")

SHAKEDOWN_FOOTERS = [
    "Not exactly hero material, but the rent's due.",
    "Parker Luck strikes again — this time in your favor.",
    "Even Spider-Man needs a side hustle sometimes.",
    "You'll feel bad about this later. Probably.",
]


def _stealth_dm_view(thief_name: str, target_tier_rank: int, protections: int) -> StaticView:
    """The DM Stealth Mode sends its owner after it turns an attempt away.

    This is the perk's only surface for the person paying for it, and it exists because
    every other one belongs to somebody else: the protected attempt renders a panel for the
    *thief*, and by construction it can only fire while the target has been idle
    STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS — so "it worked" and "you weren't watching"
    are the same condition. Before this, a subscriber could hold the tier for a month and
    have no way to learn it had ever done anything.

    **The thief is named on purpose.** It leaks nothing: /shakedown's response is not
    ephemeral, so the channel already saw "Stealth Mode — <thief> backed off <target>" while
    the target was away. This DM tells them about a message that was posted publicly where
    they weren't looking, and who came sniffing is the part with any value to them.

    §9 attribution applies in its normal orientation here, unlike the thief's panel: the
    reader IS the subscriber, so the glyph leads and their own badge trails.

    **The accent must be passed explicitly, and this is the same trap as the panel's.** It
    is built inside the *thief's* command context, so make_container()'s ambient default is
    the thief's tier — an unsubscribed thief would leave a Symbiote subscriber's own perk DM
    with no accent bar at all, and a subscribed one would paint it their colour. The
    parameter is named `target_tier_rank`, not `tier_rank`, for the same reason it is on
    ShakedownResult: on this one command a bare "tier_rank" reads as the invoker's.

    A pure builder, taking no session and no bot, so the copy and the accent are assertable
    from scratch/check_stealth_instrumentation.py without a Discord connection.
    """
    glyph = emoji("stealth_mode")
    lead = f"{glyph} " if glyph else ""
    badge = tier_badge(target_tier_rank)
    trail = f" {badge}" if badge else ""

    # 0 means count_stealth_protections() swallowed a failure (it can't legitimately be 0 —
    # the attempt being reported was logged before this ran), so the clause is dropped
    # rather than rendered as a wrong number. Same rule as patreon_cog's _stealth_mode_line.
    if protections > 1:
        count_line = f"That's {protections} attempts it's turned away for you in total."
    elif protections == 1:
        count_line = "That's the first one it's ever turned away for you."
    else:
        count_line = ""

    return StaticView(
        "Stealth Mode",
        f"{lead}**{thief_name}** came looking for you while you were away. They didn't get "
        f"close — the suit noticed first, and it doesn't need you awake to do that.{trail}",
        footer_lines=[
            # The 20 is interpolated, never typed: hardcoding a threshold into copy is what
            # went stale on the Venom Blast line when its multiplier moved. It's spelled out
            # at all because "why did it protect me that time and not this time" is the
            # obvious next question, and this DM is where it gets asked.
            f"Nothing was taken. It steps in on its own once you've been away "
            f"{STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS // 60}+ minutes.",
            count_line,
        ],
        icon_key="stealth_mode",
        accent=accent_for_rank(target_tier_rank),
    )


class PvpCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="shakedown", description="Desperate for cash? Shake down another player.")
    async def shakedown(
        self,
        ctx: discord.ApplicationContext,
        target: Option(discord.Member, "Who are you shaking down?"),
    ):
        if target.id == ctx.author.id:
            await ctx.respond(embed=error_embed("You can't shake yourself down. That's just called stress."))
            return
        if target.bot:
            await ctx.respond(embed=error_embed("It's a bot. It has no wallet. Leave it alone."))
            return

        async with async_session() as session:
            thief = await get_or_create_user(session, ctx.author.id)
            victim = await get_or_create_user(session, target.id)

            remaining = await get_remaining_seconds(session, thief.discord_id, "shakedown")
            if remaining > 0:
                await ctx.respond(embed=error_embed(f"Lay low a bit. Try again in {format_remaining(remaining)}."))
                return

            protected = await get_remaining_seconds(session, victim.discord_id, "shakedown_target")
            if protected > 0:
                await ctx.respond(
                    embed=error_embed(
                        f"{target.display_name} is still on guard from the last hit. "
                        f"Try someone else, or wait {format_remaining(protected)}."
                    )
                )
                return

            if victim.wallet < MIN_TARGET_WALLET:
                await ctx.respond(
                    embed=error_embed(f"{target.display_name} is broker than you right now. Not worth the risk.")
                )
                return

            result = await attempt_shakedown(session, thief, victim)
            await set_cooldown(session, thief.discord_id, "shakedown", SHAKEDOWN_COOLDOWN_SECONDS)
            await set_cooldown(session, victim.discord_id, "shakedown_target", TARGET_PROTECTION_SECONDS)

        if result.stealth_protected:
            # GAME_DESIGN §9 attribution, with the one inversion the rest of the codebase
            # doesn't have: the glyph leads to say WHICH perk stopped you, the badge trails to
            # say whose subscription it was — and here that's the TARGET's, not the reader's.
            # This is deliberate. Without it the thief just sees an unexplained refusal that
            # still burned their cooldown and moved no cash, which reads as a bug rather than
            # as a perk. Do not "fix" this to ctx.author's rank; the thief's tier has no
            # bearing on whether Stealth Mode fires.
            glyph = emoji("stealth_mode")
            lead = f"{glyph} " if glyph else ""
            badge = tier_badge(result.target_tier_rank)
            trail = f" {badge}" if badge else ""
            # A V2 panel rather than error_embed, for two reasons. error_embed titles all five
            # of this command's refusals "Parker Luck." — so the only Symbiote perk a thief
            # will ever watch fire was dressed as the thief's own bad luck — and a legacy
            # embed can carry neither the tier accent bar nor a readable thumbnail, which are
            # the two things that make a perk look like a perk.
            #
            # The accent is passed EXPLICITLY, for the same reason the badge is read off the
            # result: make_container()'s ambient default is the *invoking* user's tier, which
            # here is the thief's. Left to default, a subscribed thief would get their own
            # colour on a message about somebody else's perk, and an unsubscribed one would
            # get no bar at all on a panel whose whole subject is a subscription. All three
            # marks — glyph, badge, accent — have to name the one person who paid for this.
            #
            # Safe to pass through unguarded only because stealth_mode_active() gates on
            # Symbiote: the rank here is never below Arachnid, so accent_for_rank never
            # returns None. It would matter if it could — make_container treats None as
            # "fall back to the ambient accent", i.e. back to the thief's.
            view = StaticView(
                "Stealth Mode",
                f"{lead}Something's watching {target.display_name} — you back off "
                f"before you even get close.{trail}",
                footer_lines=[
                    "You never got close enough to get caught — no cash moved, and no penalty either.",
                    f"Your cooldown still ran, though. Try again in "
                    f"{format_remaining(SHAKEDOWN_COOLDOWN_SECONDS)}.",
                ],
                icon_key="stealth_mode",
                accent=accent_for_rank(result.target_tier_rank),
            )
            await ctx.respond(view=view, files=view.files)
            # AFTER the response, always. Answering the interaction has a 3-second deadline
            # and this needs a fetch_user round-trip plus a DM send, so doing it first risks
            # failing the thief's command to notify the target.
            if result.notify_target:
                await self._notify_stealth_target(
                    target.id, ctx.author.display_name, result.target_tier_rank
                )
        elif result.success:
            view = StaticView(
                "Shakedown — Success",
                f"You corner {target.display_name} in an alley and lighten their pockets.",
                fields=[("Cash Taken", f"${result.amount:,}")],
                footer_lines=[random.choice(SHAKEDOWN_FOOTERS)],
                icon_key="pvp",
            )
            await ctx.respond(view=view, files=view.files)
        else:
            embed = error_embed(f"{target.display_name} fights back — or someone sees you. You bail, but not clean.")
            embed.add_field(name="Cash Lost", value=f"${result.amount:,}")
            await ctx.respond(embed=embed)

    async def _notify_stealth_target(self, target_id: int, thief_name: str, target_tier_rank: int) -> None:
        """DMs the target that Stealth Mode turned an attempt away. Never raises.

        Whether to send at all was decided in the service — `notify_target` means the
        throttle slot is already claimed (see shakedown_service.claim_stealth_dm_slot), so
        this only does the Discord half.

        Nothing here is allowed to break the shakedown, which has already been answered by
        the time this runs. A closed-DM target is the expected failure and logs at info; a
        bounced send still burns the 15-minute window, which is correct rather than merely
        tolerable — DMs being closed is a persistent state, so releasing the slot would just
        mean retrying a doomed send on every future attempt.
        """
        # Read after the service logged this attempt, so the count includes it. Fail-soft on
        # its own (returns 0 on any error), which _stealth_dm_view renders as no count clause.
        protections = await count_stealth_protections(target_id)
        try:
            view = _stealth_dm_view(thief_name, target_tier_rank, protections)
            user = await self.bot.fetch_user(target_id)
            await user.send(view=view, files=view.files)
        except discord.HTTPException:
            log.info(
                "Stealth Mode DM undelivered (DMs closed or user unreachable): target=%s — the "
                "protection still applied, and /patreon perks carries the count",
                target_id,
            )
        except Exception:
            log.exception("Stealth Mode DM failed unexpectedly; the shakedown itself was unaffected")


def setup(bot: discord.Bot):
    bot.add_cog(PvpCog(bot))
