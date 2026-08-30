import random

import discord
from discord import Option, OptionChoice
from discord.ext import commands

from db.base import async_session
from services.ally_service import ALLY_NAMES, get_current_happiness, list_gift_items, visit_ally
from services.biomorphic_service import scavenge_subtext
from services.busy import get_busy, set_busy
from services.cooldowns import format_remaining
from services.economy import get_or_create_user
from services.patreon_service import TIER_RANK_ARACHNID, get_tier_rank, tier_badge
from services.server_perks import resolve_perks
from utils.embeds import error_embed
from utils.v2_embeds import StaticView

ALLY_CHOICES = [OptionChoice(name=name, value=key) for key, name in ALLY_NAMES.items()]

ALLY_FOOTERS = [
    "May still worries. MJ still notices when you're distracted.",
    "The people who matter don't care about your patrol stats.",
    "Being Spider-Man is easy. This part isn't.",
    "Someone in your life deserves better texts back.",
]


async def gift_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    async with async_session() as session:
        gifts = await list_gift_items(session)

    typed = (ctx.value or "").lower()
    return [
        discord.OptionChoice(name=f"{gift.name} — ${gift.price:,} (+{gift.happiness_boost} base)", value=gift.key)
        for gift in gifts
        if typed in gift.name.lower()
    ][:25]


class AllyCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    ally = discord.SlashCommandGroup("ally", "Keep up with the people who matter.")

    @ally.command(name="check", description="See how Aunt May and MJ are doing.")
    async def check(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            perks = await resolve_perks(session, ctx)
            happiness = {
                key: await get_current_happiness(session, user.discord_id, key, perks)
                for key in ALLY_NAMES
            }
            tier_rank = await get_tier_rank(session, ctx.author.id)

        field_groups = []
        for key, name in ALLY_NAMES.items():
            level = happiness[key]
            note = " — she's noticed you've been busy." if level < 30 else ""
            field_groups.append((name, [("", f"{level}/100{note}")]))

        footer_lines = [random.choice(ALLY_FOOTERS)]
        if tier_rank >= TIER_RANK_ARACHNID:
            # The viewer's OWN tier badge, not the tier the drawback originates from.
            # Symbiote inherits this cost from Arachnid, and since the copy never names
            # a tier (GAME_DESIGN.md §9) the badge is the only thing saying whose
            # subscription is talking — an Arachnid badge here tells a Symbiote
            # subscriber their happiness drain belongs to somebody else's tier.
            footer_lines.append(
                f"{tier_badge(tier_rank)} They're holding onto Peter Parker harder than they used to — "
                f"and they're right to. Show up, or happiness slips faster than it used to.".strip()
            )
        view = StaticView("Who You're Neglecting", field_groups=field_groups, footer_lines=footer_lines)
        await ctx.respond(view=view, files=view.files)

    @ally.command(name="visit", description="Spend some time with Aunt May or MJ.")
    async def visit(
        self,
        ctx: discord.ApplicationContext,
        who: Option(str, "Who are you visiting?", choices=ALLY_CHOICES),
        gift: Option(str, "Bring a gift? (optional, buy from /shop first)", autocomplete=gift_autocomplete, required=False),
    ):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)

            busy = await get_busy(session, user.discord_id)
            if busy is not None:
                label, remaining_busy = busy
                await ctx.respond(
                    embed=error_embed(f"You're busy {label} right now. Free in {format_remaining(remaining_busy)}.")
                )
                return

            tier_rank = await get_tier_rank(session, user.discord_id)
            perks = await resolve_perks(session, ctx)
            ok, message, result = await visit_ally(session, user, who, gift, tier_rank, perks)
            if not ok:
                await ctx.respond(embed=error_embed(message))
                return

            await set_busy(session, user.discord_id, f"ally:{who}", result.visit_seconds)

        name = ALLY_NAMES[who]

        if result.backfired:
            flavor = f"Another gift? {name} isn't buying it this time — this landed badly."
        elif result.gift_name:
            flavor = f"You bring {name} a {result.gift_name}."
        else:
            flavor = f"You spend some real time with {name}."

        sign = "+" if result.happiness_delta >= 0 else ""
        fields = [("Happiness", f"{result.new_happiness}/100 ({sign}{result.happiness_delta})")]
        if result.backfired:
            fields.append((
                "Gift Fatigue", "Too many gifts in a row — give it a visit with nothing next time.",
            ))
        # Biomorphic Webbing's pickup hangs off Time Spent rather than Happiness: the
        # ally-visit flavor lines are all about the trip ("on the walk over"), and the
        # webbing helping itself to a component has nothing to do with how the visit went.
        time_value = format_remaining(result.visit_seconds)
        if result.scavenged:
            time_value += scavenge_subtext(result.scavenged, tier_rank)
        fields.append(("Time Spent", time_value))

        view = StaticView(
            f"Visiting {name}",
            flavor,
            fields=fields,
            footer_lines=[
                f"You can't /patrol again for {format_remaining(result.visit_seconds)}.",
                random.choice(ALLY_FOOTERS),
            ],
            icon_key=gift if gift else None,
        )
        await ctx.respond(view=view, files=view.files)


def setup(bot: discord.Bot):
    bot.add_cog(AllyCog(bot))
