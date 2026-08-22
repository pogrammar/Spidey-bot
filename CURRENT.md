# Current Session State — 2026-08-21 (end of session)

Snapshot of exactly where things stand. Read [GAME_DESIGN.md](GAME_DESIGN.md) first for the actual mechanics — this file is about *status*: what's committed, what's uncommitted, what's still open, and what to do next.

## Headline: today's changes are uncommitted on top of `212800d`

`git log` shows `212800d` ("patreon perks under dev") is the last commit — that one already contains the full Arachnid/Symbiote perk system, Stealth Mode, and the Windows ngrok fixes from the *previous* session (despite an earlier draft of this file claiming that work was still uncommitted — it wasn't; that was a stale note, now corrected). **Everything below is today's session's work, uncommitted on top of `212800d`.** `git diff --stat`: 10 files changed, 237 insertions, 79 deletions. No untracked files this time — the two icon renames (see below) show up as tracked renames.

**Modified (tracked, uncommitted):**
```
GAME_DESIGN.md              cogs/ally_cog.py            cogs/patreon_cog.py
cogs/patrol_cog.py          cogs/shop_cog.py             data/items.json
services/battle_service.py  services/gadget_service.py  services/patrol_service.py
services/shop_service.py
```
Plus two tracked renames: `assets/icons/Electric_Webbing.png` → `electric_webbing.png`, `assets/icons/Spider_Bots.png` → `spider_bots.png` (fixes a case-sensitivity bug — the lookup code reads the lowercase item key, so these silently wouldn't have resolved on the case-sensitive Linux prod deployment even though they worked fine on Windows).

**DB state**: local dev SQLite DB is re-seeded and includes `spider_bots`, `electric_webbing`, and the new `camera_silver` item. Committing is purely a code-catch-up — no new Alembic migration was needed this session (no schema changes, just new `Item`/`items.json` rows, which `db/seed.py` upserts on every startup).

## What changed this session (all functionally verified, not just code review)

1. **Emoji-only tier attribution** — dropped the redundant "Arachnid"/"Symbiote" text wherever it appeared next to the tier emoji (`_arachnid_tag()` in `battle_service.py`, the Organic Webbing patrol line, the ally-decay footer note). GAME_DESIGN.md's attribution rule (§9) updated to match: emoji alone carries it now, no tier-name text.

2. **Spider Bots / Electric Webbing: passive perk → real gadget → normal selectable gadget** (this flip-flopped twice today, see below) — landed as **ordinary selectable gadgets**, Arachnid+-gated at purchase only:
   - Added to `data/items.json` (Spider Bots $550/lvl8, Electric Webbing $750/lvl14) and wired into `gadget_service.GADGET_EFFECTS` with two new effect kinds (`bonus_damage`, `shock_burst`) — same Select/button flow, wearout, and upgrade path as the original 5 gadgets.
   - `shop_service.ARACHNID_GATED_ITEM_KEYS` blocks the *purchase* for non-Arachnid+ subscribers with a "subscribe and /patreon link" message; everyone still sees them in the shop at full price/description.
   - **Design history worth knowing**: this started the session as a straight shop-visibility ask, which I first implemented as an always-on *passive* tier perk turned into a gated purchasable gadget (no player-facing button, fired automatically each round) — because that matched the mechanic's existing behavior. The user then flagged that patrol had no buttons for them and asked to make them real selectable gadgets instead, which is the current, final shape. If a future session sees old references to "passive Arachnid gadgets" anywhere (memory, old chat context), that's superseded — trust GAME_DESIGN.md §6.1 over it.

3. **Silver-Grade Camera** (`camera_silver`, $1,000, Arachnid+-gated) — a real second Gear-section shop item, not the `upgrade_level`-on-one-item design GAME_DESIGN §9.5 originally sketched (deliberately simplified this session per explicit direction, to literally show up as a second browsable entry). `patrol_service.py` now generalizes camera handling via `CAMERA_FAMILY_KEYS`/`CAMERA_TIER_STATS`/`get_equipped_camera`/`camera_tier_stats`/`bump_photo_quality`; buying either camera-family tool auto-unequips the other. Applies -70% break chance and ~32.5% photo-quality-bump chance (GAME_DESIGN §6.2). **Camera Gold tier is still unbuilt** — same pattern would extend it (`camera_gold`, add to `CAMERA_FAMILY_KEYS`, entry in `CAMERA_TIER_STATS`), -85%/~50% per the original §9.5 numbers.

4. **Arachnid branding in `/shop browse` and `/shop list`** — selecting (or listing) an Arachnid+-gated item now shows a 🕷️ "Patreon exclusive" note ahead of its description.

5. **New `/patreon perks` command** — a plain-English checklist of every perk currently active for the caller (tier-gated free perks + drawbacks), plus owned/not-owned status for the three Arachnid+-gated purchasable items, since being subscribed only unlocks the *ability* to buy those, not the items themselves. Complements the existing `/patreon status` (which just shows the raw tier).

All of the above were smoke-tested against the real local dev DB this session: seeded the new items, bought/equipped Spider Bots and Electric Webbing as a live test Arachnid-tier user, ran real `resolve_gadget` rounds and confirmed proc/fumble/wearout/no-counter behavior, bought Silver Camera and confirmed it auto-swapped out the starter camera, then cleaned up the test account (`wipe_user`). All 23 bot extensions load cleanly end-to-end (`load_extension` on every entry in `bot.EXTENSIONS`).

## Carried over from last session, still true

- **Venom Blast**'s `2x attack roll` bonus damage is still a formula-based guess, not simulation-tuned against the documented 70-75%/17-25% boss win-rate benchmarks. Unchanged this session.
- **Electric Webbing / Spider Bots proc chances** (0.20 base, +0.12/upgrade level) are still picked by comparison to existing gadget values, not a binary-search balance pass. Unchanged this session (the mechanic shape changed, the numbers didn't).

## RESOLVED 2026-08-21: the perk-track question is answered

This was the standing blocker. Two tracks exist and must not be conflated (a mistake made and corrected once already, 2026-08-18):
1. **Patreon tiers** (Arachnid/Symbiote) — built and live, GAME_DESIGN.md §9.1-9.3, §6.1-6.2.
2. **Server-boost-exclusive perks** (discord.gg/spider-man Nitro boosting) — built once, then **fully reverted** per explicit user instruction (commits `76dbc26`/`a05e719`, both on `main`). Not rebuilt since. `get_growth_choice`/`set_growth_choice` in `patreon_service.py` were originally built for *this* track — dormant, do not re-wire to Patreon.

**The decision (user, 2026-08-21):**
- **Server Booster track** owns Higher Suit Integrity, Higher Reputation XP, Supportive Allies, and Quicker Web Brewing.
- **Symbiote tier** owns Camera Gold (a step above Silver's Arachnid+ gate).
- **Not building any of them yet** — "we will work on those later." Numbers were already locked and are now written into GAME_DESIGN.md §9.5 alongside the track assignment, so §9.5 is self-sufficient; the session-memory file is no longer the only place they live.

One thing to double-check whenever this gets picked up: **Quicker Web Brewing** was originally locked as ungated/everyone, and the 2026-08-21 decision moved it under the Booster track. Recorded as Booster-gated per that direction — worth one confirmation before code, in case "the first 4" was a broad-stroke grouping rather than a deliberate change on that specific perk.

Practical note for whenever the Booster track is actually rebuilt: it needs the Discord **Members intent** re-enabled to read boost status — a live-bot config change, not just code. That plus the fact it was explicitly reverted once means it warrants a check-in before starting, even though the *ownership* question is now settled.

## Suggested next steps, in order

1. **Commit today's work** when ready. A full working feature (Arachnid+-gated gadgets/camera, shop branding, `/patreon perks`) plus the doc updates are sitting uncommitted. Asked and declined on 2026-08-21 — deliberately left dirty, not an oversight. Splitting into a couple of logical commits would read better than one giant commit.
2. Once real playtest data exists with Venom Blast live, revisit its tuning against the documented boss win-rate benchmarks.
3. Camera Gold, when built, is a small extension of the Silver pattern (§6.2) — not a new design problem. Assigned to Symbiote per above.
4. The 4 Booster-track perks need the Members intent decision before any code.

---

# HANDOFF — 2026-08-21 ~12:50 UTC (session hit its API budget mid-audit)

**Nothing in this section is implemented.** It is a work order. The audit below is real (derived from actually reading the code, not guessed), but only two of the seven relevant files were read before the budget ran out — see "What was NOT verified" before trusting any claim about the shop.

## What the user asked for, in their words

1. "the silver camera has this stat of upgrading your photos **3 in 5 times**" — i.e. `quality_bump_chance` must become **0.60**, up from the currently-shipped 0.325.
2. "when the photo is upgraded in patrol, the **arachnid branding needs to be shown** and it should say photo upgraded and all" — the bump currently happens completely invisibly. This is the headline bug.
3. "This arachnid tier is a **$5/month** taxing scheme, so the user who uses it needs to **feel premium**" — the governing design principle for every judgement call below. When in doubt, make the perk *visible*, not just numerically bigger.
4. "everyone should be able to **see** the electric webbing etc gadgets" — verify, don't assume (CURRENT.md claims this already works; unverified this session).
5. "some things may be **inconsistent or uncalculated** in the way these arachnid perks were implemented" — the user's instinct here is **correct**. See the attribution audit, it is the single biggest finding.
6. "the arachnid tier work is UNDONE" — treat the tier as unfinished, not as shipped-and-polished.

## Files actually read this session (claims about these are trustworthy)

- `services/patrol_service.py` (437 lines, read in full)
- `services/battle_service.py` (749 lines, read in full)

## What was NOT verified — read these first before acting

`services/gadget_service.py`, `services/shop_service.py`, `cogs/shop_cog.py`, `cogs/patrol_cog.py`, `utils/icons.py`, `cogs/patreon_cog.py`. In particular **ask #4 (shop visibility) was never checked** — a read of `services/gadget_service.py` was the next action when the budget ran out. Do not report ask #4 as done without reading `list_shop_items`/`ShopBrowseView`/`shop_item_autocomplete` and confirming `spider_bots`/`electric_webbing`/`camera_silver` appear for a `TIER_RANK_NONE` user in all three of `/shop list`, `/shop browse`, and `/shop buy`'s autocomplete.

## Task 1 — Silver camera bump chance 0.325 → 0.60 ("3 in 5")

`services/patrol_service.py:36`:
```python
CAMERA_SILVER_ITEM_KEY: {"break_chance_reduction": 0.70, "quality_bump_chance": 0.325},
```
→ change `0.325` to `0.60`. Also rewrite the comment block at `patrol_service.py:28-33`, which currently justifies 0.325 as "the midpoint of the ~30-35% range from GAME_DESIGN §9.5" — that justification is now dead. The user overrode the old locked range directly; 3-in-5 is the new spec.

Then update the docs to match (they currently state the old number in three places):
- `GAME_DESIGN.md` §6.2 — the `CAMERA_TIER_STATS` line quoting `0.325` and the "(0.325 = the midpoint of the "30-35%" range §9.5 originally locked in)" parenthetical.
- `GAME_DESIGN.md` §9.5 — the Camera Gold entry quotes the locked ladder "base 0% / Silver -70% / Gold -85%" break reduction and "Silver ~30-35% / Gold ~45-55%" bump. Silver's bump is now 60%, which **now exceeds Gold's locked 45-55%** — the ladder is inverted and Gold's number must be re-decided with the user (suggest ~75-80% to keep the ladder monotonic, but do not pick it unilaterally, it's a paid-tier number).
- The memory file `booster_perk_tier_design.md` — its "Locked-in ladder" line in the Bronze Camera Tier section.

**Flag to the user**: bumping Silver to 60% while Gold is still specced at 45-55% breaks the tier ladder. Gold is now assigned to Symbiote (decided earlier today), so Gold needs a higher number than Silver's 60%.

## Task 2 — the photo bump is completely invisible (the headline bug)

`services/battle_service.py:646-667` bumps the quality and banks the photo, but **nothing records that a bump happened**, so the result screen cannot mention it:
```python
banked_photo_quality = stats["photo_quality"]
if random.random() < tier_stats["quality_bump_chance"]:
    banked_photo_quality = bump_photo_quality(banked_photo_quality)
```
`BattleReport` (`battle_service.py:367-386`) has `photo_banked` / `photo_quality` / `camera_broke` but **no `photo_quality_bumped` field**. So `cogs/patrol_cog.py`'s `_render_final` has no way to know, and an Arachnid subscriber paying $5/month sees a Silver photo where a Bronze one would have been with zero indication their camera did anything. This is exactly ask #2.

Concrete fix:
1. Add to `BattleReport`: `photo_quality_bumped: bool = False` and `photo_quality_before_bump: str | None = None` (keep the original tier so the UI can say "Bronze → Silver", which reads far more premium than a bare "upgraded").
2. In `finalize_battle`, capture both when the roll succeeds, and pass them into the returned `BattleReport(...)` at `battle_service.py:730-749`.
3. In `cogs/patrol_cog.py`'s `_render_final`, under the existing "Photo Op" field group, add a line when `photo_quality_bumped` — e.g. `f"Silver-Grade Camera: Bronze → Silver photo {emoji('arachnid')}"`. Per `GAME_DESIGN.md` §9's attribution rule the 🕷️ emoji is **mandatory** and the tier-name text is **forbidden** (emoji alone carries attribution — that rule was tightened earlier today, don't reintroduce the word "Arachnid").
4. The memory file's UI note for this is still accurate and worth following: show it as an additional line under the existing Bronze/Silver "Photo Op" field group, framed as the camera "being good enough for a better shot," not as its own new section.

## Task 3 — the perk-attribution audit (this is the "inconsistent" the user sensed)

`GAME_DESIGN.md` §9 states the rule without exception: *whenever a perk actually fires, the tier's emoji must appear inline in the same message. Never just a quietly bigger number.* Measured against the code, **only one of the ten perk trigger points actually complies.** This is the substance of ask #5 and the biggest single lever on ask #3 (premium feel).

| Perk | Where it fires | Attribution today | Fix |
|---|---|---|---|
| Enhanced Strength | `battle_service.py:497-506` | ✅ correct — appends `" (Enhanced Strength)" + _arachnid_tag()` | none, use as the reference pattern |
| Photo-quality bump | `battle_service.py:653-654` | ❌ none at all | Task 2 |
| Venom Blast | `VENOM_BLAST_LINES`, `battle_service.py:452-456` | ❌ three flavor lines, **no symbiote emoji on any of them** | append a symbiote tag to the returned line at `:480` |
| Sonic Dampener (drawback) | `battle_service.py:468-469` | ❌ silently multiplies incoming damage by 1.3, **emits no line whatsoever** | the player currently cannot tell why the Shocker hits harder. Needs a visible line — a drawback the payer can't see is just a stealth nerf on a paid tier |
| Biomorphic Webbing — cash | `battle_service.py:682-683` and `patrol_service.py:355-356` | ❌ `cash += ...` with no flag and no `BattleReport`/`PatrolResult` field | add a `biomorphic_cash: int = 0` field to both dataclasses, render with the symbiote emoji |
| Biomorphic Webbing — component | `battle_service.py:676-680` | ❌ sets `item_found` **identically to a normal drop** — indistinguishable from base-game luck | add a `biomorphic_component: bool` flag, render distinctly |
| Biomorphic Webbing — photo | `battle_service.py:665-666` | ❌ silently banks a second `PendingPhoto` | add a flag + line |
| Spider Bots (`bonus_damage`) | `battle_service.py:593`, flavor at `:611` | ❌ no tier tag — a Patreon-exclusive gadget procs with zero branding | see note below |
| Electric Webbing (`shock_burst`) | `battle_service.py:583-588` | ❌ same | see note below |
| Combat-Ready Patrols | `patrol_service.py:393-394` | ⚠️ inherently un-attributable — it's a weight bonus applied *before* an outcome exists, so there's no "it fired" moment to tag | accepted gap, but consider a one-line note on the patrol result card for combat outcomes |

**Note on Spider Bots / Electric Webbing**: these are now ordinary purchasable gadgets (§6.1), so an argument exists that they need no tier tag — you bought them, they're yours. Counter-argument, and the one that matches ask #3: they are Arachnid+-exclusive $5/month content, and a subscriber firing them should *see* that this is something no free player can do. **Recommend tagging them**, and confirm with the user since it's a taste call, not a bug.

Infrastructure needed: `battle_service.py:434-436` has `_arachnid_tag()` but **no symbiote equivalent**. Add `_symbiote_tag()`, or better, a single `_tier_tag(rank: int) -> str` that resolves `arachnid`/`symbiote` from `utils/icons.py`'s `emoji()`. Both emoji keys are already uploaded and wired (per the memory file's icon-status section), so this is not blocked on art.

## Task 4 — Biomorphic Webbing's bonus photo ignores the camera tier

`battle_service.py:665-666`:
```python
if tier_rank >= TIER_RANK_SYMBIOTE and random.random() < BIOMORPHIC_WEBBING_PHOTO_CHANCE:
    session.add(PendingPhoto(user_id=user.discord_id, quality=stats["photo_quality"]))
```
It banks at `stats["photo_quality"]` — the **raw, un-bumped** tier — so a Symbiote subscriber holding a Silver camera gets a bonus photo their camera had no effect on. `GAME_DESIGN.md` §6.2 currently documents this as deliberate, but it is the exact kind of "uncalculated" inconsistency ask #5 points at: same fight, same camera, two photos, only one benefits.

Recommended: give the bonus photo its **own independent bump roll** (not a copy of the first roll's outcome — independent, matching how the three Biomorphic rolls are already independent of each other). Then update §6.2, which explicitly promises the opposite today. Worth one confirmation with the user since it is a real buff to the top tier.

## Task 5 — verified NOT a bug, do not "fix" these

Both looked wrong on first read and are actually correct. Recorded so the next session doesn't burn budget re-deriving them or, worse, "fixes" them into being wrong:

1. **Camera break-chance: cap-then-reduce order.** `battle_service.py:660-661` caps at `min(0.9, ...)` *first*, then multiplies by `(1 - break_chance_reduction)`. That ordering is deliberate and player-favourable: the cap-then-reduce path yields a 0.27 worst case for Silver, whereas reduce-then-cap would let a brutal fight climb back to the full 0.9. It also reproduces the documented survival ladder almost exactly (no perk ≈10-17% survival, Silver ≈73% vs the documented 75%). Leave it.
2. **`organic_webbing_active` being a separate flag from `web_fluid_used`** (`patrol_service.py:286-291`). Looks redundant; isn't. Setting `web_fluid_used = True` keeps downstream gating happy while `organic_webbing_active` tells the UI not to print "-1 vial", which would be a lie since no vial was touched. Documented in-code, keep.

## Task 6 — minor, low priority

`battle_service.py:611` hardcodes `"A spider-bot piled on for the extra damage."` inside the **generic** `bonus_damage` effect kind. Any future gadget using `bonus_damage` inherits spider-bot flavor. Move the line into a per-gadget flavor lookup, or leave it and accept the coupling — it is not currently wrong, just fragile.

## Suggested execution order for the next session

1. Read the six unread files listed above (start with `services/gadget_service.py` — that was the interrupted next action — then `services/shop_service.py`, `cogs/shop_cog.py`, `cogs/patrol_cog.py`, `utils/icons.py`).
2. **Ask #4 first**, since it's pure verification and may be zero work: confirm the three gated keys are visible to a non-subscriber across `/shop list`, `/shop browse`, and `/shop buy` autocomplete.
3. Task 1 (one-line number change + doc sync), and **raise the inverted Silver-vs-Gold ladder with the user** — it needs their number, not yours.
4. Task 2 (the headline bug — dataclass field, capture in `finalize_battle`, render in `_render_final`).
5. Task 3, the attribution sweep. Build `_tier_tag()` once, then work down the table. Venom Blast, Sonic Dampener, and the three Biomorphic rolls are the ones that most directly serve "feel premium."
6. Task 4 after confirming with the user.
7. Update `GAME_DESIGN.md` (§6.2, §9.2, §9.3, §9.5) and this file to match whatever actually lands.

## Verification steps (this project's established pattern — don't skip)

- `python -m compileall -q cogs services db utils` — fast syntax gate, was clean at handoff time.
- Load every extension end-to-end: `load_extension` on each entry in `bot.EXTENSIONS` (23 of them) — the standard smoke test used in prior sessions.
- Functional test against the real local dev SQLite DB, which is already seeded with `spider_bots` / `electric_webbing` / `camera_silver`: set a test account to Arachnid tier, buy and equip the Silver camera, run real `resolve_gadget` / `finalize_battle` rounds until a bump fires, and confirm the result card actually renders the bump line with the 🕷️ emoji. Clean up with `wipe_user` afterwards — that's what previous sessions did.
- Repo state at handoff: HEAD is `212800d`, working tree intentionally dirty (user declined committing earlier today — deliberate, not an oversight), 11 modified files, the two icon renames staged, all seven Alembic migrations tracked and applied, `alembic current` head `df532d94924d`. No schema change is needed for any task above — every field added is a dataclass field, not a DB column, so **no new migration**.






---

# HANDOFF EXECUTED — 2026-08-21, later the same day

The handoff above was carried out in full. **Nothing in it was removed** (per instruction) — this section records what actually landed, so read the two together: above is what was planned, below is what shipped and where it differed.

## Result per ask

| # | Ask | Outcome |
|---|---|---|
| 1 | Silver camera → "3 in 5 times" | ✅ `quality_bump_chance` 0.325 → **0.60**. Measured 0.575 and 0.600 across two 400-battle runs. |
| 2 | Show Arachnid branding + "photo upgraded" | ✅ Dedicated **"🕷️ Photo Upgraded"** row on the result card reading `Bronze → **Silver**`. Was previously invisible in every surface. |
| 3 | "Needs to feel premium" | ✅ Attribution sweep: 8 of 9 fixable trigger points now report themselves. Table in GAME_DESIGN.md §9. |
| 4 | Everyone can see Electric Webbing etc. | ✅ **Already correct — zero code changed.** Verified end-to-end; see below. |
| 5 | "Some things may be inconsistent or uncalculated" | ✅ Instinct was right. Found and fixed a real numeric bug plus 7 silent perks. |
| 6 | Sync docs if tokens run out | ✅ GAME_DESIGN.md §6.2 / §9 / §9.2 / §9.3 / §9.5 now describe shipped code; every PENDING/AUDIT block is resolved. |

## What changed, by file

- **`services/patrol_service.py`** — `quality_bump_chance` → 0.60, with the comment rewritten to say *why it can't be tuned* (it's a stated promise, not an interior knob). Added `PatrolResult.biomorphic_cash` and split the non-combat Biomorphic roll out so the amount is recoverable instead of folded into `cash_gained`.
- **`services/battle_service.py`** — added `_symbiote_tag()` and `_gated_gadget_tag()`; five new `BattleReport` fields (`photo_quality_bumped`, `photo_quality_before_bump`, `biomorphic_photo`, `biomorphic_component`, `biomorphic_cash`); `_apply_counter_with_venom_blast` now returns a `CounterOutcome` dataclass instead of a bare string; Biomorphic's bonus photo gets its own bump roll; spider-bot flavor keyed to `spider_bots` instead of firing for any future `bonus_damage` gadget.
- **`cogs/patrol_cog.py`** — the Photo Op group renders the upgrade row and the "Second Shot" row; `_biomorphic_cash_line()` as subtext under Cash on **both** the battle card and the non-combat card; Scavenged notes when the webbing caught it.
- **`GAME_DESIGN.md`** — pending notes converted to fact; new per-trigger attribution table in §9.

## The real bug ask #5 turned up

Sonic Dampener multiplied incoming damage by 1.3 *inside* `_apply_counter_with_venom_blast`, but all four call sites printed the **pre-multiplier** roll. A dampened hit therefore displayed less suit damage than it actually dealt — a paid-tier drawback that was both invisible *and* misreported. Fixed by returning `CounterOutcome.damage` and formatting every readout from it. Verified: 300 forced counters, 0 display-vs-applied mismatches (10 → 13 applied, 13 shown).

## Deliberate judgment calls (revert points, if you disagree)

- **Sonic Dampener's note is suppressed when Venom Blast fires.** Venom Blast negates the hit; printing "that landed harder" beside "the blow never lands" would contradict itself. One `return` in `_apply_counter_with_venom_blast`.
- **Spider Bots / Electric Webbing are tagged 🕷️ on use.** This was flagged in the handoff as needing a taste call. Rationale for tagging: no tier check runs at use time, so what's attributed is *owning* them — the button existing is what the subscription bought. Revert = drop `_gated_gadget_tag(gadget_key)` from the one line in `resolve_gadget`.
- **Biomorphic's second photo now gets its own bump roll** (handoff Task 4). Flagged there as a real Symbiote buff. Shipped because the alternative is indefensible on its face: same fight, same camera, two photos, only one benefits. Revert = one `if` in `finalize_battle`.
- **Copy kept literal against the math.** "Twice as ugly" was rejected for the dampener's +30%, because Venom Blast's "twice as hard" really is 2×.

## Still open — needs your number, not an implementation decision

**Camera Gold's photo-bump chance.** Silver is now 0.60; Gold's originally-locked 45–55% is *below* that and Gold sits on the **higher** (Symbiote) tier, so the ladder is inverted. Gold has to beat 0.60. Nothing in code depends on this yet — there's no `camera_gold` entry in `CAMERA_TIER_STATS` — so it blocks nothing until Gold gets built. Recorded in GAME_DESIGN.md §6.2 (OPEN note) and §9.5.

## Ask #4, verified rather than assumed

`list_shop_items()` applies **no tier filter** (`Item.price.is_not(None)` only); `ARACHNID_GATED_ITEM_KEYS` is read *exclusively* inside `buy_item`. Confirmed live against a seeded DB with a non-subscriber account: all three keys present in the catalog, all three rendered across `/shop list`'s 3 pages carrying "🕷️ Patreon exclusive", none rep-locked, and a purchase attempt correctly refused with the `/patreon link` pointer. The only thing that ever hides a gadget is the ordinary reputation lock every gadget shares (Spider Bots 8, Electric Webbing 14, inside the existing 5/10/15/20 free-gadget ladder).

## Verification actually run

- `python -m compileall -q cogs services db utils` → clean.
- All **23/23** `bot.EXTENSIONS` load.
- Result card rendered for baseline / bump / bump+broke+all-three-Biomorphic. **Baseline output is byte-identical to before** — no perk, no layout change.
- 300 forced counters for Sonic Dampener: tagged every time, correct 1.3× applied, display always matched. Non-subscriber and non-Shocker boss both correctly unaffected.
- ~1,100 real `finalize_battle` calls on a throwaway DB (`SPIDEY_DB_URL=sqlite+aiosqlite:///./_tmp_test.db`, deleted afterwards): bump rate 0.575/0.600 vs 0.60 target; base camera never bumps; no camera banks nothing; `PendingPhoto` rows reconcile exactly (`400 banked + 78 biomorphic = 478`).
- **Gold-cap guard**: 150 gold-tier encounters with a Silver camera → the roll fires but can't go higher, and **0** were falsely flagged as upgraded. `photo_quality_before_bump` is only set on a real tier change, so the card can never print "Gold → Gold".
- `wipe_user` cleanup confirmed: 0 rows left for both test accounts.

**No new migration** — every field added is a dataclass field, not a DB column, exactly as the handoff predicted. Working tree remains intentionally dirty; still not committed.
