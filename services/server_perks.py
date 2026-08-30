"""Which perks the person running this command has earned in the community server.

Resolved from the interaction itself, not from the member cache, and *not* from the
Discord Members intent. The bot runs on discord.Intents.default() (see bot.py) and this
module is the reason it can stay that way: Discord ships the invoker's role list and
guild-boost timestamp inside every interaction payload, and pycord's Member.__init__
stores both verbatim —

    self.premium_since = utils.parse_time(data.get("premium_since"))
    self._roles = utils.SnowflakeList(map(int, data["roles"]))

(discord/member.py, the two lines this whole module rests on.) No gateway event, no
cache warm-up, no privileged intent. GAME_DESIGN.md 9.5 recorded "requires re-enabling
the Discord Members intent" as this track's blocker from 2026-08-21 until it was checked
against those two lines and found to be false.

The consequence that shapes everything below: perks are a property of *the command that
was just run*, not of the account. Run it in a DM or in some other server and there is no
role list in the payload, so there are no perks. That is exactly the scoping the owner
asked for, and it falls out of the transport rather than needing to be enforced.

Perk magnitudes deliberately do NOT live here. Each one sits next to the code it changes
(ally_service.SUPPORTIVE_ALLIES_DECAY_MULTIPLIER, economy.ACCELERATED_GROWTH_XP_MULTIPLIER,
battle_service.HIGHER_INTEGRITY_DAMAGE_REDUCTION, brewing_service.QUICKER_BREW_DURATION,
patrol_service.CAMERA_TIER_STATS). This module answers only "who has which perk" — the
one exception is LOWER_COOLDOWN_MULTIPLIER, because cooldowns are written from four
different modules and this helper is the only chokepoint they share.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import User
from services.patreon_service import TIER_RANK_ARACHNID, TIER_RANK_NONE, get_tier_rank

# A ladder, not a set of flags: Level 10 includes everything Level 5 grants. Written this
# way because leveling bots differ on whether they *strip* the level-5 role when they add
# level-10, and a member who has been promoted must never come out with fewer perks than
# one who hasn't. Comparing a rank makes both wirings produce the same answer.
PERK_RANK_NONE = 0
PERK_RANK_LEVEL_5 = 1
PERK_RANK_LEVEL_10 = 2

# Lower Cooldown, applied by scaled() below. -25%: patrol 30s -> 22s, bugle 60s -> 45s,
# shakedown 120s -> 90s.
LOWER_COOLDOWN_MULTIPLIER = 0.75

# The two occupants of the exclusive slot, as stored in User.perk_pair_choice. Values match
# PatreonLink.growth_perk_choice's ("xp"/"allies") on purpose even though the two columns
# are unrelated — if the dormant one is ever folded into this, the stored data is already
# compatible and no backfill is needed.
PAIR_CHOICE_XP = "xp"
PAIR_CHOICE_ALLIES = "allies"
PAIR_CHOICES = (PAIR_CHOICE_XP, PAIR_CHOICE_ALLIES)


@dataclass(frozen=True, slots=True)
class ServerPerks:
    """Frozen because it gets threaded through a whole patrol — including onto
    PatrolBattleView, where it has to still mean what it meant when the battle started.

    tier_rank is the invoker's *live* Patreon rank and is the one field that can't come
    from the interaction payload. It's carried here rather than passed alongside so that
    every service signature downstream takes exactly one new argument instead of two.

    choice is User.perk_pair_choice, and None ("never picked") is a third state rather than
    a synonym for either value — see _pair(). Like tier_rank it's a database read, so
    perks_from() leaves it None and only resolve_perks() fills it in.
    """

    rank: int = PERK_RANK_NONE
    booster: bool = False
    tier_rank: int = TIER_RANK_NONE
    choice: str | None = None

    # --- Booster-only ---

    @property
    def organic_webbing(self) -> bool:
        return self.booster

    # --- Level 5 and up ---

    @property
    def lower_cooldown(self) -> bool:
        return self.rank >= PERK_RANK_LEVEL_5

    @property
    def higher_integrity(self) -> bool:
        return self.rank >= PERK_RANK_LEVEL_5

    # --- Level 10 ---

    @property
    def quicker_brewing(self) -> bool:
        return self.rank >= PERK_RANK_LEVEL_10

    @property
    def bronze_camera(self) -> bool:
        return self.rank >= PERK_RANK_LEVEL_10

    # --- The exclusive pair ---
    #
    # Higher Reputation XP and Supportive Allies are mutually exclusive, and the two
    # properties below are the grant site GAME_DESIGN.md 9.5 requires that exclusivity to
    # be enforced at. They are the only place in this class that isn't a plain ladder test.
    #
    # Level 10 is where the slot becomes a decision instead of a fixed answer: both perks
    # are available, exactly one can be live, and the member picks with /perks choose. Below
    # level 10 there is nothing to pick — the slot holds Higher Reputation or nothing.
    #
    # Why they can't both be granted, since they look independent: ally_service's
    # reputation_xp_multiplier pays +20% XP while an ally is thriving, and Supportive
    # Allies exists to hold allies in that band far longer (full drain 24h -> ~34h). Held
    # together they're a sustained 1.3 * 1.2 = 1.56x, which is what patreon_service's note
    # means by "stacked, they'd compound past either perk's intended standalone rate."
    #
    # Both properties read _pair() so the two can never disagree about who gets what.

    @property
    def higher_reputation(self) -> bool:
        return self._pair() == PAIR_CHOICE_XP

    @property
    def supportive_allies(self) -> bool:
        return self._pair() == PAIR_CHOICE_ALLIES

    @property
    def can_choose_pair(self) -> bool:
        """Whether /perks choose has anything to offer this member. Level 10 only, because
        that's when the second occupant of the slot unlocks."""
        return self.rank >= PERK_RANK_LEVEL_10

    @property
    def pair_default(self) -> str:
        """What the slot holds for a level 10 member who has never run /perks choose.

        Allies by default, per the owner's call — reaching level 10 should feel like it
        hands you the new thing rather than leaving you where you were. The exception is a
        live Arachnid+ pledge: a subscriber was already being paid up for the XP boost
        before they hit level 10, so switching them off it unprompted would take away
        something they're paying for. Their default keeps it. Either default is a *default*
        and nothing more — both members can switch, and to the same two options.

        Only meaningful at level 10; below that _pair() never consults it.
        """
        return PAIR_CHOICE_XP if self.tier_rank >= TIER_RANK_ARACHNID else PAIR_CHOICE_ALLIES

    def _pair(self) -> str | None:
        # Order matters. This guard is first because the server ladder is what grants the
        # slot at all — without it, any subscriber would get Higher Reputation while
        # standing outside the server, which is a Patreon perk this track was explicitly
        # not allowed to invent. The pledge only ever moves the default (see pair_default).
        if self.rank < PERK_RANK_LEVEL_5:
            return None
        # Level 5 holds one occupant. A stored "allies" from a member who has since lost
        # the level 10 role must not keep granting it — same "the live role is the only
        # source of truth" contract every other perk check uses. The column is left alone
        # so the choice comes back with the role.
        if self.rank < PERK_RANK_LEVEL_10:
            return PAIR_CHOICE_XP
        if self.choice in PAIR_CHOICES:
            return self.choice
        return self.pair_default

    @property
    def any_perk(self) -> bool:
        """Whether this member has anything at all — for surfaces that show a different
        panel to someone with no perks rather than a list of seven dark rows."""
        return self.booster or self.rank > PERK_RANK_NONE


