import discord
from discord import Option
from discord.ext import commands

import config
from services.cooldowns import disable_bypass, enable_bypass, is_bypass_enabled
from utils.embeds import base_embed, error_embed


def _is_owner(ctx: discord.ApplicationContext) -> bool:
    """Fails closed: an unset OWNER_DISCORD_ID denies everyone, not everyone-but-checked."""
    return config.OWNER_DISCORD_ID is not None and ctx.author.id == config.OWNER_DISCORD_ID


class AdminCog(commands.Cog):
    """Owner-only dev tools. Deliberately left out of /help — not for players."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    admin = discord.SlashCommandGroup("admin", "Owner-only dev tools.")

    @admin.command(name="bypass", description="Toggle cooldown + brew-time bypass for yourself.")
    async def bypass(self, ctx: discord.ApplicationContext, enabled: Option(bool, "Turn bypass on or off")):
        if not _is_owner(ctx):
            await ctx.respond(embed=error_embed("This isn't for you."), ephemeral=True)
            return

        if enabled:
            enable_bypass(ctx.author.id)
            message = (
                "Cooldown bypass is **ON** — every command's cooldown is skipped, and "
                "/lab collect ignores brew time, until you turn it off."
            )
        else:
            disable_bypass(ctx.author.id)
            message = "Cooldown bypass is **OFF**."

        await ctx.respond(embed=base_embed("Admin", message), ephemeral=True)

    @admin.command(name="bypass-status", description="Check whether your cooldown bypass is on.")
    async def bypass_status(self, ctx: discord.ApplicationContext):
        if not _is_owner(ctx):
            await ctx.respond(embed=error_embed("This isn't for you."), ephemeral=True)
            return

        state = "ON" if is_bypass_enabled(ctx.author.id) else "OFF"
        await ctx.respond(embed=base_embed("Admin", f"Cooldown bypass is **{state}**."), ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(AdminCog(bot))
