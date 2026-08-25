import discord
from discord.ext import commands

from db.base import async_session
from services.economy import get_or_create_user
from utils.icons import emoji as icon_emoji, item_label
from utils.tier_accent import current_accent
from utils.v2_embeds import add_field_groups, make_container

# One topic per subject instead of several crammed onto one page — /help used to
# bundle 3 unrelated subjects per "page" (Daily + Earning + Money all at once),
# which made page 1 huge. Splitting each subject out to its own dropdown entry
# fixes the size problem directly, not just the navigation around it.
OVERVIEW_TITLE = "Friendly Neighborhood Cheat Sheet"
OVERVIEW_BODY = (
    "You're Peter Parker. Rent's due, the camera's held together with tape, and "
    "being Spider-Man doesn't pay a cent by itself — you make it pay.\n\n"
    "Pick a topic from the dropdown below."
)

TOPICS = [
    {
        "key": "daily",
        "emoji": icon_emoji("streak") or "🔥",
        "title": "Daily",
        "summary": "Free daily cash, XP, and streak bonuses.",
        "body": (
            "• `/daily claim` — free cash, XP, and a random bonus pull, once a day.\n"
            "• Rewards grow with your streak — big payouts at milestones (7/14/30/60/100 days).\n"
            "• Miss 48 hours and the streak resets.\n"
            "• `/daily status` — check without claiming.\n"
            "• `/leaderboard` — see how your streak stacks up against everyone else."
        ),
    },
    {
        "key": "patrol",
        "emoji": icon_emoji("attack") or "🕸️",
        "title": "Patrol",
        "summary": "Fight crime — costs Web-Fluid, pays in XP and cash.",
        "body": (
            f"• `/patrol` — swing out and see what's happening. Costs 1 "
            f"{item_label('web_fluid_vial', 'Web-Fluid Vial')} (or cash if you're out).\n"
            "• Crimes turn into a real fight: Attack, Evade, or Use Gadget each round — your call decides it.\n"
            "• Evade sets up a guaranteed bonus-damage Attack next round.\n"
            "• Gets tougher as your reputation climbs. 30 sec cooldown."
        ),
    },
    {
        "key": "earning",
        "emoji": icon_emoji("camera") or "📸",
        "title": "Work",
        "summary": "Sell patrol photos, or tutor for safe cash.",
        "body": (
            "• `/bugle photos` — check what photos you're holding.\n"
            "• `/bugle submit` — sell them all to JJJ. 1 min cooldown.\n"
            "• `/tutoring` — guaranteed safe cash, but locks you out of patrol for 2 min."
        ),
    },
    {
        "key": "money",
        "emoji": icon_emoji("wallet") or "💰",
        "title": "Finance",
        "summary": "Wallet, bank, and what you're carrying.",
        "body": (
            "`/balance` — wallet, bank, reputation, suit integrity.\n\n"
            "`/inventory` — what you're carrying.\n\n"
            "`/bank deposit` / `/bank withdraw` — wallet cash can be stolen, bank cash "
            "can't. Bank capacity grows on its own as you fill it."
        ),
    },
    {
        "key": "bills",
        "emoji": "🏚️",
        "title": "Bills",
        "summary": "Rent, due dates, and the eviction meter.",
        "body": (
            "`/apartment status` — rent due date and eviction meter.\n\n"
            "`/apartment pay` — pay $400 rent. Miss it too often and your workbench "
            "gets locked."
        ),
    },
    {
        "key": "suit",
        "emoji": icon_emoji("suit_integrity") or "🔧",
        "title": "Suit",
        "summary": "Integrity, repair cost, and components.",
        "body": (
            "`/workbench status` — integrity, repair cost, components on hand.\n\n"
            "`/workbench repair` — restore to 100% using cash + scavenged components. "
            "Patrol warns you if you're low and can't afford it."
        ),
    },
    {
        "key": "gadgets",
        "emoji": icon_emoji("gadgets_category") or "🦾",
        "title": "Gadgets",
        "summary": "Equip, unequip, and upgrade your loadout.",
        "body": (
            "`/gadget panel` — click-through menu to equip, unequip, and upgrade.\n\n"
            "Carry **2 at once** — in a patrol battle you get a separate \"Use\" button "
            "for each, so which two you bring is a real choice. Unlocks by reputation level: "
            f"{item_label('web_shooters', 'Web-Shooters')} (1), {item_label('web_grabber', 'Web Grabber')} (5), "
            f"{item_label('ricochet_web', 'Ricochet Web')} (10), {item_label('upshot', 'Upshot')} (15), "
            f"{item_label('concussion_burst', 'Concussion Burst')} (20). Each can wear out and break mid-fight."
        ),
    },
    {
        "key": "pvp",
        "emoji": icon_emoji("pvp") or "🥊",
        "title": "PvP",
        "summary": "Shake down other players for cash.",
        "body": "`/shakedown @user` — try to steal a cut of their wallet. Can backfire and cost you instead.",
    },
    {
        "key": "store",
        "emoji": icon_emoji("store") or "🛒",
        "title": "General Store",
        "summary": "Buy cameras, components, gifts, and gadgets.",
        "body": "`/shop browse` or `/shop buy` — camera, repair components, gifts, gadgets.",
    },
    {
        "key": "trade_post",
        "emoji": icon_emoji("market") or "🏪",
        "title": "Trade Post",
        "summary": "Buy and sell with other players.",
        "body": (
            "`/market listings` — browse what's for sale.\n\n"
            "`/market sell` / `/market buy` / `/market cancel` — trade with other players."
        ),
    },
    {
        "key": "allies",
        "emoji": "❤️",
        "title": "Aunt May & MJ",
        "summary": "Keep your allies happy for bonus reputation.",
        "body": (
            "`/ally check` — see how neglected they are.\n\n"
            "`/ally visit` — spend time, or bring a gift from /shop. Repeating the same "
            "gift (or gifting every visit) backfires.\n\n"
            "Keeping both happy boosts reputation gains; neglecting either hurts your "
            "Bugle and Tutoring pay."
        ),
    },
    {
        "key": "lab",
        "emoji": icon_emoji("lab") or "🧪",
        "title": "Chem Lab",
        "summary": "Brew the Web-Fluid that fuels /patrol.",
        "body": (
            f"`/lab brew` — start a {item_label('web_fluid_vial', 'Web-Fluid')} batch. This is what fuels "
            "/patrol.\n\n`/lab status` / `/lab collect` — check on it, then collect."
        ),
    },
    {
        "key": "patreon",
        "emoji": icon_emoji("arachnid") or "🕷️",
        "title": "Patreon Perks",
        "summary": "Two subscriber tiers, and how to connect one.",
        "body": (
            "`/patreon subscribe` — the two tiers and exactly what each one gets you.\n\n"
            "`/patreon link` — connect a pledge you already have. Perks switch on by themselves.\n\n"
            "`/patreon perks` — what's live for you right now, and what gear you haven't bought yet.\n\n"
            "`/patreon status` / `/patreon unlink` — the raw tier the bot reads, and how to disconnect."
        ),
    },
    {
        "key": "loop",
        "emoji": "🔄",
        "title": "The Loop, Start to Finish",
        "summary": "The whole cycle in one line.",
        "body": (
            "`/lab brew` → `/patrol` → `/bugle submit` → `/bank deposit` → "
            "`/apartment pay` + `/workbench repair`."
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
    one — with 12 topics, Prev/Next would mean clicking through most of them just
    to reach the one you want."""

    def __init__(self, author_id: int, timeout: float = 180, accent: int | None = None):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.selected_key: str | None = None
        self.message: discord.Message | None = None
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
            self.children[0].disable_all_items()
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
