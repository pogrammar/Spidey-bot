import discord
from discord.ext import commands

from db.base import async_session
from services.economy import get_or_create_user
from services.patreon_service import PATREON_PAGE_URL
from utils.icons import emoji as icon_emoji, item_label
from utils.links import BOT_INVITE_URL
from utils.tier_accent import current_accent
from utils.v2_embeds import add_field_groups, make_container

# One entry per *stage of play*, not one per command group.
#
# This has been tuned in both directions and both extremes are wrong. It started as
# 3 unrelated subjects crammed onto each page (Daily + Earning + Money at once), which
# made page 1 enormous. Splitting every subject into its own dropdown entry fixed the
# size but grew to 15 options, and a 15-item dropdown is its own kind of unreadable —
# you're scanning a list to find out where a thing might be instead of reading about it.
#
# The grouping below follows the loop in the "loop" topic, so the categories match the
# order you actually do things in: fuel and fight, then get paid, then keep the roof on.
# Two-to-four commands per topic, with `-#` eyebrow labels inside the longer bodies so
# they stay scannable — that's the ceiling. Anything that pushes a body past roughly one
# screen wants to be its own topic again, and anything that would be a topic of one line
# belongs folded into a neighbour.
#
# Patreon and /invite are deliberately NOT here: they're outbound links rather than
# gameplay reference, so they're link buttons on the card (see _render) with their
# command-side details in the footer.
OVERVIEW_TITLE = "Friendly Neighborhood Cheat Sheet"
OVERVIEW_BODY = (
    "You're Peter Parker. Rent's due, the camera's held together with tape, and "
    "being Spider-Man doesn't pay a cent by itself — you make it pay.\n\n"
    "Pick a topic from the dropdown below."
)

# Small print under the dropdown, on every page. Carries what the two link buttons
# can't say: that there's an in-bot version of the Patreon pitch (the tier breakdown,
# without leaving Discord), and that a pledge does nothing at all until it's connected —
# which is the one genuinely load-bearing fact in this whole block. `/invite` is here
# because the button is for *you* clicking it; the command is for posting the link so
# somebody else can.
FOOTER_LINES = [
    "`/patreon subscribe` — both tiers in full. `/patreon link` connects a pledge you already have.",
    "`/patreon perks` — what's live for you · `/invite` — post the install link into a channel.",
]

TOPICS = [
    {
        "key": "loop",
        "emoji": "🔄",
        "title": "The Loop, Start to Finish",
        "summary": "The whole cycle in one line. Start here.",
        "body": (
            "`/lab brew` → `/patrol` → `/bugle submit` → `/bank deposit` → "
            "`/apartment pay` + `/workbench repair`\n\n"
            "Brew fluid, spend it fighting crime, sell the photos to the Bugle, bank the "
            "cash before another player takes it, then pay rent and patch the suit. "
            "Everything else in this menu feeds one of those steps.\n\n"
            "Brand new? `/start` walks you through the first few minutes."
        ),
    },
    {
        "key": "patrol",
        "emoji": icon_emoji("attack") or "🕸️",
        "title": "Patrol & Gear",
        "summary": "Fight crime — and the fluid, gadgets and suit that let you.",
        "body": (
            f"`/patrol` — swing out and see what's happening. Costs 1 "
            f"{item_label('web_fluid_vial', 'Web-Fluid Vial')} (or cash if you're out). "
            "Crimes turn into a real fight: **Attack**, **Evade** or **Use Gadget** each "
            "round — your call decides it. Evade sets up a guaranteed bonus-damage Attack "
            "next round. Gets tougher as your reputation climbs. 30 sec cooldown.\n\n"
            "-# FUEL\n"
            f"`/lab brew` — start a {item_label('web_fluid_vial', 'Web-Fluid')} batch. "
            "`/lab status` / `/lab collect` — check on it, then collect.\n\n"
            "-# GADGETS\n"
            "`/gadget panel` — click-through menu to equip, unequip and upgrade. Carry "
            "**2 at once**; in a battle each one gets its own \"Use\" button, so which two "
            "you bring is a real choice. Unlocks by reputation level: "
            f"{item_label('web_shooters', 'Web-Shooters')} (1), "
            f"{item_label('web_grabber', 'Web Grabber')} (5), "
            f"{item_label('ricochet_web', 'Ricochet Web')} (10), "
            f"{item_label('upshot', 'Upshot')} (15), "
            f"{item_label('concussion_burst', 'Concussion Burst')} (20). Each can wear out "
            "and break mid-fight.\n\n"
            "-# SUIT\n"
            "`/workbench status` — integrity, repair cost, components on hand. "
            "`/workbench repair` — back to 100% using cash + scavenged components. Patrol "
            "warns you if you're low and can't afford it."
        ),
    },
    {
        "key": "money",
        "emoji": icon_emoji("wallet") or "💰",
        "title": "Money",
        "summary": "Daily cash, paid work, and where to keep it.",
        "body": (
            "-# FREE\n"
            "`/daily claim` — cash, XP and a random bonus pull, once a day. Rewards grow "
            "with your streak, with big payouts at 7/14/30/60/100 days; miss 48 hours and "
            "it resets. `/daily status` checks without claiming.\n\n"
            "-# PAID WORK\n"
            f"`/bugle photos` then `/bugle submit` — sell the "
            f"{item_label('camera', 'photos')} from your patrols to JJJ. 1 min cooldown.\n"
            "`/tutoring` — guaranteed safe cash, but locks you out of patrol for 2 min.\n\n"
            "-# HOLDING ON TO IT\n"
            "`/balance` — wallet, bank, reputation, suit integrity. `/inventory` — what "
            "you're carrying.\n"
            "`/bank deposit` / `/bank withdraw` — wallet cash can be stolen, bank cash "
            "can't. Bank capacity grows on its own as you fill it.\n\n"
            "`/leaderboard` — how your streak stacks up against everyone else."
        ),
    },
    {
        "key": "bills",
        "emoji": "🏚️",
        "title": "Rent & Eviction",
        "summary": "Rent, due dates, and the eviction meter.",
        "body": (
            "`/apartment status` — rent due date and eviction meter.\n\n"
            "`/apartment pay` — pay $400 rent. Miss it too often and your workbench gets "
            "locked, which means no suit repairs until you're square."
        ),
    },
    {
        "key": "trading",
        "emoji": icon_emoji("store") or "🛒",
        "title": "Buying & Trading",
        "summary": "The general store, and dealing with other players.",
        "body": (
            "-# GENERAL STORE\n"
            "`/shop browse` or `/shop buy` — cameras, repair components, gifts, gadgets. "
            "Bought from the bot, always in stock.\n\n"
            "-# TRADE POST\n"
            "`/market listings` — browse what other players are selling.\n"
            "`/market sell` / `/market buy` / `/market cancel` — your side of it."
        ),
    },
    {
        "key": "people",
        "emoji": "❤️",
        "title": "People",
        "summary": "Aunt May, MJ, and shaking down other players.",
        "body": (
            "-# ALLIES\n"
            "`/ally check` — see how neglected Aunt May and MJ are.\n"
            "`/ally visit` — spend time, or bring a gift from `/shop`. Repeating the same "
            "gift (or gifting every visit) backfires.\n"
            "Keeping both happy boosts reputation gains; neglecting either hurts your "
            "Bugle and Tutoring pay.\n\n"
            "-# PVP\n"
            "`/shakedown @user` — try to steal a cut of another player's wallet. Can "
            "backfire and cost you instead."
        ),
    },
]


