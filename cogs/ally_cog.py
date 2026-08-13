import random

import discord
from discord import Option, OptionChoice
from discord.ext import commands

from db.base import async_session
from services.ally_service import ALLY_NAMES, get_current_happiness, list_gift_items, visit_ally
from services.busy import get_busy, set_busy
from services.cooldowns import format_remaining
from services.economy import get_or_create_user
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
            happiness = {
                key: await get_current_happiness(session, user.discord_id, key) for key in ALLY_NAMES
            }

        field_groups = []
        for key, name in ALLY_NAMES.items():
            level = happiness[key]
            note = " — she's noticed you've been busy." if level < 30 else ""
            field_groups.append((name, [("", f"{level}/100{note}")]))
        view = StaticView(
            "Who You're Neglecting", field_groups=field_groups, footer_lines=[random.choice(ALLY_FOOTERS)]
        )
        await ctx.respond(view=view)

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

            ok, message, result = await visit_ally(session, user, who, gift)
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
        fields.append(("Time Spent", format_remaining(result.visit_seconds)))

        view = StaticView(
            f"Visiting {name}",
            flavor,
            fields=fields,
            footer_lines=[
                f"You can't /patrol again for {format_remaining(result.visit_seconds)}.",
                random.choice(ALLY_FOOTERS),
            ],
        )
        await ctx.respond(view=view)


def setup(bot: discord.Bot):
    bot.add_cog(AllyCog(bot))
