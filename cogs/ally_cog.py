import discord
from discord import Option, OptionChoice
from discord.ext import commands

from db.base import async_session
from services.ally_service import ALLY_NAMES, get_current_happiness, list_gift_items, visit_ally
from services.busy import get_busy, set_busy
from services.cooldowns import format_remaining
from services.economy import get_or_create_user
from utils.embeds import SPIDEY_BLUE, base_embed, error_embed

ALLY_CHOICES = [OptionChoice(name=name, value=key) for key, name in ALLY_NAMES.items()]


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

        embed = base_embed("Who You're Neglecting", colour=SPIDEY_BLUE)
        for key, name in ALLY_NAMES.items():
            level = happiness[key]
            note = " — she's noticed you've been busy." if level < 30 else ""
            embed.add_field(name=name, value=f"{level}/100{note}", inline=False)
        await ctx.respond(embed=embed)

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

        embed = base_embed(f"Visiting {name}", flavor)
        sign = "+" if result.happiness_delta >= 0 else ""
        embed.add_field(name="Happiness", value=f"{result.new_happiness}/100 ({sign}{result.happiness_delta})")
        if result.backfired:
            embed.add_field(
                name="Gift Fatigue", value="Too many gifts in a row — give it a visit with nothing next time.", inline=False
            )
        embed.add_field(name="Time Spent", value=format_remaining(result.visit_seconds), inline=False)
        embed.set_footer(text=f"You can't /patrol again for {format_remaining(result.visit_seconds)}.")
        await ctx.respond(embed=embed)


def setup(bot: discord.Bot):
    bot.add_cog(AllyCog(bot))
