import discord
from discord.ext import commands

from db.base import async_session
from services.economy import get_or_create_user
from utils.embeds import SPIDEY_BLUE, base_embed
from utils.views import Paginator

PAGES = [
    {
        "title": "Friendly Neighborhood Cheat Sheet — Earning",
        "description": (
            "You're Peter Parker. Rent's due, the camera's held together with tape, and "
            "being Spider-Man doesn't pay a cent by itself — you make it pay."
        ),
        "fields": [
            (
                "🔥 Daily",
                "`/daily claim` — free cash, XP, and a random bonus pull (gifts, "
                "components, Web-Fluid, rare gadget upgrades, even rarer collectibles) "
                "every real day. Rewards grow with your streak, and hit big at "
                "milestones (7, 14, 30, 60, 100 days). Miss more than 48 hours and the "
                "streak resets — `/daily status` to check without claiming.",
            ),
            (
                "🕸️ Earning",
                "`/patrol` — swing out and see what's happening. Costs 1 Web-Fluid Vial "
                "(cash instead if you're out). Crimes turn into a real fight: Attack, "
                "Evade, or Use Gadget each round — your choices decide the outcome, not "
                "how fast you click. Evade sets up a guaranteed bonus-damage Attack next "
                "round, so it's not just defense — a real second path to winning, not "
                "just attack spam. Gets tougher as your reputation level climbs. 30 sec cooldown.\n\n"
                "`/bugle photos` — check what photos you're holding.\n\n"
                "`/bugle submit` — sell them all to JJJ. 1 min cooldown.\n\n"
                "`/tutoring` — guaranteed safe cash, but locks you out of patrol for 2 min.",
            ),
            (
                "💰 Money",
                "`/balance` — wallet, bank, reputation, suit integrity.\n\n"
                "`/inventory` — what you're carrying.\n\n"
                "`/bank deposit` / `/bank withdraw` — wallet cash can be stolen, bank cash "
                "can't. Bank capacity grows on its own as you fill it.",
            ),
        ],
    },
    {
        "title": "Friendly Neighborhood Cheat Sheet — Upkeep",
        "fields": [
            (
                "🏚️ Bills",
                "`/apartment status` — rent due date and eviction meter.\n\n"
                "`/apartment pay` — pay $400 rent. Miss it too often and your workbench "
                "gets locked.",
            ),
            (
                "🔧 Suit",
                "`/workbench status` — integrity, repair cost, components on hand.\n\n"
                "`/workbench repair` — restore to 100% using cash + scavenged components. "
                "Patrol warns you if you're low and can't afford it.",
            ),
            (
                "🦾 Gadgets",
                "`/gadget panel` — click-through menu to equip, unequip, and upgrade.\n\n"
                "Carry **2 at once** — in a patrol battle you get a separate \"Use\" button "
                "for each, so which two you bring is a real choice. Unlocks by reputation "
                "level: Web-Shooters (1), Web Grabber (5), Ricochet Web (10), Upshot (15), "
                "Concussion Burst (20). Each can wear out and break mid-fight.",
            ),
        ],
    },
    {
        "title": "Friendly Neighborhood Cheat Sheet — Trading & PvP",
        "fields": [
            (
                "🥊 PvP",
                "`/shakedown @user` — try to steal a cut of their wallet. Can backfire and "
                "cost you instead.",
            ),
            (
                "🛒 General Store",
                "`/shop browse` or `/shop buy` — camera, repair components, gifts, gadgets.",
            ),
            (
                "🏪 Trade Post",
                "`/market listings` — browse what's for sale.\n\n"
                "`/market sell` / `/market buy` / `/market cancel` — trade with other players.",
            ),
        ],
    },
    {
        "title": "Friendly Neighborhood Cheat Sheet — People & Side Hustles",
        "fields": [
            (
                "❤️ Aunt May & MJ",
                "`/ally check` — see how neglected they are.\n\n"
                "`/ally visit` — spend time, or bring a gift from /shop. Repeating the same "
                "gift (or gifting every visit) backfires.\n\n"
                "Keeping both happy boosts reputation gains; neglecting either hurts your "
                "Bugle and Tutoring pay.",
            ),
            (
                "🧪 Chem Lab",
                "`/lab brew` — start a Web-Fluid batch. This is what fuels /patrol.\n\n"
                "`/lab status` / `/lab collect` — check on it, then collect.",
            ),
            (
                "The loop, start to finish",
                "`/lab brew` → `/patrol` → `/bugle submit` → `/bank deposit` → "
                "`/apartment pay` + `/workbench repair`.",
            ),
        ],
    },
]


def _build_pages() -> list[discord.Embed]:
    embeds = []
    for i, page in enumerate(PAGES):
        embed = base_embed(page["title"], page.get("description", ""), colour=SPIDEY_BLUE)
        for name, value in page["fields"]:
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text=f"Page {i + 1}/{len(PAGES)}")
        embeds.append(embed)
    return embeds


class HelpCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="start", description="Brand new? Run this first.")
    async def start(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            await get_or_create_user(session, ctx.author.id)

        embed = base_embed(
            "Friendly Neighborhood Orientation",
            "You're Peter Parker — broke, in a homemade suit, rent due soon. Being "
            "Spider-Man doesn't pay a cent by itself. Here's where to start.",
            colour=SPIDEY_BLUE,
        )
        embed.add_field(
            name="1. Claim your first reward",
            value="`/daily claim` — free cash, reputation XP, and a random bonus pull. "
            "Come back and claim it again tomorrow — the streak is worth building.",
            inline=False,
        )
        embed.add_field(
            name="2. Get out there",
            value="`/patrol` — swing out and see what's happening. Costs a Web-Fluid "
            "Vial (or cash if you're out), and crimes turn into a real fight.",
            inline=False,
        )
        embed.add_field(
            name="3. Everything else",
            value="`/help` — the full rundown: money, gear, allies, trading, and more.",
            inline=False,
        )
        await ctx.respond(embed=embed)

    @discord.slash_command(name="help", description="New here? Start with this.")
    async def help(self, ctx: discord.ApplicationContext):
        embeds = _build_pages()
        view = Paginator(embeds, author_id=ctx.author.id)
        await ctx.respond(embed=embeds[0], view=view)


def setup(bot: discord.Bot):
    bot.add_cog(HelpCog(bot))