NO_PERKS = ServerPerks()


def _member_of(source) -> object | None:
    """The invoking member, from either an ApplicationContext or a raw Interaction.

    Both shapes are needed: patrol combat is entirely button-driven, so Higher Integrity
    and the Bronze camera fire inside PatrolBattleView callbacks, which only ever see an
    Interaction.
    """
    return getattr(source, "author", None) or getattr(source, "user", None)


def _has_role(member, role_id: int | None) -> bool:
    """True if this member holds role_id.

    Reads Member._roles, the SnowflakeList built straight from the interaction payload,
    rather than the public .roles property. That is not micro-optimisation — .roles
    resolves every id through guild.get_role() and *silently drops* any role the guild
    cache doesn't have, so on a cold cache it returns a short list with no error and the
    perk simply doesn't fire. A perk that fails silently is worse than one that raises.
    The public property is kept as a fallback for objects that aren't real Members (the
    check script's fakes, and any future pycord that renames the private attribute).
    """
    if role_id is None or member is None:
        return False
    snowflakes = getattr(member, "_roles", None)
    if snowflakes is not None:
        return snowflakes.has(role_id)
    return any(getattr(r, "id", None) == role_id for r in getattr(member, "roles", ()))


def in_perks_guild(source) -> bool:
    """Whether this command was run in the community server at all.

    Separate from perks_from() returning NO_PERKS, because those are two different
    situations that want two different answers on screen: a member standing in the server
    with no level roles yet hasn't unlocked anything, while someone in a DM or another
    server is in the wrong place entirely and needs an invite. Collapsing them (which /perks
    did until 2026-08-30) tells the first person they aren't in a server they're standing in.

    False when the feature is switched off, so an unconfigured bot shows the invite rather
    than claiming the player has unlocked nothing.
    """
    guild_id = config.PERKS_GUILD_ID
    return guild_id is not None and getattr(source, "guild_id", None) == guild_id


