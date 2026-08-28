import datetime
import random

import discord
from discord.ext import commands

from db.base import async_session
from services.brewing_service import BREW_COST, YIELD_RANGE, collect_brew, get_brew_status, start_brew
from services.economy import get_or_create_user
from services.patreon_service import PATREON_SHOP_URL, VIAL_BUNDLES, format_bundle_price
from utils.embeds import error_embed, link_button_view
from utils.icons import item_label
from utils.v2_embeds import StaticView

LAB_ICON = "lab"
VIAL_ICON = "web_fluid_vial"
MUTATION_ICON = "unstable_web_fluid"

LAB_FOOTERS = [
    "ESU's chem lab was not built for this.",
    "Somewhere, a professor is very confused about the missing beakers.",
    "Web fluid: 10% chemistry, 90% vibes.",
    "Curt Connors would not approve of these safety standards.",
]

# ---------------------------------------------------------------------------
# The Patreon vial-shop nudge (2026-08-28)
# ---------------------------------------------------------------------------
# Real money, bought off-platform, fulfilled by hand — see patreon_service.VIAL_BUNDLES
# for the fulfilment contract. Nothing in this cog knows or can know whether anybody has
# bought anything; these are link buttons and copy, full stop.
#
# EVERY path out of all three commands carries the nudge, success and refusal alike
# (owner's call, revised 2026-08-28 — the first pass put it on the three success cards
# only). One line per path, because each moment is selling something different: brewing
# sells the wait, collecting sells the yield, status sells whichever the player is sitting
# in, and a refusal sells the thing they just failed to get.
#
# On the refusals this reverses an earlier decision of mine, and the reasoning that
# argued against it is worth keeping rather than deleting, because it's the reason the
# refusal copy is written differently from the success copy. Turning a refusal into a
# sales pitch is the mistake round six fixed in the other direction on /shakedown, and
# "you can't afford this, here's how to pay us" is the version of that with money
# attached. What makes it defensible here: the refusals are the highest-intent moment in
# the command — the player wanted vials *right now* and the game said no — and the pitch
# is an answer to that rather than a non-sequitur. So the nudge goes on them, but quietly:
# plain prose, no bold, appended as its own paragraph under the error, with the button
# alongside rather than the whole card restyled to sell. If it ever reads as the bot
# needling somebody it just turned away, this paragraph is the thing to reconsider first.
#
# The refusals stay classic `error_embed`s and must NOT become Components V2 cards. Two
# reasons: "Parker Luck." grey is the shared error identity across every cog, and
# make_container() would paint a refusal in the caller's own Patreon accent. See
# utils.embeds.link_button_view, which exists for exactly this.
#
# Every figure is interpolated — the bundle price from VIAL_BUNDLES[0], the per-batch
# yield from brewing_service.YIELD_RANGE. The comparison the copy is built on ("a batch
# gets you 2-4, this gets you 30") is only tempting while it's true, and it stops being
# true the moment either constant moves. This is the same rule §9.3 applies to perk copy,
# and it binds harder here: a stale number in a price quote is a stale number the bot is
# asserting about real money.
#
# Deliberately NOT a listing of all four bundles: the shop page is the catalog, and a
# four-row price table inside a chem-lab card reads as an ad break rather than a nudge
# (owner's call, 2026-08-28). Only the cheapest bundle is quoted, as an entry price, with
# "and up" doing the work of implying the rest — which is why every line says the stashes
# *start* at this price rather than that they cost it. The word matters: quoting $3 flat
# would be false the moment somebody clicks through to a $19.99 option.
#
# Also deliberately NOT gated on Patreon tier. A one-time vial purchase and a recurring
# pledge are different products, so a Symbiote subscriber seeing this is being shown
# something they don't already have — unlike the §9 perk surfaces, there's nothing here a
# subscription makes redundant.
_BUNDLE_VIALS, _BUNDLE_PRICE = VIAL_BUNDLES[0]
_BUNDLE_PRICE_TEXT = format_bundle_price(_BUNDLE_PRICE)
_YIELD_LOW, _YIELD_HIGH = YIELD_RANGE

# No price on the button (owner's call, 2026-08-28). A button is a label for an action,
# and the price belongs in the sentence that frames it — quoting a single figure on the
# button also fights the "starts at, goes up" framing the copy is built on, since a
# button has no room to say "and up" without reading as clutter.
SHOP_BUTTON_LABEL = "Buy Web-Fluid Stash"

SHOP_PITCH_BREW = (
    f"Or don't wait. Web-Fluid stashes on the Patreon shop **start at {_BUNDLE_PRICE_TEXT}** for "
    f"{_BUNDLE_VIALS} vials and go up from there — no hotplate, no timer, no smell."
)
SHOP_PITCH_STATUS = (
    f"Tired of watching a timer? Web-Fluid stashes **start at {_BUNDLE_PRICE_TEXT}** for "
    f"{_BUNDLE_VIALS} vials on the Patreon shop and climb from there, landing in your inventory "
    "ready to swing."
)
SHOP_PITCH_COLLECT = (
    f"{_YIELD_LOW}-{_YIELD_HIGH} vials a batch adds up slowly. Stashes on the Patreon shop "
    f"**start at {_BUNDLE_VIALS} vials for {_BUNDLE_PRICE_TEXT}** and go up from there — that's a "
    "whole night of brewing, skipped."
)

