import random

import discord
from discord.ext import commands

from db.base import async_session
from db.models import User
from services.leaderboard_service import CATEGORIES, get_leaderboard, get_rank
from utils.icons import thumbnail
from utils.tier_accent import current_accent
from utils.v2_embeds import make_container

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

CATEGORY_ICON_KEYS = {"wealth": "money", "reputation": "reputation", "streak": "streak"}

LEADERBOARD_PAGE_SIZE = 5  # 4 pages of 5 = the top 20 fetched by get_leaderboard

LEADERBOARD_FOOTERS = [
    "J. Jonah Jameson refuses to run this as a story. Still true though.",
    "Somewhere, someone is furiously refreshing this.",
    "Bragging rights: the only reward that doesn't decay.",
    "The city doesn't care about your rank. Your friends do.",
]


class CategorySelect(discord.ui.Select):
    def __init__(self, panel: "LeaderboardView"):
        options = [
            discord.SelectOption(
                label=meta["label"], value=key, emoji=meta["emoji"], default=(key == panel.category)
            )
            for key, meta in CATEGORIES.items()
        ]
        super().__init__(placeholder="Choose a category...", options=options)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.category = self.values[0]
        self.panel.page = 0
        async with async_session() as session:
            await self.panel._render(session)
        await interaction.response.edit_message(view=self.panel, files=self.panel.files, attachments=[])


class PrevPageButton(discord.ui.Button):
    def __init__(self, panel: "LeaderboardView", *, disabled: bool):
        super().__init__(label="Previous", emoji="◀", style=discord.ButtonStyle.secondary, disabled=disabled)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.page -= 1
        async with async_session() as session:
            await self.panel._render(session)
        # Same category, same icon — no need to touch files/attachments on this edit.
        await interaction.response.edit_message(view=self.panel)


class NextPageButton(discord.ui.Button):
    def __init__(self, panel: "LeaderboardView", *, disabled: bool):
        super().__init__(label="Next", emoji="▶", style=discord.ButtonStyle.secondary, disabled=disabled)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.page += 1
        async with async_session() as session:
            await self.panel._render(session)
        await interaction.response.edit_message(view=self.panel)


class LeaderboardView(discord.ui.DesignerView):
    def __init__(self, author_id: int, category: str = "wealth", timeout: float = 180):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.category = category
        self.page = 0
        self.message: discord.Message | None = None
        self.files: list[discord.File] = []
        # Captured once: _render runs again from the page/category callbacks, which are
        # separate tasks where the ambient per-command accent is already gone.
        self.accent = current_accent()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.children:
            self.children[0].disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _render(self, session) -> None:
        self.clear_items()
        meta = CATEGORIES[self.category]
        entries = await get_leaderboard(session, self.category)
        total_pages = max(1, -(-len(entries) // LEADERBOARD_PAGE_SIZE))  # ceil division
        self.page = max(0, min(self.page, total_pages - 1))
        start = self.page * LEADERBOARD_PAGE_SIZE
        page_entries = entries[start : start + LEADERBOARD_PAGE_SIZE]

        container = make_container(self.accent)
        header_text = (
            f"# Leaderboard\n{meta['emoji']} {meta['label']} — Top {len(entries)} "
            f"(Page {self.page + 1}/{total_pages})"
        )
        result = thumbnail(CATEGORY_ICON_KEYS.get(self.category, ""))
        if result is not None:
            thumb, file = result
            container.add_section(discord.ui.TextDisplay(header_text), accessory=thumb)
            self.files = [file]
        else:
            container.add_section(
                discord.ui.TextDisplay(header_text),
                accessory=discord.ui.Button(
                    label=meta["label"], emoji=meta["emoji"], style=discord.ButtonStyle.secondary, disabled=True
                ),
            )
            self.files = []
        container.add_separator()

        if not entries:
            container.add_text("Nobody's on the board yet. Be the first.")
        else:
            lines = [
                f"{MEDALS.get(i, f'{i}.')} <@{entry.discord_id}> — {meta['format'](entry.value)}"
                for i, entry in enumerate(page_entries, start=start + 1)
            ]
            container.add_text("\n".join(lines))

        footer_lines = []
        me = await session.get(User, self.author_id)
        if me is not None and not any(e.discord_id == self.author_id for e in entries):
            rank = await get_rank(session, self.category, me)
            footer_lines.append(f"Your rank: #{rank} — {meta['format'](meta['value_of'](me))}")
        footer_lines.append(random.choice(LEADERBOARD_FOOTERS))

        container.add_separator()
        container.add_text("\n".join(f"-# {line}" for line in footer_lines))

        container.add_separator()
        container.add_row(
            PrevPageButton(self, disabled=self.page == 0),
            NextPageButton(self, disabled=self.page >= total_pages - 1),
        )
        container.add_row(CategorySelect(self))

        self.add_item(container)


class LeaderboardCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="leaderboard", description="See who's on top — wealth, reputation, or daily streaks.")
    async def leaderboard(self, ctx: discord.ApplicationContext):
        view = LeaderboardView(author_id=ctx.author.id)
        async with async_session() as session:
            await view._render(session)
        await ctx.respond(view=view, files=view.files)
        view.message = await ctx.interaction.original_response()


def setup(bot: discord.Bot):
    bot.add_cog(LeaderboardCog(bot))
