import asyncio
import random
import time

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

# Nothing this command renders may ping. Rows are plain names now rather than <@id>
# mentions, so the only mention left that Discord could parse is a literal "@everyone" or
# "@here" sitting inside somebody's display name — which is allowed in a display name, and
# would otherwise fire from a leaderboard row. Suppressing it message-wide is one line and
# doesn't require mangling names with backslashes to defuse.
NO_PINGS = discord.AllowedMentions.none()

UNKNOWN_NAME = "Unknown Web-Slinger"

# Names are cached process-wide rather than resolved per render, and the reason is a hard
# failure, not an optimisation. bot.get_user only knows people the gateway has told us about,
# and until the Members intent is approved that's very few — so nearly every row falls through
# to an HTTP fetch. Doing those before the first response is what made /leaderboard return
# `10062 Unknown interaction` (a 404): Discord gives 3 seconds to acknowledge a command, five
# serialised fetches plus the DB can exceed it, and then there is no response at all.
#
# Bounded by construction: the only keys that can ever land here are the top 20 of three
# categories. A renamed user reads stale for at most the TTL, which is a fair trade for a board
# that draws instantly and doesn't re-fetch the same five people on every page flip.
NAME_TTL_SECONDS = 15 * 60
_name_cache: dict[int, tuple[float, str]] = {}


def cached_name(bot: discord.Bot, discord_id: int) -> str | None:
    """A name for one row without touching the network, or None if we'd have to.

    Sync on purpose. This runs during the render that produces the *first* response, which must
    not await anything that talks to Discord — see NAME_TTL_SECONDS above.
    """
    hit = _name_cache.get(discord_id)
    if hit is not None and time.monotonic() - hit[0] < NAME_TTL_SECONDS:
        return hit[1]
    user = bot.get_user(discord_id)
    if user is None:
        return None
    _name_cache[discord_id] = (time.monotonic(), user.display_name)
    return user.display_name


async def fetch_name(bot: discord.Bot, discord_id: int) -> None:
    """Resolves one row over HTTP and caches it. Only ever called after a response has gone out.

    The failure is cached too. A miss is a deleted account, or someone the bot shares no server
    with while rate limited — none of which starts resolving because we retried it on the next
    page flip, and a retry costs exactly as much as a success.
    """
    try:
        user = await bot.fetch_user(discord_id)
    except discord.HTTPException:
        _name_cache[discord_id] = (time.monotonic(), UNKNOWN_NAME)
    else:
        _name_cache[discord_id] = (time.monotonic(), user.display_name)


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
        await interaction.response.edit_message(
            view=self.panel, files=self.panel.files, attachments=[], allowed_mentions=NO_PINGS
        )
        await self.panel.resolve_pending_names()


class PrevPageButton(discord.ui.Button):
    def __init__(self, panel: "LeaderboardView", *, disabled: bool):
        super().__init__(label="Previous", emoji="◀", style=discord.ButtonStyle.secondary, disabled=disabled)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.page -= 1
        async with async_session() as session:
            await self.panel._render(session)
        # Same category, same icon — no need to touch files/attachments on this edit.
        await interaction.response.edit_message(view=self.panel, allowed_mentions=NO_PINGS)
        await self.panel.resolve_pending_names()


class NextPageButton(discord.ui.Button):
    def __init__(self, panel: "LeaderboardView", *, disabled: bool):
        super().__init__(label="Next", emoji="▶", style=discord.ButtonStyle.secondary, disabled=disabled)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.page += 1
        async with async_session() as session:
            await self.panel._render(session)
        await interaction.response.edit_message(view=self.panel, allowed_mentions=NO_PINGS)
        await self.panel.resolve_pending_names()


class LeaderboardView(discord.ui.DesignerView):
    def __init__(self, bot: discord.Bot, author_id: int, category: str = "wealth", timeout: float = 180):
        super().__init__(timeout=timeout)
        # Held because rows show real names now, and turning an ID into a name needs the
        # cache/API. _render is also reached from the page and category callbacks, so it
        # can't rely on an interaction being in scope.
        self.bot = bot
        self.author_id = author_id
        self.category = category
        self.page = 0
        self.message: discord.Message | None = None
        self.files: list[discord.File] = []
        # Rows _render couldn't name from cache. Drained by resolve_pending_names() after the
        # panel is already on screen.
        self.pending_names: list[int] = []
        # Captured once: _render runs again from the page/category callbacks, which are
        # separate tasks where the ambient per-command accent is already gone.
        self.accent = current_accent()
        # Also once — the panel is rendered twice for a single view (placeholder names, then
        # the real ones), and a footer joke that changed underneath that redraw would read as
        # a glitch rather than a refresh.
        self.footer = random.choice(LEADERBOARD_FOOTERS)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
            return False
        return True

    async def resolve_pending_names(self) -> None:
        """Fills in the rows the render had to leave as placeholders, then redraws the panel.

        Called *after* the response is out, which is the entire point: these fetches are what
        blew the 3-second interaction deadline when they ran before it, and here there's no
        deadline left to blow. Results land in the shared cache, so the next page flip — and
        every other player's board — draws them for free.

        Redrawn via Message.edit rather than an interaction edit: it works the same from the
        command and from any callback, it isn't bound to a 15-minute token, and it's one of the
        paths that sets the Components V2 flag (Interaction.edit_original_response doesn't).
        """
        pending, self.pending_names = self.pending_names, []
        if not pending or self.message is None:
            return
        await asyncio.gather(*(fetch_name(self.bot, discord_id) for discord_id in pending))
        if self.is_finished():
            # Timed out mid-fetch. on_timeout already disabled the controls and had the last
            # word on this message; re-rendering would hand back live buttons nobody listens to.
            return
        async with async_session() as session:
            await self._render(session)
        try:
            # No files=/attachments=: the category icon is already attached and unchanged, and
            # re-sending it would upload the same PNG a second time.
            await self.message.edit(view=self, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.children:
            self.children[0].disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(view=self, allowed_mentions=NO_PINGS)
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
            # Cache-only, so this render can't block on Discord. Whatever comes back None is
            # handed to resolve_pending_names() once the panel is on screen.
            names = {e.discord_id: cached_name(self.bot, e.discord_id) for e in page_entries}
            self.pending_names = [did for did, name in names.items() if name is None]
            lines = [
                f"{MEDALS.get(i, f'{i}.')} {names[entry.discord_id] or UNKNOWN_NAME} — "
                f"{meta['format'](entry.value)}"
                for i, entry in enumerate(page_entries, start=start + 1)
            ]
            container.add_text("\n".join(lines))

        footer_lines = []
        me = await session.get(User, self.author_id)
        if me is not None and not any(e.discord_id == self.author_id for e in entries):
            rank = await get_rank(session, self.category, me)
            footer_lines.append(f"Your rank: #{rank} — {meta['format'](meta['value_of'](me))}")
        footer_lines.append(self.footer)

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
        view = LeaderboardView(self.bot, author_id=ctx.author.id)
        async with async_session() as session:
            await view._render(session)
        # Responded to directly, not deferred. A defer would buy 15 minutes, but the placeholder
        # message Discord creates for it doesn't carry the Components V2 flag and
        # edit_original_response never sets one — so the panel could never be edited in. The
        # render above is pure DB work and comfortably inside the 3-second window; the slow part
        # (naming rows) happens below, once there's no deadline left to miss.
        await ctx.respond(view=view, files=view.files, allowed_mentions=NO_PINGS)
        view.message = await ctx.interaction.original_response()
        await view.resolve_pending_names()


def setup(bot: discord.Bot):
    bot.add_cog(LeaderboardCog(bot))