class TopicSelect(discord.ui.Select):
    def __init__(self, browser: "HelpBrowserView"):
        current = browser._topic()
        options = [
            discord.SelectOption(
                label=t["title"],
                value=t["key"],
                description=t["summary"],
                emoji=t["emoji"],
                default=(t["key"] == browser.selected_key),
            )
            for t in TOPICS
        ]
        # Select placeholders are plain text — they can't render custom Discord emoji
        # markup, so this one deliberately drops the emoji rather than showing the
        # literal "<:name:id>" string. Moot in practice: once an option is `default`,
        # Discord shows that option's own label+emoji in the closed box instead.
        placeholder = current["title"] if current else "Choose a topic..."
        super().__init__(placeholder=placeholder, options=options)
        self.browser = browser

    async def callback(self, interaction: discord.Interaction):
        self.browser.selected_key = self.values[0]
        self.browser._render()
        await interaction.response.edit_message(view=self.browser)


class OverviewButton(discord.ui.Button):
    """A real, clickable accessory beside the topic header — not another row of
    buttons stacked below the text — that jumps back to the topic list."""

    def __init__(self, browser: "HelpBrowserView"):
        super().__init__(label="Overview", emoji="🏠", style=discord.ButtonStyle.secondary)
        self.browser = browser

    async def callback(self, interaction: discord.Interaction):
        self.browser.selected_key = None
        self.browser._render()
        await interaction.response.edit_message(view=self.browser)


