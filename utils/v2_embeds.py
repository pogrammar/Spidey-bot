"""Components V2 equivalent of utils/embeds.py — StaticView/static_container are for
plain, non-interactive command responses; PaginatedView below covers Prev/Next
browsing over pre-built pages. Anything with real per-page interactivity beyond
turning pages (patrol battle, gadget panel, shop browse) needs its own bespoke
DesignerView built with ActionRow instead; @discord.ui.button/@discord.ui.select
are not usable directly on a DesignerView at all.

A message sent with a Components V2 view cannot also carry `content` or `embed` —
it must be fully self-contained (see utils/mention_patch.py's is_components_v2
check, which already skips the @mention injection for these)."""

from __future__ import annotations

import discord

from utils import icons


def add_header(
    container: discord.ui.Container, title: str, description: str = "", icon_key: str | None = None
) -> discord.File | None:
    """Adds the title (+ optional description) as the container's header. Renders
    as a Section with a Thumbnail accessory when icon_key is given and that PNG
    exists — returns the discord.File that must be included in the response/edit's
    `files=` list, or the attachment:// URL won't resolve. Falls back to a plain
    '# title' TextDisplay (no file needed, returns None) when there's no icon_key
    or the art hasn't been dropped into assets/icons/ yet — never an error either
    way, just a plainer header until the art shows up."""
    header_text = f"# {title}"
    if description:
        header_text += f"\n{description}"

    if icon_key:
        result = icons.thumbnail(icon_key)
        if result is not None:
            thumb, file = result
            container.add_section(discord.ui.TextDisplay(header_text), accessory=thumb)
            return file

    container.add_item(discord.ui.TextDisplay(header_text))
    return None


def _add_group(
    container: discord.ui.Container, heading: str | None, fields: list[tuple[str, str]], bulleted: bool = False
) -> None:
    """A single field: if its name is explicitly empty (`("", value)`), it merges
    with the heading into one block — "**Aunt May**\\n45/100" — because the
    heading alone already identifies the value and a generic label like
    "Happiness" would just repeat that; the heading stays bold here since it's
    standing in as the value's own label, not tagging a category. Every other
    heading — whether the group has one field or several — renders as the same
    small subtext "eyebrow" tag (Discord's `-#`, uppercased) above the field(s),
    so a category label like "Equipped" or "On Hand" looks the same regardless
    of how many stats happen to be inside it that call. Keeping that consistent
    matters: field count is data-driven (a group can flip between one field and
    several depending on game state), and the heading's visual weight shouldn't
    flicker along with it.

    Multiple fields: eyebrow heading (if any), then either bullets in one
    combined block (bulleted=True — for short, same-shape stats like a
    wallet/bank pair) or each field as its own block with a soft gap between
    (bulleted=False, the default) — pick whichever reads better for that
    specific command; there isn't one right answer for every group."""
    if len(fields) == 1:
        name, value = fields[0]
        if heading and not name:
            container.add_item(discord.ui.TextDisplay(f"**{heading}**\n{value}"))
        elif heading:
            container.add_item(discord.ui.TextDisplay(f"-# {heading.upper()}\n**{name}:** {value}"))
        else:
            container.add_item(discord.ui.TextDisplay(f"**{name}:** {value}" if name else value))
        return

    if heading:
        container.add_item(discord.ui.TextDisplay(f"-# {heading.upper()}"))

    if bulleted:
        lines = [f"• **{name}:** {value}" if name else f"• {value}" for name, value in fields]
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
    else:
        for i, (name, value) in enumerate(fields):
            if i > 0:
                container.add_item(discord.ui.Separator(divider=False))
            container.add_item(discord.ui.TextDisplay(f"**{name}**\n{value}" if name else value))


def add_field_groups(
    container: discord.ui.Container,
    groups: list[tuple[str | None, list[tuple[str, str]]]] | None,
    bulleted: bool = False,
) -> None:
    """Appends each (heading, fields) group to an already-built container — a divider
    before the first group, and between every group after it. Pulled out of
    static_container so an interactive DesignerView (patrol's battle/result cards,
    built by hand around their own Section/ActionRow) can reuse the exact same
    grouping + eyebrow-heading rules instead of re-implementing them."""
    groups = [(heading, group_fields) for heading, group_fields in (groups or []) if group_fields]
    if not groups:
        return
    container.add_item(discord.ui.Separator())
    for i, (heading, group_fields) in enumerate(groups):
        if i > 0:
            container.add_item(discord.ui.Separator())
        _add_group(container, heading, group_fields, bulleted)