# The refusal lines. Plain prose, no bold — see the note above on why these are quieter
# than the three above them. Each has to fit *both* of its command's failure reasons,
# because the service hands back one message and the cog can't tell which one it got:
# brew fails on an active batch or a wallet under $30, collect on nothing brewing or a
# batch that isn't ready.
#
# Note what the brew line does NOT say: it doesn't quote BREW_COST. "start at $3 ... no
# $30" puts two prices one clause apart, and the smaller one is a substring of the larger,
# which is confusing to read and was actively misleading in the first draft. "No chemicals
# to buy" carries the wallet-short case without a second figure, and covers the
# already-brewing case at the same time.
SHOP_PITCH_BREW_FAIL = (
    f"Web-Fluid stashes start at {_BUNDLE_PRICE_TEXT} for {_BUNDLE_VIALS} vials on the Patreon "
    "shop and go up from there — no chemicals to buy, no five-minute wait."
)
SHOP_PITCH_COLLECT_FAIL = (
    f"Not in the mood to wait? Web-Fluid stashes start at {_BUNDLE_PRICE_TEXT} for "
    f"{_BUNDLE_VIALS} vials on the Patreon shop and go up from there, ready the moment they land."
)

SHOP_BUTTON = (SHOP_BUTTON_LABEL, PATREON_SHOP_URL)


def shop_refusal(message: str, pitch: str) -> tuple[discord.Embed, discord.ui.View]:
    """A refusal that still points at the shop — the error text, its pitch as a separate
    paragraph, and the shop button beside it. Returns `(embed, view)` to be splatted into
    `ctx.respond(embed=..., view=...)`; kept as one helper so the two-paragraph shape and
    the button can't drift apart between brew's refusal and collect's."""
    return error_embed(f"{message}\n\n{pitch}"), link_button_view(*SHOP_BUTTON)



class LabCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    lab = discord.SlashCommandGroup("lab", "Empire State University's chem lab — brew Web-Fluid on the side.")

    @lab.command(name="status", description="Check on your current batch.")
    async def status(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            brew = await get_brew_status(session, user.discord_id)

        if brew is None:
            view = StaticView(
                "Chem Lab",
                f"Nothing brewing. Start a batch for ${BREW_COST} with /lab brew.",
                footer_lines=[SHOP_PITCH_STATUS, random.choice(LAB_FOOTERS)],
                icon_key=LAB_ICON,
                link_button=SHOP_BUTTON,
            )
            await ctx.respond(view=view, files=view.files)
            return

        now = datetime.datetime.utcnow()
        if brew.ready_at <= now:
            status_text = "Ready to collect — run /lab collect."
        else:
            minutes = int((brew.ready_at - now).total_seconds() // 60)
            status_text = f"Still cooking, about {minutes} minutes left."
        view = StaticView(
            "Chem Lab",
            status_text,
            footer_lines=[SHOP_PITCH_STATUS, random.choice(LAB_FOOTERS)],
            icon_key=LAB_ICON,
            link_button=SHOP_BUTTON,
        )
        await ctx.respond(view=view, files=view.files)

    @lab.command(name="brew", description="Start a new Web-Fluid batch.")
    async def brew(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message = await start_brew(session, user)
        if ok:
            view = StaticView(
                "Chem Lab",
                description=message,
                footer_lines=[SHOP_PITCH_BREW, random.choice(LAB_FOOTERS)],
                icon_key=VIAL_ICON,
                link_button=SHOP_BUTTON,
            )
            await ctx.respond(view=view, files=view.files)
        else:
            embed, view = shop_refusal(message, SHOP_PITCH_BREW_FAIL)
            await ctx.respond(embed=embed, view=view)

    @lab.command(name="collect", description="Collect your batch, if it's ready.")
    async def collect(self, ctx: discord.ApplicationContext):
        async with async_session() as session:
            user = await get_or_create_user(session, ctx.author.id)
            ok, message, result = await collect_brew(session, user)

        if not ok:
            embed, view = shop_refusal(message, SHOP_PITCH_COLLECT_FAIL)
            await ctx.respond(embed=embed, view=view)
            return

        fields = []
        if result.mutated:
            mutation_label = item_label(MUTATION_ICON, "Unstable Web-Fluid")
            fields.append((
                "Mutation!", f"One vial came out wrong — an {mutation_label}. Might be worth something.",
            ))
        view = StaticView(
            "Chem Lab — Batch Ready",
            f"You collect {result.vials}x {item_label(VIAL_ICON, 'Web-Fluid Vial')}.",
            fields=fields,
            footer_lines=[SHOP_PITCH_COLLECT, random.choice(LAB_FOOTERS)],
            icon_key=MUTATION_ICON if result.mutated else VIAL_ICON,
            link_button=SHOP_BUTTON,
        )
        await ctx.respond(view=view, files=view.files)


def setup(bot: discord.Bot):
    bot.add_cog(LabCog(bot))