class HelpBrowserView(discord.ui.DesignerView):
    """Dropdown jumps straight to any topic instead of paging through them one by
    one — even at six topics, Prev/Next would mean clicking through most of them just
    to reach the one you want. (No count quoted on purpose: the old docstring said 12
    and had already gone stale twice.) Discord caps a Select at 25 options, which is
    the hard ceiling on TOPICS; the practical one is much lower and lives in the
    comment above TOPICS."""

    def __init__(self, author_id: int, timeout: float = 180, accent: int | None = None):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.selected_key: str | None = None
        self.message: discord.Message | None = None
        # Rebuilt by every _render(); on_timeout needs the *current* objects to exempt
        # them, and the timeout can only fire after the last render, so this is always
        # in step with what's on screen.
        self._link_buttons: list[discord.ui.Button] = []
        # `accent` is passed explicitly by OpenGuideButton, which builds one of these from
        # inside a component callback where the ambient per-command context is already
        # gone. /help builds it in the command body, where reading that context works.
        self.accent = accent if accent is not None else current_accent()
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.children:
            # exclusions= matters: Container.disable_all_items() sets disabled on anything
            # that has the attribute, link buttons included, and Discord renders a
            # disabled link button greyed out and unclickable. There's nothing to protect
            # — a link has no callback that could fire against a dead view — so an expired
            # menu that can no longer change pages should still get you to the two links.
            self.children[0].disable_all_items(exclusions=self._link_buttons)
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def _topic(self) -> dict | None:
        return next((t for t in TOPICS if t["key"] == self.selected_key), None)

    def _render(self) -> None:
        self.clear_items()
        topic = self._topic()

        container = make_container(self.accent)
        if topic is None:
            header_text = f"# {OVERVIEW_TITLE}\n{OVERVIEW_BODY}"
            accessory = discord.ui.Button(label="Menu", emoji="📖", style=discord.ButtonStyle.secondary, disabled=True)
        else:
            header_text = f"# {topic['emoji']} {topic['title']}\n{topic['body']}"
            accessory = OverviewButton(self)
        container.add_section(discord.ui.TextDisplay(header_text), accessory=accessory)
        container.add_separator()
        container.add_row(TopicSelect(self))
        container.add_separator()
        container.add_text("\n".join(f"-# {line}" for line in FOOTER_LINES))
        # Link buttons rather than two more dropdown entries: both are outbound links, so
        # a topic page for either would be a paragraph whose only real content is a URL
        # you then have to select and paste. As buttons they're one click from every page
        # instead of two from one, and they stop competing for space with the gameplay
        # reference. on_timeout exempts them — see the comment there.
        self._link_buttons = [
            discord.ui.Button(
                label="Patreon Perks",
                emoji=icon_emoji("arachnid") or "🕷️",
                style=discord.ButtonStyle.link,
                url=PATREON_PAGE_URL,
            ),
            discord.ui.Button(
                label="Add to Your Server",
                emoji=icon_emoji("web_shooters") or "➕",
                style=discord.ButtonStyle.link,
                url=BOT_INVITE_URL,
            ),
        ]
        container.add_row(*self._link_buttons)

        self.add_item(container)


class OpenGuideButton(discord.ui.Button):
    """Swaps the welcome message straight into the /help browser — a new player
    shouldn't have to close this and type another command to get there.

    Carries the accent down from the StartView that owns it: the guide it builds is
    constructed inside this callback, which is the one place in the project a V2 view is
    born outside a command body and so the only one that can't resolve the accent from
    ambient context."""

    def __init__(self, accent: int | None = None):
        super().__init__(label="Open Full Guide", emoji="📖", style=discord.ButtonStyle.primary)
        self.accent = accent

    async def callback(self, interaction: discord.Interaction):
        guide = HelpBrowserView(author_id=interaction.user.id, accent=self.accent)
        await interaction.response.edit_message(view=guide)
        guide.message = await interaction.original_response()


class StartView(discord.ui.DesignerView):
    def __init__(self, author_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message: discord.Message | None = None
        self.accent = current_accent()

        container = make_container(self.accent)
        container.add_section(
            discord.ui.TextDisplay(
                "# Friendly Neighborhood Orientation\n"
                "You're Peter Parker — broke, in a homemade suit, rent due soon. Being "
                "Spider-Man doesn't pay a cent by itself. Here's where to start."
            ),
            accessory=discord.ui.Button(label="Welcome!", emoji="👋", style=discord.ButtonStyle.secondary, disabled=True),
        )
        add_field_groups(
            container,
            [
                (
                    None,
                    [
                        (
                            "1. Claim your first reward",
                            "`/daily claim` — free cash, reputation XP, and a random bonus "
                            "pull. Come back and claim it again tomorrow — the streak is "
                            "worth building.",
                        ),
                        (
                            "2. Get out there",
                            "`/patrol` — swing out and see what's happening. Costs a "
                            f"{item_label('web_fluid_vial', 'Web-Fluid Vial')} (or cash if you're out), and "
                            "crimes turn into a real fight.",
                        ),
                        (
                            "3. Everything else",
                            "Tap **Open Full Guide** below for the full rundown — money, "
                            "gear, allies, trading, and more.",
                        ),
                    ],
                )
            ],
        )
        container.add_separator()
        container.add_row(OpenGuideButton(self.accent))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your welcome message.", ephemeral=True)
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


class HelpCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(
        name="start",
        description="Brand new? Run this first.",
        contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm},
    )
    async def start(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            await get_or_create_user(session, ctx.author.id)

        view = StartView(author_id=ctx.author.id)
        await ctx.respond(view=view)
        view.message = await ctx.interaction.original_response()

    @discord.slash_command(name="help", description="New here? Start with this.")
    async def help(self, ctx: discord.ApplicationContext):
        view = HelpBrowserView(author_id=ctx.author.id)
        await ctx.respond(view=view)
        view.message = await ctx.interaction.original_response()


def setup(bot: discord.Bot):
    bot.add_cog(HelpCog(bot))