def static_container(
    title: str,
    description: str = "",
    fields: list[tuple[str, str]] | None = None,
    field_groups: list[tuple[str, list[tuple[str, str]]]] | None = None,
    bulleted: bool = False,
    icon_key: str | None = None,
) -> tuple[discord.ui.Container, discord.File | None]:
    """`fields` is a flat, unheaded list — fine for short commands with 2-4 related
    values. `field_groups` is `[(heading, fields), ...]` — use it once a command's
    data actually splits into distinct categories (e.g. money vs. standing,
    equipped vs. stored) so the layout reflects that instead of one flat list
    pretending everything's the same kind of thing. Pass one or the other, not both.

    icon_key: see add_header — renders the title as a Section+Thumbnail when the
    art exists. Returns (container, file); the file (if not None) must be passed
    through to the response/edit's `files=` list by the caller.

    Only one visible divider brackets the body (header -> content); different
    *groups* get a real divider between them too. Fields within the same group
    either each get their own block (default) or get combined into one bulleted
    block (bulleted=True) — see _add_group."""
    container = discord.ui.Container()
    file = add_header(container, title, description, icon_key)

    groups = field_groups or ([(None, fields)] if fields else [])
    add_field_groups(container, groups, bulleted)

    return container, file


class StaticView(discord.ui.DesignerView):
    """A single-container, no-buttons V2 view for plain informational responses.

    footer_lines: what this project calls the "footer" on a V2 command — one or
    more small subtext lines at the bottom of the container (Discord's `-#`
    markdown, applied per line, not per block). Typically a functional note (a
    cooldown/warning the player needs) plus a randomized witty one-liner picked
    with random.choice() from that cog's own flavor pool at the call site — see
    any converted cog for the pattern.

    icon_key: pass to give the title a Thumbnail (see add_header). When set and
    the art exists, `self.files` holds the discord.File that must be passed to
    `ctx.respond(view=view, files=view.files)` — always safe to pass even when
    empty, so call sites don't need an if-check of their own."""

    def __init__(
        self,
        title: str,
        description: str = "",
        fields: list[tuple[str, str]] | None = None,
        field_groups: list[tuple[str, list[tuple[str, str]]]] | None = None,
        bulleted: bool = False,
        footer_lines: list[str] | None = None,
        icon_key: str | None = None,
        timeout: float | None = None,
    ):
        super().__init__(timeout=timeout)
        container, file = static_container(title, description, fields, field_groups, bulleted, icon_key)
        if footer_lines:
            container.add_item(discord.ui.Separator())
            subtext = "\n".join(f"-# {line}" for line in footer_lines if line)
            container.add_item(discord.ui.TextDisplay(subtext))
        self.add_item(container)
        self.files: list[discord.File] = [file] if file else []


class _PrevPageButton(discord.ui.Button):
    def __init__(self, pager: "PaginatedView", *, disabled: bool):
        super().__init__(label="Previous", emoji="◀", style=discord.ButtonStyle.secondary, disabled=disabled)
        self.pager = pager

    async def callback(self, interaction: discord.Interaction):
        self.pager.index -= 1
        self.pager._render()
        await interaction.response.edit_message(view=self.pager)


class _NextPageButton(discord.ui.Button):
    def __init__(self, pager: "PaginatedView", *, disabled: bool):
        super().__init__(label="Next", emoji="▶", style=discord.ButtonStyle.secondary, disabled=disabled)
        self.pager = pager

    async def callback(self, interaction: discord.Interaction):
        self.pager.index += 1
        self.pager._render()
        await interaction.response.edit_message(view=self.pager)


class PaginatedView(discord.ui.DesignerView):
    """Prev/Next pager over a list of static_container-shaped pages — for content-only
    browsing (/market listings) with no per-page interactivity beyond turning pages.
    Each page is a dict of static_container's kwargs (title, description,
    fields/field_groups, bulleted).

    icon_key adds an inline emoji before the title (see utils.icons.emoji) rather
    than a Thumbnail accessory — the accessory slot here is already doing real work
    (the page counter), and a Section can only have one accessory, so the icon has
    to share space with the title text instead of competing for that slot."""

    def __init__(
        self,
        pages: list[dict],
        author_id: int,
        footer_lines: list[str] | None = None,
        icon_key: str | None = None,
        timeout: float = 180,
    ):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.author_id = author_id
        self.footer_lines = footer_lines
        self.icon_key = icon_key
        self.index = 0
        self.message: discord.Message | None = None
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

    def _render(self) -> None:
        self.clear_items()
        page = self.pages[self.index]

        container = discord.ui.Container()
        icon_prefix = icons.emoji(self.icon_key) if self.icon_key else None
        title_line = f"{icon_prefix} {page['title']}" if icon_prefix else page["title"]
        header_text = f"# {title_line}"
        if page.get("description"):
            header_text += f"\n{page['description']}"
        container.add_section(
            discord.ui.TextDisplay(header_text),
            accessory=discord.ui.Button(
                label=f"{self.index + 1}/{len(self.pages)}", style=discord.ButtonStyle.secondary, disabled=True
            ),
        )

        groups = page.get("field_groups") or ([(None, page["fields"])] if page.get("fields") else [])
        add_field_groups(container, groups, page.get("bulleted", False))

        if self.footer_lines:
            container.add_separator()
            subtext = "\n".join(f"-# {line}" for line in self.footer_lines if line)
            container.add_text(subtext)

        container.add_separator()
        container.add_row(
            _PrevPageButton(self, disabled=self.index == 0),
            _NextPageButton(self, disabled=self.index == len(self.pages) - 1),
        )

        self.add_item(container)