async def get_pair_choice(session: AsyncSession, discord_id: int) -> str | None:
    """The stored half of the exclusive slot, or None if never picked.

    Reads the row rather than creating it: every command already passes through
    utils/first_run.py's before_invoke hook, so by the time anything asks this the user row
    exists. A missing row here means a caller outside that path, and None is the right
    answer for it.
    """
    user = await session.get(User, discord_id)
    return user.perk_pair_choice if user is not None else None


async def set_pair_choice(session: AsyncSession, user: User, choice: str) -> None:
    """Store which half of the pair this member wants.

    Takes the User rather than an id so the caller owns get-or-create (this module can't
    import services.economy for it — economy imports *this* module for ServerPerks). No
    level check here on purpose: whether the member is allowed to choose is a question about
    their live roles, which only the cog holding the interaction can answer, and doing it
    here would mean re-resolving perks from a source this function doesn't have.
    """
    user.perk_pair_choice = choice
    await session.commit()


def perks_from(source) -> ServerPerks:
    """The payload-only half: roles and boost status, no database, no await.

    tier_rank and choice are left at their defaults, so the exclusive pair resolves as if
    the member had no pledge and had never picked. Callers that care about either want
    resolve_perks(); this one is for the guild/role gate on its own.
    """
    guild_id = config.PERKS_GUILD_ID
    # Unset guild id switches the feature off wholesale. The live bot pulls this code
    # before its .env has the ids, and a perk system firing in the wrong guild is worse
    # than one that does nothing.
    if guild_id is None or getattr(source, "guild_id", None) != guild_id:
        return NO_PERKS

    member = _member_of(source)
    if member is None:
        return NO_PERKS

    rank = PERK_RANK_NONE
    if _has_role(member, config.PERKS_LEVEL_5_ROLE_ID):
        rank = PERK_RANK_LEVEL_5
    # Not elif, and checked second: whichever way the leveling bot handles the level-5
    # role on promotion, the higher rank wins.
    if _has_role(member, config.PERKS_LEVEL_10_ROLE_ID):
        rank = PERK_RANK_LEVEL_10

    # premium_since is the guild-boost timestamp from the same payload, so it's a free
    # second source. Used as a fallback rather than the primary signal because the owner
    # specified a role, and a role can be handed out for reasons Discord doesn't know
    # about.
    booster = _has_role(member, config.PERKS_BOOSTER_ROLE_ID)
    if not booster and config.PERKS_BOOSTER_ROLE_ID is None:
        booster = getattr(member, "premium_since", None) is not None

    return ServerPerks(rank=rank, booster=booster)


async def resolve_perks(session: AsyncSession, source) -> ServerPerks:
    """perks_from() plus the two database-backed inputs the exclusive pair needs.

    This is the one cogs should call. Costs one extra read for anyone holding a level role
    and a second only at level 10 — the early return means the base-case majority pays
    nothing.
    """
    perks = perks_from(source)
    if perks is NO_PERKS or perks.rank < PERK_RANK_LEVEL_5:
        return perks
    member = _member_of(source)
    tier_rank = await get_tier_rank(session, member.id)
    # Only level 10 can have picked, and only level 10 has the stored value read back by
    # _pair(), so there's nothing to fetch below it.
    choice = await get_pair_choice(session, member.id) if perks.rank >= PERK_RANK_LEVEL_10 else None
    return ServerPerks(
        rank=perks.rank, booster=perks.booster, tier_rank=tier_rank, choice=choice
    )


def scaled(seconds: float, perks: ServerPerks) -> float:
    """A gameplay cooldown, cut by Lower Cooldown if it applies.

    Only for cooldowns that are a *pacing* decision — patrol, bugle, shakedown. Do not
    route the others through here:

      - /daily's 24h window IS the definition of a streak, so shortening it doesn't
        speed the player up, it changes what a streak means;
      - the ad, top.gg-vote and stealth-DM throttles are infrastructure, not gameplay;
      - the battle-in-progress lock and tutoring's busy lock guard state machines. A
        shorter lock there is a race, not a perk.
    """
    if not perks.lower_cooldown:
        return seconds
    return seconds * LOWER_COOLDOWN_MULTIPLIER
