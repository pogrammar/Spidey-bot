# Spidey Bot — Game Design Reference

A persistent, always-on-server Discord economy/RPG bot built on pycord, SQLAlchemy (async), and a global (single-server) economy. This document is the canonical reference for every number, formula, and system in the game as of **2026-08-22**. It exists so a future session (or a fresh context window) can pick up exact mechanics without re-deriving them from source. When in doubt, this file should match the code — if it doesn't, the code wins and this file is stale.

Companion file: [CURRENT.md](CURRENT.md) covers in-progress work, open questions, and session-specific state. This file covers locked, implemented mechanics only.

---

## 1. Architecture

- **Framework**: pycord (`discord.Bot`), Components V2 UI (`discord.ui.DesignerView`, `Container`/`Section`/`Thumbnail`) for most panels; a few (`WipeConfirmView` in admin) are deliberately left as classic V1 embeds.
- **DB**: SQLAlchemy async ORM, `AsyncAttrs` base class (lets lazy relationships be awaited via `.awaitable_attrs.x` instead of hitting `MissingGreenlet`). Migrations via Alembic (`alembic/versions/`).
- **DB URL**: `SPIDEY_DB_URL` env var, defaults to local SQLite (`sqlite+aiosqlite:///./spidey.db`).
- **Session pattern**: every cog does `async with async_session() as session:` per command invocation — no long-lived sessions.
- **Cogs** (`cogs/`) are thin — slash commands, views, rendering. **Services** (`services/`) hold all business logic and are what this document mostly describes.
- **Icons**: two parallel systems in `utils/icons.py` — `icon_file(key)` reads a PNG off disk (`assets/icons/{key}.png`) for Components V2 `Thumbnail`/`Section` accessories; `emoji(key)` returns a pre-uploaded Discord application emoji reference string (`<:name:id>`) for inline use in text/buttons. Both return `None` on a miss — a missing icon never errors, it just renders without one. `item_label(key, name)` is the standard "emoji + name" formatter for item names in embeds.
- **First-run hook**: `utils/first_run.py`'s `announce_if_first_time` is pycord's *only* global `before_invoke` hook (pycord allows just one). It does double duty: welcomes brand-new users, and stamps `User.last_active_at` on every command for every existing user — this is what Stealth Mode's inactivity window reads from. Any future "run on every command" need has to ride along on this same function, not add a second hook.
- **Admin auth**: `config.ADMIN_DISCORD_IDS` (comma-separated env var) are root — always trusted, never revocable via command. `AdminUser` DB table holds everyone else granted access via `/admin admins add`, revocable the same way. An empty root set denies everyone (fails closed), not "everyone unchecked."
- **Cooldown bypass**: `services/cooldowns.py` keeps an in-memory (not DB) per-process `_BYPASS_USER_IDS` set, toggled via `/admin bypass` — a dev/testing convenience that resets on bot restart.
- **Web server**: `utils/webapp.py` hosts a shared aiohttp app; `/health` (for UptimeRobot), `/patreon/callback` (OAuth) both register routes on it. Exposed to the internet via an ngrok tunnel (see §9).
- **Bot presence**: `cogs/status_cog.py` rotates a themed `discord.Streaming` line every 60s. Three things about it are load-bearing, all changed 2026-08-25 when the bot was rendering as a plain online presence instead of a streaming one. **(1)** The status must be `Status.online` — Discord only gives a Streaming activity the purple "live" treatment on an online presence; set `idle` or `dnd` and the client renders the ordinary idle/dnd dot and drops the badge, so the activity sits in the payload and is invisible everywhere anyone would look. It was `Status.idle`. **(2)** The presence is *also* passed to `discord.Bot(...)` in `bot.py`, because those two kwargs are what goes into the IDENTIFY payload, and Discord resets a bot's presence to IDENTIFY whenever the gateway session is **invalidated** rather than resumed. Nothing raises when that happens, so the rotation loop cannot know it needs to re-assert — it just keeps sleeping. **(3)** The loop catches `Exception`, not `discord.HTTPException`. `change_presence` never touches REST — it's `Client.change_presence` → `self.ws.change_presence` → `socket.send_str` — so `HTTPException` is one of the few things it *cannot* raise, and that clause matched nothing for as long as it existed, on a loop that dies permanently on anything it doesn't catch. Note that (1) is the only one of the three with a demonstrated causal link to the symptom; (2) and (3) are holes found while tracing it, either of which could produce the same visible result intermittently. None of the three is verifiable without a live client — confirm by looking at Discord after a restart.

---

## 2. Core Identity: the `User` model

One row per Discord user, global (no per-guild state) — `discord_id` is the primary key.

| Field | Default | Notes |
|---|---|---|
| `wallet` | 0 | Liquid cash — spendable, stealable, taxable. |
| `bank` | 0 | Safe cash — immune to `/shakedown`. |
| `bank_capacity` | 5000 | Auto-expands (§7). |
| `reputation_xp` | 0 | Drives `reputation_level` (§3). |
| `suit_integrity` | 100 | 0–100. HP-like in boss fights, cosmetic/tax-triggering elsewhere. |
| `crime_level` | 0 | 0–100, city-wide "heat" meter (§5.4). |
| `boss_clears` | 0 | How many boss gates cleared — drives which boss is next (§5.3). |
| `eviction_meter` | 0 | 0–100, rises from missed rent (§8). |
| `next_rent_due` | now+7d | Weekly rent cycle. |
| `daily_streak` / `daily_longest_streak` | 0 | `/daily` (§10). |
| `daily_last_claimed` | null | |
| `last_active_at` | null | Stamped every command (see Architecture). Powers Stealth Mode. |

Computed properties:
- `reputation_level` = `level_for_xp(reputation_xp)` (§3).
- `reputation_multiplier` = `1 + 0.05 * (reputation_level - 1)` — flat +5% payout per level above 1, applied to Bugle sales and tutoring cash.

---

## 3. Reputation & Leveling

`utils/leveling.py` — deliberately has zero imports from `db`/`services` (avoids a circular import with `db.models.User`).

- `BASE_LEVEL_XP = 100` (cost of level 1→2, matches the old flat-100 system exactly)
- `LEVEL_GROWTH = 1.12` — each level costs 12% more XP than the last (accelerating curve)
- Cumulative XP for level *N* = `sum(round(100 * 1.12^(k-1)) for k in 1..N-1)`
- Precomputed via `bisect` up to level 100 for O(log n) lookup; extends past that on demand (e.g. an admin manually setting a huge XP value) rather than erroring.

**`add_reputation(session, user, xp)`** (`services/economy.py`) is the single chokepoint every XP grant routes through (patrol, tutoring, daily) — this matters because every conditional multiplier below composes here, and this is also where the boss-gate cap is enforced:
1. `gained = max(0, xp)`
2. If `crime_level >= HIGH_CRIME_THRESHOLD (70)`: `gained *= CRIME_XP_PENALTY_MULTIPLIER (0.8)`
3. If Arachnid+ subscriber has chosen the (currently Patreon-disconnected, see §9.4) `"xp"` growth perk: `gained *= ACCELERATED_GROWTH_XP_MULTIPLIER (1.3)`
4. Capped so `reputation_xp` never exceeds `xp_for_level(next_boss_gate_level(user))` — **you cannot XP-grind past an unbeaten boss gate.**
5. Returns the *actual* delta applied (not the raw roll) — every caller displays this real number, not the pre-cap/pre-penalty one.

---

## 4. Boss Gates & Boss Fights

- `BOSS_LEVEL_INTERVAL = 5` — every 5th reputation level is gated.
- `next_boss_gate_level(user) = 5 * (boss_clears + 1)`.
- `at_boss_gate(user)`: XP has reached the gate's floor but the boss guarding it hasn't been beaten.
- Reaching a boss gate on `/patrol` diverts straight to a boss fight (never rolled from the normal weighted table) — but **only if `suit_integrity == 100`**; otherwise you're shown a "Boss Incoming" card and sent to `/workbench repair`.
- **`BOSS_ROSTER`** (`services/patrol_service.py`): 20 named villains, one per gate, in fixed order: Chameleon, Hammerhead, Tombstone, the Shocker, the Scorpion, the Vulture, the Lizard, Rhino, Morbius, Kraven, Electro, Sandman, Hobgoblin, Mysterio, Mister Negative, Doctor Octopus, the Green Goblin, Venom, Carnage, the Sinister Six. Each has 3 hand-written flavor lines.
- Past bracket 20, the roster **cycles** with a `"(Round N)"` suffix (`boss_name()`). `boss_flavor_lines()` still resolves via `(bracket - 1) % 20`.
- **Difficulty freeze**: `boss_difficulty_level(user) = min(next_boss_gate_level(user), MAX_BOSS_DIFFICULTY_LEVEL)` where `MAX_BOSS_DIFFICULTY_LEVEL = 5 * 20 = 100`. Past level 100, leveling and `boss_clears` keep incrementing, but the fight itself stops getting any harder — silent, never announced in UI.
- **Reward**: beating a boss is a flat promotion — `reputation_xp` snaps directly to `xp_for_level(cleared_gate + 1)` (no partial carry-over, bypasses `add_reputation()`'s normal accrual entirely — the Reputation XP booster perk explicitly does *not* apply here), plus `boss_cash_reward = round(rand_range([300, 500]) * difficulty)`.
- Boss fights are always exactly `BOSS_ROUND_COUNT = 10` rounds — no variance (unlike crime tiers), so the fight reads as a real set-piece.
- **Suit integrity is real HP in boss fights** (and only boss fights) — hitting 0% mid-fight is an instant loss (`end_reason = "suit_depleted"`), not cosmetic.
- Boss fights let you use **every gadget you own** (`list_all_owned_gadgets`, via a `Select` dropdown), not just your 2 equipped ones — `resolve_gadget`'s `all_owned=True` flag when `outcome_key == "boss"`.
- Documented (not re-verified this session) win rates at max difficulty (level 100), full 10-round fight: zero gadgets ≈ 0–1% (essentially unwinnable past the first boss); realistic gadget-for-bracket loadout at upgrade level 1 ≈ 17–25%; fully-owned fully-upgraded kit ≈ 70–75%. The very first boss (bracket 1, only 2 gadgets unlocked) sits ≈ 58–60% regardless of upgrade level.

---

## 5. Patrol (`/patrol`) — the core loop

`services/patrol_service.py` + `services/battle_service.py`. Cooldown: `PATROL_COOLDOWN_SECONDS = 30`, but during an active battle the lock is sized to that fight's actual worst case: `round(BATTLE_ROUND_TIMEOUT * max_rounds) + BATTLE_LOCK_MARGIN_SECONDS (40)` (`BATTLE_ROUND_TIMEOUT = 30s`/round) — reset to the normal 30s the instant the fight ends.

### 5.1 Web fluid gate
Every patrol needs 1 `web_fluid_vial` (from `/lab brew`, §11). No vial on hand → pay a cash tax instead: `rand_range([20, 40])`. Arachnid+ subscribers skip this entirely (Organic Webbing at Arachnid, Biomorphic Webbing at Symbiote — same behaviour, two grades of one perk, §9.2) — never touches vial inventory or the tax, 100% of the time, not a chance roll.

### 5.2 Outcome roll
`_roll_patrol_outcome()` — weighted choice over `data/loot_tables.json`'s `"patrol"` table:

| Outcome | Base weight | XP | Notes |
|---|---|---|---|
| `nothing` | 35 | 1–3 | No combat, no cash. |
| `scenic` | 30 | 2–5 | +$10–30 cash, no combat. |
| `crime_bronze` | 15 | 8–15 (pre-difficulty) | Combat. |
| `crime_silver` | 12 | 14–24 | Combat. |
| `crime_gold` | 8 | 20–35 | Combat. |

Combat-tier weights get boosted by `crime_level * CRIME_LEVEL_WEIGHT_BONUS (0.3)` — a maxed 100 crime_level pushes combat odds from a baseline 35% to ≈66%. Arachnid+ subscribers (Combat-Ready Patrols) add a further flat `+15` to each combat entry's weight (≈55% at crime_level 0, ≈72% stacked with maxed crime_level) — deliberately never a 100% guarantee, since even the game's own most extreme state tops out under that.

### 5.3 Difficulty scaling
`difficulty_multiplier(level) = 1 + 0.05 * (level - 1)` (`DIFFICULTY_PER_LEVEL = 0.05`) — level 1 = 1.0x, level 11 = 1.5x, level 21 = 2.0x. Shared by both non-combat XP/cash scaling and combat enemy-stat scaling. Boss fights use the frozen `boss_difficulty_level()` (§4) instead of raw level.

### 5.4 Non-combat resolution (`nothing`/`scenic`)
Resolved instantly, no battle UI. `xp = round(rand_range(outcome_xp) * ally_xp_multiplier * difficulty)`, cash (scenic only) similarly scaled, +Biomorphic Webbing bonus roll for Symbiote+ (§9.3). Crime-level always decays by `rand_range([2, 3])` regardless of outcome (`CRIME_LEVEL_DECAY_RANGE`; sizing rationale in §5.7). A hazard may fire (§5.6) regardless of combat/non-combat.

### 5.5 Combat resolution
`battle_service.py`'s `ENEMY_STATS` dict defines per-tier base stats:

| Tier | base_hp | base_damage | base_hit_chance | photo_quality | component | base_drop_chance |
|---|---|---|---|---|---|---|
| `crime_bronze` | 28 | 4–9 | 0.50 | bronze | spandex_fabric | 0.25 |
| `crime_silver` | 38 | 6–12 | 0.52 | silver | micro_electronics | 0.15 |
| `crime_gold` | 50 | 8–16 | 0.55 | gold | micro_electronics | 0.30 |
| `boss` | 100 | 12–22 | 0.58 | gold | micro_electronics | 0.40 |

Round count for crime tiers is randomly picked per-fight from `[5, 6, 7]` (`ROUND_RANGE`), shown from round 1 so there's never hidden info. `BASELINE_ROUNDS = 3` is the number every other constant was tuned against. HP is corrected for round-count variance: `hp_ratio = 1 + ROUND_HP_SLOPE[tier] * (num_rounds - 3)`, where `ROUND_HP_SLOPE = {bronze: 0.36, silver: 0.315 (linear interpolation, not independently binary-searched), gold: 0.27, boss: 0.0 (fixed round count, no variance to correct)}`. More rounds = less variance (law of large numbers) — these slopes were binary-searched to hold each tier's win rate steady as round count varies.

**Combat difficulty soft cap**: raw `difficulty_multiplier(level)` feeds into a capped curve for win/loss math only:
```
COMBAT_DIFFICULTY_SOFT_CAP_THRESHOLD = 2.5
COMBAT_DIFFICULTY_SOFT_CAP_CEILING = 3.2
ATTACK_DAMAGE_DIFFICULTY_SCALE = 0.90   # enemy HP outpaces attack scaling — winning stays hard forever
```
Below 2.5x, difficulty passes through unchanged. Above it, `combat_difficulty = 2.5 + 0.7 * (1 - exp(-(raw - 2.5) / 0.7))` — asymptotically approaches 3.2x rather than climbing forever. This exists because uncapped scaling made gold crimes hit a real wall around level 25–30 (0% win rate all the way to 100). Only affects enemy HP/damage/hit-chance and player attack scaling — gadget wearout and camera-break odds still key off the **raw**, uncapped difficulty.

Enemy stats from the capped difficulty:
- `enemy_hp = round(base_hp * combat_difficulty * hp_ratio)`
- `enemy_damage_range = [max(1, round(lo*cd)), max(2, round(hi*cd))]`
- `enemy_hit_chance = min(0.8, base_hit_chance + (cd - 1) * 0.1)`
- Player `attack_damage_range`: base `ATTACK_DAMAGE = {bronze:[10,18], silver:[11,20], gold:[12,22], boss:[12,22]}`, scaled by `atk_scale = 1 + (cd-1) * 0.90`.

**Actions per round** (30s timeout, `BATTLE_ROUND_TIMEOUT`):
- **Attack**: `ATTACK_HIT_CHANCE = 0.75`. On hit, deals a roll from `attack_damage_range`; on miss, no damage, enemy gets a free counter roll.
- **Evade**: sets `combo_ready = True` (next Attack is a guaranteed hit at `COMBO_DAMAGE_MULTIPLIER = 1.5x`, `COMBO_HIT_CHANCE = 1.0`). Evade itself deals no damage but reduces incoming counter damage to `EVADE_DAMAGE_MULTIPLIER = 0.25x` if still caught.
- **Gadget** (up to 2 equipped, or all-owned in boss fights): rolls the gadget's effect (§6); on a fumble, silently falls back to a plain attack roll so using a gadget is never strictly worse than clicking Attack.
- Enemy counter: `_enemy_counter()` rolls `enemy_hit_chance`; on hit, damage = `rand_range(enemy_damage_range) * incoming_multiplier`, applied to `total_suit_damage`. A clean kill (enemy HP hits 0) always skips the counter roll for that round.

**Clean win bonus**: `CLEAN_WIN_XP_BONUS = [5, 12]`, `CLEAN_WIN_CASH_BONUS = [10, 25]` — only if the enemy's HP hit 0 (`won_clean`), not on a rounds-exhausted "retreat."

**Entering unprotected** (suit_integrity ≤ 0 at fight start): `entered_unprotected = True`. On finalize, an immediate injury tax `rand_range([20, 50])` (`UNPROTECTED_INJURY_RANGE`) hits the wallet, and camera-break chance gets `+0.15` (`UNPROTECTED_CAMERA_BREAK_BONUS`).

**Camera / photo mechanic**: if a camera is equipped, a `PendingPhoto` is banked at the tier's `photo_quality`. Break chance: `0.06 + 0.05 * hits_taken`, `+0.15` if unprotected, then `* difficulty` (raw, uncapped), capped at 0.9. On break, the equipped camera InventoryItem is deleted outright.

**Scavenge (component drop)**: `drop_chance = min(0.9, base_drop_chance + scavenge_bonus)`. If `suit_integrity < SCAVENGE_URGENCY_THRESHOLD (50)`, urgency adds up to `SCAVENGE_URGENCY_BONUS_MAX (0.25)` more, scaled linearly by how far below 50 you are ("desperate scavenging when you're hurt").

**Crime-level decay** (post-fight): `CRIME_LEVEL_DECAY_RANGE_WIN = [4,6]` on a clean win, `CRIME_LEVEL_DECAY_RANGE_LOSS = [1,3]` otherwise — a real win calms the city more than showing up and losing. Uniform across every crime tier and boss fights alike; boss wins are rare enough that a special case would barely register next to routine crime-tier outcomes. Sizing rationale in §5.7.

### 5.6 Donations & hazards (`data/loot_tables.json`)
Rolled independently on every patrol resolution (combat or not):
- **Donations** (pure upside): `grateful_bystander` 12% chance, +$15–40; `city_council_thanks` 5% chance, +$50–120.
- **Hazards** (pure downside, "Parker Luck"): `medical_bill` 5%, -$30 to -80; `lost_wallet_cash` 3%, -$15 to -40; `mj_birthday_gift` 2%, -$25 to -70; `aunt_may_flowers` 3%, -$15 to -35. The two ally-themed hazards get their chance multiplied by `NEGLECT_HAZARD_MULTIPLIER (2.5)` if that specific ally's happiness is below `LOW_HAPPINESS_THRESHOLD (30)` — neglect makes "Parker Luck" hit harder, thematically.

### 5.7 The `crime_level` dial — one source, one sink
Set 2026-08-22. `crime_level` has exactly **one source (`/tutoring`)** and **one sink (`/patrol`)**. Nothing else moves it — `/ally visit` used to raise it too and deliberately no longer does (§12), because one lever each is what makes the meter legible to a player who never reads a wiki: *tutoring lets the city slide, patrolling puts it back*.

The two sides are sized for **per-minute parity, not per-action parity** — the wall-clock each activity costs is what's being balanced:

| | per action | wall-clock cost | per minute |
|---|---|---|---|
| `/tutoring` rise | `+8..15`, plus `+10` on an unhandled 12% jam | 120s `busy` lock | **≈6.35** |
| `/patrol` drain | `[2,3]` non-combat, `[4,6]` win, `[1,3]` loss | 30s cooldown | **5.5–7.4** (rises with crime, see below) |

So **one tutoring session takes ≈4 patrols to clear**, and a 4:1 patrol-to-tutoring habit sits mid-meter. Sizing these 1:1 per action instead would drain ≈4x faster than it builds and pin the meter at 0 — which would quietly switch off *both* things `crime_level` actually drives (the combat-odds bonus in §5.2 and the ≥70 reputation-XP penalty in §3), turning a mechanic into dead code.

The drain is **self-stabilizing**: higher crime means more combat encounters (§5.2), and combat clears more than a quiet street does, so patrol's drain climbs from ≈5.9/min at crime 0 to ≈6.7/min at crime 90 (at a 60% win rate). Crime converges rather than running away. Verified by simulation across 40/60/80% win rates: at 4 patrols per tutoring the meter settles mid-range, below that it pins high and the XP penalty bites, above it the city stays calm.

---

## 6. Gadgets

`services/gadget_service.py`. Category `"gadget"` items, `MAX_EQUIPPED_GADGETS = 2` (loadout choice — boss fights exempt this cap, see §4). Each has `upgrade_level` 0–3 (`MAX_UPGRADE_LEVEL`), per-copy (durability + upgrade tracked per `InventoryItem` row).

| Gadget | Unlock lvl | Price | Effect kind | base_chance | bonus/level | Notes |
|---|---|---|---|---|---|---|
| Web-Shooters | 1 | $200 | `negate_damage` | 0.25 | +0.05 | Blocks the counter entirely, zero offensive value — bonus/level deliberately kept flat (a higher chance would trade away kill-securing rounds). |
| Web Grabber | 5 | $350 | `bonus_donation` | 0.55 | +0.11 | +$30–70 bonus cash. |
| Ricochet Web | 10 | $500 | `scavenge_boost` | 0.44 | +0.14 | +0.25 scavenge bonus this fight. |
| Upshot | 15 | $650 | `bonus_xp` | 0.30 | +0.145 | +50% of `base_xp` as bonus, and its own hit deals 1.4x damage. |
| Concussion Burst | 20 | $900 | `group_defense` | 0.38 | +0.19 | Deals 1.1x damage AND fully blocks the counter (best defensive gadget). |

Trigger chance = `min(0.9, base_chance + bonus_per_level * upgrade_level)`.

**Upgrade cost**: `round(item.price * 0.6 * next_level)` (`UPGRADE_COST_MULTIPLIER = 0.6`) — e.g. Web-Shooters level 1 = $120, level 2 = $240, level 3 = $360.

**Wearout**: `GADGET_BASE_BREAK_CHANCE = 0.05` per encounter while equipped/used, scaled by `min(0.9, 0.05 * difficulty_multiplier)` — uses the **raw**, uncapped difficulty, not the combat-soft-capped one. Breaking deletes the `InventoryItem` row outright (must be rebought) — or, for a stacked row, decrements `quantity` by one; see §20.3 and the §20.12 note. A **tier-locked** gadget can't break at all (§9.1.2).

Passive contexts (`/tutoring`, `/bugle submit` "jam" events) call `roll_gadget_effect`/`roll_gadget_wearout` with `gadget_key=None`, which picks randomly among whatever's equipped rather than letting the player choose.

### 6.1 Arachnid+ Patreon-exclusive gadgets — Spider Bots & Electric Webbing
Mechanically **ordinary gadgets** — same `"gadget"` category, same shop/equip/upgrade path, same `GADGET_EFFECTS`-driven Select/button flow in battle as the five in the table above (including normal wearout via `roll_gadget_wearout`, since they go through `resolve_gadget` like everything else). The only thing different about them is **the tier gate**: `patreon_service.GATED_ITEM_MIN_RANK = {"spider_bots": TIER_RANK_ARACHNID, "electric_webbing": TIER_RANK_ARACHNID, "camera_silver": TIER_RANK_ARACHNID, "camera_gold": TIER_RANK_SYMBIOTE}` — `buy_item` looks the item's key up in that map and refuses anyone below the mapped rank with a "subscribe and /patreon link" message, and since 2026-08-23 `list_usable_gadgets` re-checks the same map at **use** time so a lapsed pledge switches them off (§9.1.2). Everyone still **sees** them in `/shop list`/`/shop browse` at full price (same visibility as a reputation-locked gadget, plus an inline Patreon-exclusive branding note in `/shop browse`'s selected-item view, badged with every tier that clears the gate per §9's static-catalog rule) — only the purchase itself is blocked.

(Earlier this session these briefly existed as always-on, tier-gated *passive* procs with no equip step at all — the original always-on shape §9.2 used to describe. That was reworked into the ordinary-gadget shape described here per explicit direction, since a passive/no-action version meant no button ever showed up for them in a patrol battle, which read as broken/missing rather than intentional.)

| Gadget | Unlock lvl | Price | GADGET_EFFECTS kind | base_chance | bonus/level | Notes |
|---|---|---|---|---|---|---|
| Spider Bots | 8 | $550 | `bonus_damage` | 0.45 | +0.13 | Normal attack roll + `[5,12]` bonus damage; counter still resolves normally (no defensive component). |
| Electric Webbing | 14 | $750 | `shock_burst` | 0.45 | +0.145 | Normal attack roll + `[8,15]` bonus damage, AND fully blocks the counter for that round (same "no counter" shape as Concussion Burst's `group_defense`). |

Trigger chance formula is the same `min(0.9, base_chance + bonus_per_level * upgrade_level)` as the table above.

**Both were re-rated 2026-08-22 (0.20/+0.12 → the numbers above), per direct player-facing feedback that they "fail WAY too often, it's like they don't even work."** The original rates were chosen by comparison alone (explicitly *below* the free ladder's 0.25–0.55 baseline, never simulation-verified) as an anti-pay-to-win guard. They overshot: at 0.20 base and 0.56 fully upgraded, these were the **least reliable gadgets in the game** outside the deliberately-exempt Web Shooters, and Spider Bots cost **more** than Ricochet Web ($550 vs $500) while firing at **under half** its rate. They also broke §6's own documented convention that `bonus_per_level` scales with unlock level — a flat +0.12 at unlock 8/14, where unlock 10 gets +0.14.

The correction is a change of *which lever* guards the tier, not a removal of the guard: what keeps a paid gadget from being pay-to-win is a modest **effect**, not unreliable **delivery**. Their effects are flat damage adds (`[5,12]`/`[8,15]`) — the weakest kind in the table, below Ricochet Web's scavenge boost and Upshot's +50% XP — and `base_chance` in this codebase is rated against effect *strength*, not unlock level (that's why Web Grabber sits at 0.55 and Upshot at 0.30). So they belong at the reliable end. Resulting ladder, L0→L3: Spider Bots **45/58/71/84%**, Electric Webbing **45/59.5/74/88.5%** — seated just above Ricochet Web's 0.44 base, under Web Grabber's 0.55, and Concussion Burst keeps the 90% ceiling. `MAX_EQUIPPED_GADGETS = 2` means bringing both still costs the entire loadout.

### 6.2 Camera tiers — base camera, Silver-Grade (Arachnid+) and Gold-Grade (Symbiote), all Patreon-exclusive above the base
Implemented as **separate `"tool"` items**, not the `upgrade_level`-on-one-item design §9.5 originally sketched (that design is superseded by this — kept simpler to ship and to show up as a real second entry in `/shop browse`'s Gear section next to the base camera, per explicit direction). `patrol_service.CAMERA_FAMILY_KEYS = ["camera", "camera_silver", "camera_gold"]`, **ordered lowest to highest tier**, and that ordering is load-bearing in three places: `get_equipped_camera`'s best-tier tiebreak, `install_tool`'s retire slice, and `buy_item`'s downgrade refusal all read the list index *as* the tier.

- **Equip exclusivity, and since 2026-08-24 it is destructive**: installing a camera **deletes** every lower-tier body in `CAMERA_FAMILY_KEYS` — `install_tool` slices `CAMERA_FAMILY_KEYS[:incoming_tier]` and drops those rows outright. It used to unequip and keep them, and that was the bug behind "I bought the Gold camera but I still see the Silver one in my inventory": the kept row was meant to be a lapsed-pledge fallback and never functioned as one (`get_equipped_camera` filters on `equipped`, there is no `/equip` command to switch a camera back on, and `get_effective_camera`'s lapse path degrades the *equipped* body's stats rather than falling back to a retired row), while `/inventory` listed it with no annotation at all, because `economy_cog` only marks a tool line when it's equipped or tier-locked. So a dead $1,000 Silver sat next to the Gold looking like working gear. **Deleting was the owner's explicit choice** over labelling, hiding, or refunding it, made with the $1,000 write-off stated. `get_equipped_camera` still searches the whole family and returns the highest-tier equipped row — nothing in the app can produce two equipped bodies any more, but the guarantee is cheap and `scratch/check_camera_gold.py` builds that row by hand to keep asserting it.
- **Because installing is destructive, a downgrade purchase is refused rather than honoured.** `buy_item` compares the incoming key's family index against the best owned one and rejects anything at or below it, naming what it would have cost you; without that, $1,000 of Silver would scrap a $3,000 Gold body. Two locks on the same invariant (this and `install_tool`'s lower-tiers-only slice) and both are load-bearing — the slice keeps a *sideways* install from eating a better body, the refusal keeps the player from asking for it.
- **`get_effective_camera(session, user_id)` is what gameplay reads, not `get_equipped_camera`** (2026-08-23). It returns an `EffectiveCamera` carrying the `row` that's equipped, the `item_key` whose stats actually **apply**, a `tier_locked` flag, and a `.label` (emoji + the `Item.name` read from the database). The two keys differ only when a paid body's pledge has lapsed — see §9.1.2 for that demotion, and the camera-icon rule further down this section for what `.label` is for.
- **`camera_silver`** — $1,000, Arachnid+-gated (`GATED_ITEM_MIN_RANK`), same durability/break-on-delete shape as the base camera. `patrol_service.CAMERA_TIER_STATS["camera_silver"] = {"break_chance_reduction": 0.70, "quality_bump_chance": 0.60}`:
  - **Break-chance reduction**: `battle_service.finalize_battle` multiplies the existing break-chance formula (§5.5) by `(1 - break_chance_reduction)` before rolling.
  - **Quality-bump chance**: `0.60` — **"3 in 5 times"**, set 2026-08-21, replacing the 0.325 that shipped as the midpoint of §9.5's original "30-35%" range. This one is **copy, not a balance knob**: the subscriber is told "3 in 5" outright, so it stays exactly 0.60 and the promise changes before the number does. Independent roll (`patrol_service.bump_photo_quality`) bumping the banked photo up one tier (bronze→silver→gold, capped at gold) *before* it's banked.
  - **Attribution** (implemented 2026-08-21): `BattleReport.photo_quality_bumped` / `.photo_quality_before_bump` carry it out of `finalize_battle`, and `patrol_cog._render_final` renders a dedicated **"🕷️ Photo Upgraded"** row spelling out `Bronze → Silver`. `photo_quality_before_bump` is only set when the tier **actually changed** — on a gold-tier encounter the roll still fires but can't go higher, and the card must not claim an upgrade that didn't happen (verified: 0 false flags in 150 gold encounters).
  - **Biomorphic Webbing's bonus photo** (§9.3) now takes **its own independent bump roll** off the same camera (changed 2026-08-21; it previously banked at the encounter's raw un-bumped quality). Same fight, same camera, two photos — the lens doesn't stop working for the second shot. Rolled separately rather than copying the first photo's result, so the two can legitimately bank at different qualities. This is a small **buff to the Symbiote tier**; one `if` in `finalize_battle` reverts it.
- Camera **Gold** tier shipped 2026-08-22 — base + Silver + Gold all exist. Gold is gated to the **Symbiote** tier (decided 2026-08-21), a step above Silver's Arachnid+ gate. Its bump chance is **0.80 — "4 in 5"** (decided 2026-08-22; this **closed** the inverted-ladder question that was open here). Like Silver's 0.60 it is **copy, not a balance knob** — "4 in 5" deliberately parallels Silver's "3 in 5" so the two read as one ladder, while staying visibly short of a guarantee.

**Handing over a tool now has exactly one implementation: `shop_service.install_tool(session, user_id, item)` (2026-08-24).** It equips the tool, sets full durability, and **deletes** every lower-tier sibling in `CAMERA_FAMILY_KEYS` (see the equip-exclusivity bullet above for why deletion rather than retirement). This was extracted from `buy_item` because it *was* the only implementation and `/admin inventory give-item` wasn't using it — that command called `inventory_service.add_item`, which is a stacking primitive for consumables: it creates the row with `equipped=False` and `durability=NULL` and never touches siblings. So an admin-granted Gold camera landed **completely inert** (`get_effective_camera` reads only `equipped.is_(True)` rows) while the Silver body it was supposed to replace kept taking every photo. Buying Gold from the shop had always worked correctly — the two reports that sounded identical ("Gold Camera doesn't retire the Silver one") turned out to be two different bugs on two different paths, and both are fixed here.
- **Admin grant was the only route into that state**, which is why it survived: cameras are `category: "tool"`, which `market_service.NOT_TRADEABLE_CATEGORIES` blocks, and there is no general `/equip` command. Anything that hands a player a tool from now on goes through `install_tool` — do not reach for `add_item` for a `"tool"`.
- `install_tool` deliberately **does not commit** (both callers have other work in the same transaction), and the admin path **ignores the `quantity` argument**, forcing 1: there is no such thing as owning two of the same tool, and honouring a quantity of 3 would produce a row the rest of the game can't read.
- `inventory_service.add_item` was left alone on purpose. It's correct for what it is — a stacking primitive and a true leaf (imports only `db.models`). The bug was the caller, and "fixing" `add_item` to know about equip state would give a leaf module opinions about tools.



| Tier | Break-chance reduction | Photo-bump chance | Price | Gate |
|---|---|---|---|---|
| `camera` | 0% | 0.00 | $150 | none (starter) |
| `camera_silver` | -70% | 0.60 ("3 in 5") | $1,000 | Arachnid+ |
| `camera_gold` | -85% | 0.80 ("4 in 5") | $3,000 | Symbiote |

Neither knob ever reaches its extreme (-100% / 1.00) at any tier, on purpose: camera-break tension and the value of a genuine gold-crime photo both have to survive the top tier.

**Every surface that mentions a camera shows the camera they actually own (2026-08-23).** Patrol result cards, `/bugle photos` and the photo-bump row previously all drew the base beat-up 35mm and, in one case, hardcoded the words *"the silver lens"* — so a Symbiote subscriber shooting on a $3,000 Gold body was shown someone else's equipment on every single photo. The fix is that the *camera resolves once, server-side*, and the key travels with the result rather than each cog picking an icon:

- `BattleReport.camera_item_key` / `.camera_label` are set by `finalize_battle` from `get_effective_camera` — resolved there because it needs the session and the live tier check, neither of which the cog has.
- `EffectiveCamera.label` reads `Item.name` **from the database** rather than hardcoding a string, which is what stops copy from drifting out of step with the shop and the inventory. The dataclass's `name` default (`"Camera"`) is a floor for a missing items row, not a fallback anyone should hit.
- Cogs render `emoji(report.camera_item_key)` and `report.camera_label`. The rule for new copy: **never write `item_label("camera", ...)` or the word "camera" into a card that a paid body could have taken** — ask the report or `get_effective_camera`.
- The one deliberate exception is `help_cog`'s module-level camera glyph, which is a **category** icon on a static help page, not a claim about anyone's gear. `/bugle submit` likewise keeps `icon_key="money"` — it's about the payout, not the lens.

**Gold's gate is no longer blocked — and Gold is now built (2026-08-22).** Purchase gating used to be a flat `ARACHNID_GATED_ITEM_KEYS` set behind a single `>= TIER_RANK_ARACHNID` check, which had no way to express "Symbiote only" — so Gold couldn't be gated correctly no matter how it was built. That set is now `patreon_service.GATED_ITEM_MIN_RANK`, a per-key `{item_key: min_rank}` map, and `"camera_gold": TIER_RANK_SYMBIOTE` is the whole gate. `GATED_ITEM_KEYS` is derived from the map for the callers that only ask "is this gated at all?", so no second list has to be kept in step. (It moved out of `shop_service` on 2026-08-23, when use-time enforcement gave it a second consumer — a map that both the shop and the battle loop read belongs with the tier logic, not with purchasing.)

Gold ships as four one-line additions and nothing else, because every consumer reads the two tables rather than naming a tier: the `items.json` entry, `CAMERA_GOLD_ITEM_KEY` appended to `patrol_service.CAMERA_FAMILY_KEYS` (ordered low→high, so `get_equipped_camera`'s `max(..., key=CAMERA_FAMILY_KEYS.index)` picks Gold over an also-equipped Silver for free), the `CAMERA_TIER_STATS["camera_gold"]` row, and the rank-map line. **Both silent-failure modes are real and worth knowing:** a camera key missing from `CAMERA_FAMILY_KEYS` still equips but never registers as a camera at all, and one missing from `CAMERA_TIER_STATS` silently falls back to the base camera's `0.0/0.0` — no error either way, just a $3,000 item that quietly does nothing. Verified 2026-08-22 with 39 checks: seeded price/category/durability, icon resolution, both knobs beating Silver without reaching the caps, an Arachnid subscriber refused with `"a Symbiote Patreon perk"` (no `"Arachnid"` anywhere in the message), Symbiote buying both tiers, Silver auto-retired on the Gold purchase, the Gold-over-Silver tiebreak, and 200k-trial Monte Carlo confirming 79.90% measured against the promised 0.80 bump (Silver 60.26% against 0.60).

---

## 7. Economy Primitives

`services/economy.py`.

- **`add_wallet`/`add_bank`**: clamp at 0 (never negative), log a `Transaction` row, return the *actual* delta applied (may be less than requested if clamped) — every caller displays this real number.
- **Bank capacity auto-expansion**: `BANK_UPGRADE_BASE = 2000`, `BANK_UPGRADE_PER_LEVEL = 500`. Increment = `2000 + 500 * (reputation_level - 1)`. Triggers reactively (a deposit that wouldn't fit — expands by exactly enough plus the base increment) and proactively (`BANK_AUTO_UPGRADE_THRESHOLD = 0.9` — bank already ≥90% full after a deposit that *did* fit, expand anyway so the next deposit doesn't immediately hit the ceiling).
- **`Transaction`** table is an audit log — every wallet/bank change ever, `wipe_user` deliberately leaves it intact even when wiping everything else.
- **`wipe_user`**: explicit bulk deletes across `InventoryItem`, `PendingPhoto`, `Cooldown`, `Ally`, `GiftUsage`, `Brew`, `MarketListing` (seller-side) — not relying on ORM cascade, which doesn't reliably fire for every path here.

---

## 8. Apartment / Rent

`services/apartment_service.py`. `RENT_AMOUNT = $400`, `RENT_PERIOD = 7 days`. `/apartment pay` debits the **bank** (not wallet) manually. A background scheduler tick (`process_due_rents`, driven by `cogs/scheduler_cog.py`) auto-debits anyone past `next_rent_due`: if bank covers it, silently paid and `eviction_meter` resets to 0; if not, `eviction_meter += EVICTION_INCREMENT (25)` (capped 100). At `eviction_meter >= 100`, `/workbench repair` is locked out entirely until rent's paid.

---

## 9. Patreon Perks

`services/patreon_service.py` is the single chokepoint: `get_tier_rank(session, discord_id)` returns `TIER_RANK_NONE (0)` / `TIER_RANK_ARACHNID (1)` / `TIER_RANK_SYMBIOTE (2)`, read from `PatreonLink.tier` (whatever string Patreon's API last reported) matched against `config.PATREON_ARACHNID_TIER_NAME`/`PATREON_SYMBIOTE_TIER_NAME` (exact strings from `.env`, case-sensitive, currently `"Arachnid"` confirmed live). **Rank comparisons, not equality** — `tier_rank >= TIER_RANK_ARACHNID` correctly includes Symbiote subscribers for every Arachnid-tier perk, since Symbiote is a strict superset.

`tier_rank` is computed once per `/patrol` invocation and threaded through every downstream call (`begin_patrol`/`begin_boss_patrol` → `PatrolBattleView.tier_rank` → every `resolve_*`/`finalize_battle` call for that fight).

**Attribution rule** (applies to every perk below without exception): whenever a perk actually fires, the tier's emoji (`arachnid`/`symbiote` from `utils/icons.py`) alone must appear inline in the same message — e.g. `"(Enhanced Strength) 🕷️"` appended to the hit line. No accompanying tier-name text ("Arachnid"/"Symbiote") — the emoji alone carries the attribution. Never just a quietly bigger number.

**Two glyphs, not one — the rule sharpened 2026-08-24.** The badge answers *whose subscription is talking*. It does **not** answer *which perk fired*, and it was being asked to do both: with the badge as the only marker, a Venom Blast, a bonus component and a bonus photo all carried byte-identical attribution, so a player could tell that something paid-for had happened but never what. Now every perk that has its own emoji renders **glyph first, badge last**:

```
<:biomorphic_webbing:…> The webbing shook an extra $1,234 loose. <:SYMBIOTE:…>
└─ which perk fired                                              └─ whose subscription
```

The tier's *name* is still never spelled out — that half of the rule is unchanged. Both ends degrade independently: a not-yet-uploaded perk emoji renders the line without a lead, a missing badge renders it without a trail, and neither raises. Four perk glyphs are live (`stealth_mode`, `venom_blast`, `organic_webbing`, `biomorphic_webbing`); perks with no glyph of their own (the combat override, the Symbiote repair reskin) correctly render the badge alone rather than borrowing another perk's icon. Verified by `scratch/check_symbiote_icons.py`, which renders each line and asserts the glyph leads, the badge trails, each appears exactly once, and no *foreign* perk glyph or tier badge has leaked in.

**One deliberate inversion, in `/shakedown` only.** Stealth Mode is the one perk whose beneficiary isn't the person reading the message — the thief gets bounced by the *target's* subscription. So that badge shows `result.target_tier_rank` (returned by `attempt_shakedown` rather than re-read in the cog, so it's the exact rank the gate judged). Without it the thief reads an unexplained refusal that still burned their cooldown, which looks like a bug rather than a perk. Do not "correct" this to `ctx.author`'s tier; the thief's tier has no bearing on whether the perk fires.

**Which badge, though — clarified 2026-08-23.** Because the copy never names a tier, the badge *is* the entire signal, and there are two different questions it can answer. The answer depends on which:

| | Question it answers | Helper | Shows |
|---|---|---|---|
| **Live attribution** — a perk firing, a drawback being explained to the person bearing it | "whose subscription is talking?" | `patreon_service.tier_badge(tier_rank)` | the **one** badge for the tier the player **has** |
| **Static catalog** — shop listings, `/patreon perks` checklists, help-page category glyphs | "who is this available to?" | `patreon_service.tier_requirement_badges(min_rank)` | **every** badge that clears the gate |

Getting this backwards is what the fix corrected: an Arachnid+ perk firing for a **Symbiote** subscriber used to show the Arachnid badge, which tells them their own perk belongs to somebody else's tier. Conversely a shop entry for an Arachnid+ item showing only the Arachnid badge tells a Symbiote subscriber they can't buy something they can. Same table, two rows, opposite answers — and both were wrong in the same direction (assuming the gate's tier and the player's tier are the same thing, which for a superset tier they never are).

Helpers: `battle_service._tier_tag(tier_rank)` (trails), `._perk_glyph(icon_key)` (leads, space-suffixed) and `._gated_gadget_tag(key, tier_rank)`. All three return `""` below Arachnid / when the emoji is missing, rather than raising, so a not-yet-uploaded emoji degrades to a partly-tagged line instead of a broken fight — which is why call sites interpolate them unguarded. (`_tier_tag` replaced the fixed-tier `_arachnid_tag()`/`_symbiote_tag()` pair — a helper that hardcodes its own tier *cannot* implement the rule above.) Outside `battle_service` the same two jobs are done inline off `patreon_service.tier_badge()` and `utils.icons.emoji()`; `biomorphic_service.scavenge_subtext()` is the shared version for the three non-combat activities, so they can't drift apart.

**Audit 2026-08-21 — the rule was aspirational; it is now implemented.** An audit found that of ten perk trigger points, only Enhanced Strength complied. Every gap below except the last was closed the same day:

| Perk | Where | Status |
|---|---|---|
| Enhanced Strength | `battle_service.resolve_attack` | ✅ was already compliant — the reference pattern |
| Silver camera photo bump | `finalize_battle` → `patrol_cog._render_final` | ✅ fixed — dedicated "Photo Upgraded" row, §6.2 |
| Venom Blast | `resolve_venom_blast` | ✅ fixed — badge-tagged since 2026-08-21, and **carries its own `venom_blast` glyph as of 2026-08-24**. Moved out of `_apply_counter_with_venom_blast` into its own resolver the same day, when the mechanic became a button (§9.3) — the line is now the whole log entry for the round rather than a fragment prepended to a counter, so it owns its own lead |
| ~~Sonic Dampener~~ | ~~`_apply_counter_with_venom_blast`~~ | ⛔ **mechanic deleted 2026-08-24** — see §9.3. It was fixed here 2026-08-21 (it had emitted **no line at all** while silently multiplying incoming damage by 1.3) and then removed outright, so the row is kept only to explain the gap in the numbering. The helper named here no longer exists either; it was deleted later the same day (see below) |
| The suit overrides you | `resolve_evade`, `resolve_gadget` | ✅ compliant from the start — `SYMBIOTE_OVERRIDE_LINES` + `_tier_tag()`. A drawback, so the line is doing double duty: without it, an ignored Dodge reads as a bug rather than as the tier's stated cost. **No perk glyph exists for it**, so the badge correctly stands alone rather than borrowing another perk's icon |
| Biomorphic cash | `finalize_battle`, `finish_noncombat_patrol` | ✅ fixed — `biomorphic_cash` on both `BattleReport` and `PatrolResult`, rendered as subtext under Cash |
| Biomorphic component | `finalize_battle` | ✅ fixed — was byte-identical to a base-game drop; now a subtext line under Scavenged |
| Biomorphic bonus photo | `finalize_battle` | ✅ fixed — "Second Shot" row, and it now gets its own bump roll (§6.2) |
| Biomorphic ambient scavenge | `tutoring`/`ally`/`bugle` services | ✅ compliant on arrival (2026-08-24) — `biomorphic_service.scavenge_subtext()`, one shared renderer for all three activities |
| Stealth Mode | `pvp_cog.shakedown` | ✅ **fixed 2026-08-24, finished 2026-08-25** — rendered with *no badge at all*, the last remaining gap. Now glyph-led and badge-trailed off the **target's** rank; see the inversion note above. The 2026-08-25 pass added the third attribution mark and the missing surface: the branch is a V2 `StaticView` carrying the **target's** accent and the `stealth_mode` thumbnail instead of an `error_embed` titled "Parker Luck.", and `/patreon perks` now tells the *subscriber* how many attempts it blocked (§9.3). It is also the only perk that renders **twice** — the thief's panel inverts the attribution, and the target's DM (`_stealth_dm_view`) carries the same three marks in their normal orientation, since there the reader is the payer |
| Symbiote repair reskin | `suit_service`, `suit_cog` | ✅ compliant on arrival (2026-08-24) — the panel intro line carries the badge. No glyph: this isn't one perk firing, it's a whole panel reading differently |
| Spider Bots / Electric Webbing | `resolve_gadget` | ✅ tagged via `_gated_gadget_tag()`. Originally a **taste call** rather than a rule requirement, on the grounds that no tier check ran at use time — what was being attributed was *owning* them. **As of 2026-08-23 it's a rule requirement**: `list_usable_gadgets` re-checks the tier at use time, so if the tag renders at all, the pledge is current (§9.1.2) |
| Combat-Ready Patrols | `patrol_service._roll_patrol_outcome` | ⚠️ **accepted gap** — a weight bonus applied *before* any outcome exists, so there is no moment to attribute. Naming it would mean claiming a specific encounter was caused by the perk, which isn't knowable |

The sweep also fixed a real numeric bug it surfaced: Sonic Dampener multiplied the counter *inside* `_apply_counter_with_venom_blast` while all four call sites printed the **pre-multiplier** roll, so a dampened hit displayed less suit damage than it actually dealt. The fix was a `CounterOutcome` dataclass carrying the actually-applied `damage`, with every call site formatting from that.

**Both the helper and `CounterOutcome` are gone as of 2026-08-24**, and the bug they fixed cannot come back in the same shape. The dampener's deletion removed the only thing that modified a counter after it was rolled, and Venom Blast becoming a button removed the only thing that replaced one: the blast no longer intercepts a hit, so there is no longer a path where the damage applied differs from the damage printed. All four call sites now read `counter = _enemy_counter(state)`, `_apply_counter(state, counter)`, and format `counter` — one value, applied and displayed. Anything reintroduced that adjusts a counter mid-flight has to bring back a return channel of its own; don't assume a bare `int` is safe because it is today.

### 9.1 OAuth linking flow
`/patreon link` → `build_authorize_url()` generates a random `state` token (kept in an in-memory `_pending` dict, 10-min TTL — a bot restart mid-link just means re-running the command, an acceptable tradeoff to avoid a throwaway DB table) → sent as a DM (or inline if already in DMs, or inline as fallback if DMs are closed) with a link-style button → Patreon redirects to `/patreon/callback` (served by the shared aiohttp app) → `handle_callback()` exchanges the code, fetches identity **with `fields[tier]=title`** (a v2 API quirk: without this explicit field request, a resource is stripped to just `type`+`id`, and tier detection silently breaks — this was a real bug found and fixed 2026-08-18) → upserts `PatreonLink` → sends a tier-specific welcome DM (`ARACHNID_INTRO`/`SYMBIOTE_INTRO`/`NO_PLEDGE_INTRO` in `patreon_cog.py`).

`/patreon status` shows the raw Patreon-reported tier string plus the resolved perk-tier label. `/patreon unlink` deletes the DB row only — does not touch the actual Patreon pledge.

`/patreon subscribe` (added 2026-08-25) is the front of the funnel — the surface that existed nowhere before it. Every other command in the group reports on a link you already have, and the only pointers a non-subscriber ever got said "subscribe" without saying where, because no patreon.com creator URL existed anywhere in the repo. `patreon_service.PATREON_PAGE_URL` (`https://www.patreon.com/c/spideybotdiscord`, the canonical `/c/` creator form) now sits beside the three OAuth endpoints so every patreon.com URL is in one place, and `patreon_cog.SubscribeView` renders a V2 card with a link button to it. Four things about it are deliberate and shouldn't be "tidied":

- **One line per perk, and the long copy is deliberately NOT reused.** `/patreon perks` is read by somebody who has already paid and wants the detail; this is read by somebody deciding whether to, and a wall of paragraphs is a wall they bounce off (owner's call, 2026-08-25 — the card shipped that morning with the long constants and was cut the same day, 2697 → 1416 characters). So `PITCH_ARACHNID`/`PITCH_ARACHNID_COST`/`PITCH_SYMBIOTE`/`PITCH_SYMBIOTE_COST` are separate, terse lines: bold perk name, plain payoff, one line each. That reintroduces the drift risk the shared-constant rule exists to kill, so two things hold the two descriptions together — **every number is interpolated from the same constant the long line uses** (`ENHANCED_STRENGTH_DAMAGE_BONUS`, `ARACHNID_ALLY_DECAY_INCREASE`), so no rate change can leave a stale figure here; and **`scratch/check_patreon_subscribe.py` asserts count parity** against the long lists, so adding a perk to `ARACHNID_PERKS` or `_symbiote_perks` without writing its one-liner fails a check instead of silently shipping a tier that undersells itself. The same script caps line length (with custom-emoji markup stripped) and rejects mid-line sentence breaks, so pasting the long copy back in trips it. That cap is a paragraph-regression ceiling, not a terseness target — the owner hand-lengthened two Symbiote lines the same day and they're meant to stay long, so don't tighten it back to hug the current longest line. Each tier's **cost** still sits inside that tier's own block, so the upside can't be read without the downside.
- **`_eyebrow()` exists because the card has a two-level hierarchy and `_add_group` models one.** Handing `_add_group` three fields per tier renders the tier name as the small grey eyebrow and "What it costs you" as bold above it — the sub-label shouting over the product. So each tier is *one merged field* (empty field name, which takes `_add_group`'s bold branch) with its categories nested inside at `-# UPPER` weight. That keeps the project's existing vocabulary (bold = this block's own label, `-# UPPER` = a category tag within it) instead of inventing a third style for one card, and it dropped the component count from 22 to 12.
- **This is the only surface allowed to spell tier names out in text.** §9's emoji-only rule governs *attribution* ("which subscription made this happen"), where the name adds nothing the badge doesn't. A catalog is the inverse: you cannot sell somebody a tier you won't name, and the name has to match Patreon's own checkout. Names come from `TIER_RANK_LABELS`, not typed.
- **`_tier_gear` matches rank exactly, not `>=`** — the one other place in §9 where that's correct (cf. `accent_for_rank`). It answers "which tier *introduces* this item", so the Silver camera appears under Arachnid only; `>=` would repeat it under Symbiote, reading as two unlocks and padding the higher tier with the lower one's list. Safe as equality because `GATED_ITEM_MIN_RANK`'s values can only be one of the two paid ranks, both of which are pitched.

It is also the group's **only non-ephemeral command** — it contains nothing about the caller, and one person asking is how the rest of a channel learns the page exists. It runs no query at all: the card is identical for a subscriber, a non-subscriber and a lapsed one, and the accent bar comes from the ambient context for free. The two places that used to say "subscribe" with no destination now name it: `shop_service.buy_item`'s gated-item refusal, and `_perk_sections`' rank-0 line (which serves both an unlinked `/patreon perks` and the `NO_PLEDGE_INTRO` welcome, so it names `subscribe` *and* `link` and lets the reader pick).

`/patreon perks` is the richer companion to `/patreon status` — a plain-English checklist of every perk actually active for the caller right now (built from `ARACHNID_PERKS`/`SYMBIOTE_PERKS_STATIC` plus the `ORGANIC_WEBBING_LINE`/`BIOMORPHIC_WEBBING_LINE` pair in `patreon_cog.py`, gated by the same `tier_rank` comparisons as everything else), plus an ownership check (owned vs. not-owned, per `InventoryItem`) for the gated purchasable items since a tier only unlocks the *ability* to buy those, it doesn't grant them. That checklist iterates `patreon_service.GATED_ITEM_MIN_RANK` directly and filters to `tier_rank >= min_rank`, so a newly gated item appears without a second edit and an Arachnid subscriber is never shown a Symbiote-only item under a heading that reads "Yours to Buy" (`patreon_cog.GATED_ITEM_LABELS` supplies only the wording, and falls back to the raw key if someone forgets to add one). Those same lists are the single source for the post-link welcome DM, so the two surfaces cannot drift.

**One line on that checklist is not a constant, and that's deliberate.** Stealth Mode's is built by `_stealth_mode_line(protections)` and appended to `SYMBIOTE_PERKS_STATIC` by `_symbiote_perks()`, because it carries a per-subscriber count (§9.3). The `stealth_protections` argument threads through `_perk_sections` and `build_welcome_view` with a **default of 0**, which renders the line exactly as it read before the count existed — so a caller with no reason to spend a query can omit it, and every existing positional two-arg call still works. `_stealth_protections()` holds the tier check, so neither call site can forget it and count for a card that has no Stealth Mode line to put the answer on.

#### 9.1.1 Tier re-checking — implemented 2026-08-23
**The hole this closed:** a tier was written once at link time and *never read again*. Cancelling a pledge kept every perk forever, and the `PatreonLink` docstring claimed a periodic re-check that did not exist. `refresh_stale_links()` + `SchedulerCog.patreon_tick` are that re-check.

- **The one rule: only a successful identity read may rewrite a stored tier.** Every other path leaves it exactly as it was. This is not defensive coding, it's the actual product decision — a Patreon 503 must never take perks away from someone who is paying. The direction of error is deliberate: a lapsed pledge keeps its perks for up to one extra `REFRESH_STALE_AFTER` window, which is the correct side to be wrong on.
- **Why a `RefreshOutcome` dataclass and not a `str | None` return.** The whole contract hinges on the difference between *"they have no pledge"* and *"we couldn't tell"* — both of which are `tier=None`. `reached_patreon` is what separates them, and a bare tier return literally cannot express it. That distinction is the fail-open guarantee; everything else is bookkeeping.
- **Cadence.** `PATREON_TICK_INTERVAL_MINUTES = 15` drains the queue, `REFRESH_STALE_AFTER = 6h` decides what's *in* it, `REFRESH_BATCH_SIZE = 25` caps one tick. These are three separate knobs on purpose: the tick is drain speed, the staleness window is per-link re-read frequency. 25 × 96 ticks/day = 2,400 checks, which re-reads every subscriber many times over at any realistic count while staying far under Patreon's rate limits. Rows are taken **oldest-`last_checked_at` first**, so nothing can be starved.
- **A failed check still stamps `last_checked_at`.** Without that, a permanently-failing link sits at the head of the queue and gets retried every single tick forever, hammering Patreon and starving everyone behind it. The tier is untouched, so the fail-open guarantee is unaffected — only the retry cadence changes. The one exception is missing local credentials (`PATREON_CLIENT_ID`/`SECRET` unset), which doesn't stamp at all: that's a misconfiguration rather than an attempt, and the queue should still be in its true order once it's fixed.
- **Transient vs. dead, and why "dead" is allowed to remove perks.** `_TransientPatreonError` (network error, timeout, 5xx, 429, malformed payload) fails open. `_DeadLinkError` — a **4xx on the refresh grant**, i.e. Patreon authoritatively saying this grant is gone — does not, and clears the tier. Reasoning: an outage is temporary and retrying fixes it; a revoked grant can *never* be verified again by any number of retries, and continuing to grant paid perks off a credential we can't check is the exact hole this loop exists to close. The row is kept rather than deleted so `/patreon status` still shows something and one `/patreon link` repairs it.
- **The rotated-token trap, and the guard against it.** Patreon issues a *new* refresh token every time one is used. So the new pair is committed **immediately** on a successful refresh, before the identity call — if the process died between the two, the stored token would already be spent, the next cycle would read `invalid_grant`, and the loop would declare a **paying subscriber** dead. Relatedly, a refresh response with no `refresh_token` field keeps the old one rather than blanking it, which would manufacture the same dead link. Both cases are covered in `scratch/check_patreon_refresh.py`.
- **A 401 on the *retry* stays fail-open.** If Patreon rejects a token it minted seconds earlier, that's Patreon's problem, not a dead grant — so it's reclassified transient rather than falling through to the dead-link branch.
- **The tick catches broadly on purpose.** `tasks.loop` kills itself permanently after one uncaught exception (see the note in `cogs/status_cog.py`'s `rotate_status`, which is where that was learned the hard way), and *this* loop going quietly dead reopens the very hole it closes. The service already fails open per-link, so the cog's `except Exception` is only for the unexpected — a DB error, or something malformed getting past the service.
- **Deliberately NOT included: notifying the user.** A tier change is logged (at `warning` level when it costs someone perks — the only record that a background job removed something a player paid for, and the first thing to check if they ask why), but no DM is sent. The existing welcome copy is wrong for a lapse (`NO_PLEDGE_INTRO` reads as a greeting), and lapse-notification copy is its own decision with its own tone question. Log first, decide the copy later.
- **`patreon_user_id` is never rewritten by the loop.** If it ever differs, the row belongs to a different Patreon account than it did — `/patreon link` handles that properly, and silently reassigning it in a background job would hide it.
- **Verified:** `scratch/check_patreon_refresh.py`, 74 checks against a fake `aiohttp.ClientSession` and a throwaway DB. Covers lapse, downgrade, upgrade, no-change, five outage shapes (500/503/429/connection error/timeout), 5xx and malformed responses on the refresh grant, missing local credentials, the refresh-and-retry happy path, refresh-token rotation and retention, all three dead-link statuses, the 401-on-retry reclassification, batch ordering and cap, an empty table, and end-to-end that `get_tier_rank()` actually reflects a refreshed downgrade (and that a stored `growth_perk_choice` goes inert with it).


#### 9.1.2 Revocation — perks stop working when the money stops, implemented 2026-08-23
§9.1.1 closed the *detection* hole (a stored tier that was never re-read). This closes the *enforcement* hole underneath it: two perks were gated **only at purchase**, so once bought they worked forever regardless of tier. Detecting a lapse accomplishes nothing if nothing reads the result.

**Both halves of "they stopped paying" are one check.** A cancelled pledge clears the stored tier (`refresh_stale_links`) and `/patreon unlink` deletes the row outright, so `get_tier_rank` returns `TIER_RANK_NONE` either way. Nothing anywhere needs to distinguish them — reading the **live** rank at use time, never anything cached at purchase, is the entire mechanism.

`patreon_service.locked_item_keys(session, discord_id, item_keys) -> frozenset[str]` is the shared query: which of these keys does the caller own but no longer clear the gate for. It **returns empty without touching the database** when nothing in `item_keys` is gated, which is the normal case for the overwhelming majority of players. Four near-duplicate rank comparisons collapsed onto it.

**The two enforcement points:**

| Item type | Enforced in | Behaviour while locked |
|---|---|---|
| Gadgets (`spider_bots`, `electric_webbing`) | `gadget_service.list_usable_gadgets` | Owned, equipped, listed — and inert. No battle button, no proc, no break |
| Cameras (`camera_silver`, `camera_gold`) | `patrol_service.get_effective_camera` | Still takes photos, at the **base** body's stats (`0.0 / 0.0`) |

**Inert, not deleted — and the gadget list stays honest.** A locked gadget keeps its row, its slot and its `upgrade_level`. Two reasons, and the second is the load-bearing one: resubscribing switches it straight back on rather than asking someone to re-buy gear they paid real money for; and hiding it from `list_equipped_gadgets` would **free a slot**, let them equip a third gadget, and hand them three live gadgets the moment they resubscribed (`equip_gadget` counts against `MAX_EQUIPPED_GADGETS` off that list, so it has to keep reporting what's genuinely equipped). Hence the split: `list_equipped_gadgets`/`list_all_owned_gadgets` answer *what's owned and equipped*, `list_usable_gadgets` answers *what may fire*. **Every gameplay path goes through the latter** — `roll_gadget_effect`, `roll_gadget_wearout`, the patrol battle's button build, and `daily_service._grant_free_gadget_upgrade`.

**The gadget/camera asymmetry is deliberate, not an oversight.** A locked gadget **can't break**; a locked camera **can**, at the base body's rate. The gadget does nothing while locked, so breaking it would make lapsing *destroy* gear rather than pause it. The camera is still earning photos, so it carries the risk that comes with that — and making it invulnerable while locked would turn lapsing into its own perk. (Mechanically this falls out for free: `camera_tier_stats()` of the fallback key has no `break_chance_reduction`, so the demoted body rolls at exactly the base rate.)

**Revocation has to be visible, or it reads as a bug.** A gadget that silently never fires is indistinguishable from a broken one, and the button-suppression above makes it *more* invisible, not less. So every surface that shows owned gear can label it, via the one `locked_item_keys` call:
- `/gadget status` — per-line `-# Inactive` subtext plus a footer.
- `/gadget panel` — a `Status` stat field, and **Upgrade disabled**. Equip/Unequip stay live: freeing the slot is the one useful thing to do with an inactive gadget.
- `/inventory` — `— Inactive` on the line (§19.5). The **only** place a revoked camera is explained; cameras have no status command of their own.
- Autocomplete and Select descriptions — `"inactive (Patreon tier lapsed)"`, checked *before* the equipped/durability branches so the more important fact wins the limited space.

**Spending on inert gear is blocked at the service, not just greyed out in the UI.** `upgrade_gadget` rejects a locked gadget with its own message rather than reusing the equipped-list filter — filtering would emit *"that gadget isn't equipped"* about a gadget sitting visibly in a slot, and this is a **cash spend**: charging for an upgrade to something that can't fire is the worst version of the bug. `daily_service`'s free streak upgrade routes through `list_usable_gadgets` for the same reason — the reward *names* the gadget it upgraded, and naming an inert one reads as the reward having been wasted.

**Verified:** `scratch/check_revocation.py`, 51 checks against a throwaway DB. Covers lapse and unlink for both item types, gated-vs-free isolation, that a locked gadget survives a certain-to-fire break roll, the upgrade refusal taking no money, resubscription restoring both, Gold demoting to base stats while the Gold row stays owned, `locked_item_keys` short-circuiting on an all-free list, both badge helpers, and the `/inventory` fold/label changes.


1. **Organic Webbing** — deterministic, not a chance roll: Arachnid+ patrols never touch `web_fluid_vial` inventory or the no-fluid cash tax, 100% of the time. `/lab brew` output is unaffected/still sellable. **This is the lower grade of a two-grade perk, not a standalone one** — at Symbiote it becomes Biomorphic Webbing (§9.3), which does everything it does plus more. See "The webbing ladder" below.
2. **Enhanced Strength** — `+30%` Attack damage (`ENHANCED_STRENGTH_DAMAGE_BONUS`), **crime-tier patrols only** (excluded from boss fights — that difficulty curve is tuned around full-strength numbers).
3. **Combat-Ready Patrols** — flat `+15` weight bonus to each crime-tier patrol-roll entry (`COMBAT_READY_PATROLS_WEIGHT_BONUS`).
4. **Drawback (the only one)**: ally happiness decays `+50%` faster (`ARACHNID_ALLY_DECAY_INCREASE`), always-on, no opt-in — full drain 24h → 16h (§12). **Narrative framing (set 2026-08-22, and it is the load-bearing one):** the allies aren't neglected and they aren't needy — they are deliberately holding onto Peter Parker, because the further the bond takes him the less of him comes back. Visiting is what keeps Peter *Peter* rather than letting the thing underneath off its leash. This framing is what makes the drawback scale-with-tier read as *story* rather than as a tax: Symbiote inherits it (§9.3) and there the monster is literal, so the same mechanic lands harder without a second number. Do not re-frame this as "your allies watch you more closely" (the pre-2026-08-22 wording) — that read as surveillance and gave the cost no stakes. The copy lives in **`/ally check`**'s footer (not `/ally visit`, which shows no tier footer), and per §9's live-attribution rule it is badged `tier_badge(tier_rank)` — the **viewer's own** tier, corrected 2026-08-23. Symbiote inherits this cost, and since the copy never names a tier, an Arachnid badge here told a Symbiote subscriber their own happiness drain belonged to somebody else's tier.

**The webbing ladder — Organic is Biomorphic's prerequisite (set 2026-08-22).** Organic Webbing and Biomorphic Webbing are **one perk with two grades**, not two perks that stack. Biomorphic is what Organic *grows into*: it does everything Organic does (vial-free patrols, no cash tax) and adds the four bonus rolls in §9.3. The prerequisite needs no enforcement code — the rank ladder already guarantees it, since every `>= TIER_RANK_ARACHNID` check passes for Symbiote too.

Mechanically this changed nothing (a Symbiote subscriber always had both behaviours); what it changed is **attribution**, which was actively wrong before:
- `_perk_sections()` (`patreon_cog.py`) listed both as **peer bullets** on a Symbiote card, advertising the same vial-free patrol twice under two names and reading like Biomorphic was a sidegrade beside Organic. Now exactly one webbing line renders per card — `ORGANIC_WEBBING_LINE` or `BIOMORPHIC_WEBBING_LINE`, never both — and it leads the list, since it's the perk that touches every single patrol.
- Both patrol-card sites (`_noncombat_view`'s Web Fluid field and the battle-start banner) hardcoded the **Arachnid** emoji and the literal text "Organic Webbing", so a Symbiote subscriber was told a *lower tier's* perk was what saved them the vial. Both now route through `patrol_cog._webbing_note()`, driven by `PatrolStart.biomorphic_webbing_active` / `PatrolResult.biomorphic_webbing_active` (`tier_rank >= TIER_RANK_SYMBIOTE`, set alongside `organic_webbing_active` in `_begin`). That flag is **naming-only** — it can never be true unless `organic_webbing_active` is, and it changes no behaviour, only which perk gets the credit and therefore which tier emoji appears.

**Electric Webbing and Spider Bots moved out of this list** (were originally a free always-on part of this tier, changed 2026-08-20): they're now Arachnid+-gated *purchasable* gadgets — see §6.1 — rather than something every Arachnid+ subscriber gets automatically. Their proc rates were re-tuned 2026-08-22 (0.20 → 0.45 base); the original below-baseline numbers and why they were wrong are recorded in §6.1.

**Visibility of the gated items — verified 2026-08-21, no work needed.** All four (`spider_bots`, `electric_webbing`, `camera_silver`, `camera_gold`) are visible to **everyone**, subscriber or not, by design: `shop_service.list_shop_items()` applies **no tier filter at all** (only `Item.price.is_not(None)`), and `GATED_ITEM_MIN_RANK` is consulted *exclusively* inside `buy_item`. `shop_cog._patreon_branding()` marks them "🕷️ Patreon exclusive" in `/shop list` and `/shop browse` — the badge is the emoji of whichever tier gates that specific item, so a Symbiote-only item wears the Symbiote emoji rather than misreporting an Arachnid pledge as sufficient — and a non-subscriber's purchase attempt fails with a message pointing at `/patreon link`. The only thing that hides a gadget from `/shop browse` and the `/shop buy` autocomplete is the ordinary reputation lock (`_is_locked`), which applies identically to every gadget — Spider Bots `unlock_level 8`, Electric Webbing `14`, sitting inside the existing free-gadget ladder (5/10/15/20). Nothing tier-related suppresses them anywhere.

### 9.3 Symbiote tier (rank 2) — implemented, live — includes everything above plus:
1. **Venom Blast** — boss fights only, once per fight, and since 2026-08-24 **deployed by the player rather than fired by the suit**. A third button rides in the action row beside Attack and Evade for the whole fight; while suit integrity is above `VENOM_BLAST_TRIGGER_INTEGRITY` (= 25%) it is `disabled` and labelled "Venom Blast — Not Charged", at or below it becomes plain "Venom Blast" and pressable, and after use it stays in the row reading "Venom Blast — Spent" (kept rather than dropped so the row can't reflow under a player mid-click and turn their Evade into something else). A press deals `VENOM_BLAST_DAMAGE_MULTIPLIER` (= 2) × a normal attack roll and the enemy does not counter that round. Resolved by `battle_service.resolve_venom_blast()` — a peer of `resolve_attack` / `resolve_evade` / `resolve_gadget`, not a hook inside one of them — and rendered by `patrol_cog.VenomBlastButton`. Arming is a pure predicate, `venom_blast_ready(state, tier_rank)`, so the button's `disabled`, the resolver's own guard and the simulator all ask one question in one place.
   - **The button is present from round 1 for anyone who has it, and absent entirely for anyone who doesn't.** It renders only for `tier_rank >= TIER_RANK_SYMBIOTE` in a **boss** fight, so the row is two buttons wide for everyone else and nothing moved for them; a permanently-dead button is mid-fight advertising, and `/patreon perks` is where that belongs. But for a subscriber it appears greyed rather than appearing on arming, because **a button that only shows up once the condition is met can't teach anyone the condition exists** — and a `-#` subtext line under the row spells the bar out ("The bond charges at 25% suit integrity or lower" → "The bond is charged. One shot, this fight only."), disappearing once the charge is spent. The subtext explains the bar and never nudges the press.
   - **It is exempt from the suit override** (item 5). A once-per-fight charge the player deliberately spent must not be hijacked into an ordinary attack — that reads as the tier eating its own perk. `resolve_venom_blast` never consults `SYMBIOTE_OVERRIDE_CHANCE`, and this is **asserted, not assumed**: `scratch/check_override.py` forces the rate to 1.0 and presses 20,000 armed blasts — zero hijacks, 20,000 lands.
   - **The arming bar moved from 0% to 25%, and under a button it is a plain buff — which reverses what this section said the day before.** At 0 the bar means "the hit that would have ended the fight," and for a *button* that is unreachable: the fight is over before there's anything to press, so bar 0% is simply the perk switched off (measurably — 0.0% arm rate at every bracket). The old non-monotonicity — the charge spending itself on a survivable hit and leaving a lethal one later unabsorbed — was **a property of automatic interception**, and it cannot exist when the player picks the round. Re-swept over `{0,15,25,35,50,65,80}` after the change (`scratch/check_venom_trigger.py`, 4 seeds × 60k boss fights per cell, greedy press-on-arm policy; the coarser `{0,15,25,35,50}` grid in `scratch/combat_sim.py venom` agrees within 0.2 points):

     | arming bar | 0% | 15% | 25% | 35% | 50% | 65% | 80% |
     |---|---|---|---|---|---|---|---|
     | b10 win rate | 2.44% | 9.79% | **13.91%** | 17.35% | 19.41% | 19.76% | 19.75% |
     | b1 arm rate | 0.0% | 14.6% | **28.9%** | 44.4% | 68.6% | 86.4% | 97.0% |
     | b10 median round pressed | — | 5.0 | **4.0** | 4.0 | 3.0 | 3.0 | 3.0 |
     | b10 pressed on round 1–2 | — | 0.0% | **0.0%** | 2.3% | 13.4% | 23.3% | 23.2% |

     Win rate is **monotone increasing to ~50% and then flat** (65% and 80% are inside noise of each other), against a bracket-10 no-Patreon floor of **3.93%**. Every step up to 50% clears 3σ. The **U-shape this table used to show is gone, and it was never a measurement error** — it was the automatic mechanic, faithfully measured.
   - **Why 25% and not the optimum.** ~65% is the optimum and 25% gives up **5.85 points** of bracket-10 win rate to it, deliberately. What those points buy is tone: at 25% the median press is round 4 and **no** press lands on rounds 1–2, so the charge is a late-fight decision rather than an opening move — the only reading under which `VENOM_BLAST_LINES` ("swallows the hit whole") and the button's own "the bond is charged" framing are honest. At 65% nearly a quarter of presses are round 1–2 and it becomes a rotation piece. **Those 5.85 points are the price of the copy, not an oversight**; raise the bar and rewrite the copy in the same commit.
   - **Read this before concluding the perk is fine.** The button is a **much weaker perk than the automatic version was**, and no arming bar buys that back. The old code returned `damage=0` on the hit that would have crossed the line — a once-per-fight **death save**, applied by the suit at exactly the right moment because it had perfect information about a roll that had already happened. A button cannot do that: it is pressed *before* the round resolves. At the same 25% bar, bracket-10 win rate went **60.86% (automatic) → 13.91% (button)**. That span also contains the override moving 0.15 → 0.30, but the override is the small term: at bar 0%, where the button never arms, Symbiote still wins 2.44% against the same 3.93% floor, so the override costs about 1.5 points there and **the rest of the ~47-point gap is the mechanic**. This is a direct consequence of the design that was asked for, not a regression to go hunting. Closing it would mean giving the press a defensive component (absorb the *next* hit, say) — a different mechanic, not a different number in this section.
   - **At bracket 1 the tier is now net negative in boss fights: −0.38%** (96.89% Symbiote vs 97.27% unsubscribed — `combat_sim.py venom` reports this as a finding on purpose, and it is the only one it reports). Neither term is a bug: a comeback perk has no headroom at a boss you already beat 97 times in 100, and the override still charges its ~0.3 points there. It is the **only** bracket where this is true — b5 / b10 / b20 are **+26.76% / +10.30% / +8.81%**. It matters because bracket 1 is the boss a brand-new subscriber meets first, so "my first boss got slightly harder after I pledged" is a plausible ticket with a true answer.
   - **Validated 2026-08-23, re-validated 2026-08-24 for the button** (`scratch/combat_sim.py venom`), closing the "flagged for real validation" note that stood here from when `scratch/boss_tune2.py` was lost. The sim drives the *real* resolvers rather than re-deriving the damage math, and replicates only `_advance`'s end-of-round conditions (those live in a cog). It is **gadget-free** — `resolve_gadget` is async and hits the DB per round — so its absolute win rates are **not** comparable to the 70–75% benchmark in `ENEMY_STATS["boss"]`, which assumed a fully-upgraded kit. What it measures is *deltas* between otherwise-identical configurations, where the missing gadget contribution cancels. **One thing the harness now decides that it never used to:** the blast is a choice, so the sim has to have a policy, and `run_fight`'s is *press it the round it arms*. Greedy is defensible rather than merely convenient — the charge doesn't grow, integrity only falls, and holding it forfeits a round of guaranteed damage and a negated counter for nothing — but a real player saving it for a round they expect to be lethal has strictly better information than the sim does. **Read every button-era number here as a floor on the perk's value, not an estimate of it.**
   - **The "2×" is now a named constant** (`VENOM_BLAST_DAMAGE_MULTIPLIER`), not the bare `* 2` it was until 2026-08-22. It is **copy-bound, not a free tuning knob**: `VENOM_BLAST_LINES` promises "twice as hard" / "pays it back double" and `SYMBIOTE_PERKS_STATIC` echoes it, so moving the number means rewriting all of that too.
   - **The self-scaling claim holds.** Because it multiplies `attack_damage_range`, which scales on the same difficulty curve as enemy HP, the blast is worth a near-constant share of the boss health bar all the way up the ladder: **33.3% / 32.3% / 31.8% / 31.7%** at brackets 1 / 5 / 10 / 20 — a 1.67% spread across the whole game. This is the entire reason it's a multiple of an attack roll rather than a flat figure that would need its own tuning pass per bracket. (Unchanged by the button: the multiplier is applied identically, only the trigger moved.)
   - **Still the tier's load-bearing perk, but no longer a landslide — re-measured for the button 2026-08-24.** Gadget-free boss win rate, Symbiote off → on: bracket 1 `97.27% → 96.89%`, bracket 5 `26.48% → 53.23%`, bracket 10 `3.78% → 14.09%`, bracket 20 `2.47% → 11.28%`. Sensitivity at bracket 10 (×0 → ×4): `2.27% / 5.70% / 14.08% / 27.13% / 39.06%` — the span across the whole range is 36.79 points, down from ~81 under the automatic version, but the shape that mattered is intact: **×2 → ×3 nearly doubles the win rate**, so the constant is still steeper than anything else in the tier. That is the same balance fragility §9.2's Enhanced Strength comment cites as its reason for excluding boss fights. **Do not treat this constant as safe to nudge.** (The older, larger figures this bullet used to carry — `98.19% / 92.47% / 70.13% / 62.47%` and a `17.55% → 98.21%` sensitivity curve — were correct measurements *of the automatic mechanic*, and are kept here only so the size of that change isn't quietly lost.)
   - **One consequence worth knowing, not a bug.** The perk is a *comeback* mechanic, so its value is inversely proportional to how well you'd otherwise do: **−0.38 points at bracket 1 and +10.3 at bracket 10**, because at bracket 1 there's no headroom to give back. The old second consequence recorded here (that the copy implied rarity while the blast fired in 76–98% of fights past bracket 5, versus 3.4% at bracket 1) is **gone in both directions** — the arm rate at the shipped bar is 28.7% / 73.2% / 46.3% / 50.8% at brackets 1 / 5 / 10 / 20, which is neither invisible at the bottom nor near-certain at the top. Under a button, "fired" also stops being purely a property of the trigger: it now includes the player choosing to press, and a fight can end with the charge armed and unspent.
2. **Biomorphic Webbing** — **the Symbiote tier's webbing perk, and the only one it has**: Organic Webbing's evolved grade, not an addition beside it (see §9.2's "webbing ladder" — Organic is its prerequisite, enforced free by the rank ladder). It carries everything Organic did (vial-free patrols, no cash tax) *plus* **four** independent rolls (not one shared roll — copy promises "coins, photos, AND parts"). All four report themselves on the result card (§9 audit); each roll's own result field exists purely for that:
   - `BIOMORPHIC_WEBBING_CASH_CHANCE = 0.25` → +$15–35 (applies to combat *and* non-combat patrols — lives in `patrol_service.py`, imported into `battle_service.py`). Reported via `BattleReport.biomorphic_cash` / `PatrolResult.biomorphic_cash` as subtext under the Cash value, since it isn't separate income — just a share of one number the player otherwise couldn't attribute.
   - `BIOMORPHIC_WEBBING_COMPONENT_CHANCE = 0.20` → bonus component drop, **only rolled if the base drop_chance roll already missed** (never stacks into a guaranteed double-drop). Combat only. Reported via `biomorphic_component` — before 2026-08-21 this was byte-identical to a base-game drop, so the entire perk was invisible.
   - `BIOMORPHIC_WEBBING_PHOTO_CHANCE = 0.20` → bonus second `PendingPhoto`, only if a camera's equipped and a photo was already banked this fight. Combat only. Reported via `biomorphic_photo` as a "Second Shot" row, and it now takes its own quality-bump roll off the equipped camera (§6.2).
   - **`AMBIENT_SCAVENGE_CHANCE = 0.30` → a component picked up during a *non-combat activity*: `/tutoring`, `/ally visit`, `/bugle submit`. Added 2026-08-24 at 0.20, raised to 0.30 on 2026-08-25.** This is the roll that finally makes the perk distinguishable from plain Organic Webbing while you are **not** patrolling — before it, a subscriber who mostly did quiet, non-patrol things saw exactly one difference (no vials) and three rolls that could never fire for them. Weighted 3:1 **Spandex Fabric : Micro-Electronics** (`AMBIENT_SCAVENGE_TABLE`), i.e. 22.5% / 7.5% per activity, matching the economy `data/items.json` already describes (spandex the ordinary patrol scavenge at $80, electronics the "rarer" one at $150 — so the rarer roll is also the more valuable). Those are the only two components in the game, and they are exactly the two the suit needs to repair, which is the point: the webbing brings home what the bond will later demand.
     - **0.30 makes this the perk's *highest* rate, above the cash roll's 0.25 and the two combat rolls' 0.20, where it used to be tied for lowest.** Raised on the owner's explicit instruction ("we need to up the percentage a lil bit"). The original argument for 0.20 — one recognisable rate across all four rolls — **was already false when it was written**, since the cash roll has always been 0.25. What makes the new ordering defensible rather than merely instructed is that the two 0.20 rolls carry *extra preconditions on top of the rate*: the component only rolls if the base `drop_chance` roll already missed, and the photo needs a camera equipped **and** a photo already banked that fight. This one fires on every completed activity with nothing else asked of it. A flat rate across rolls with unequal preconditions was never equal odds to begin with.
     - **What bounds it is the sink, not the rate, and that is the thing to check before moving it again.** A repair consumes **exactly one** Spandex Fabric (plus one Micro-Electronics past `suit_service.ELECTRONICS_THRESHOLD`) *regardless of how much integrity is missing* — the cash cost scales, the components don't. And there is **no NPC buyback anywhere in the game**: surplus components can only be listed on the player-to-player market (`market_service`). So this rate buys a Symbiote subscriber repair self-sufficiency and adds supply to that market; it does not mint cash, and the $80 / $150 figures are shop **buy** prices, not payouts. Any "expected value per activity" figure derived from them is a catalog valuation, not income.
     - **`/ally visit` is the fastest way to farm it, and it is free.** The `gift_key` parameter is optional — a giftless visit takes the `PLAIN_VISIT_BOOST` branch, costs no item, and still commits and still rolls. There is **no cooldown** on the command, only the busy lock, and `visit_duration_seconds` bottoms out at `MIN_VISIT_SECONDS` (30s) once the ally is near-happy. So ~120 rolls/hour is reachable by clicking, which at 0.30 is ~36 components/hour against ~24 before. This farm predates the rate change and is bounded by the sink above rather than by the rate, so it is recorded here as a known shape rather than as a defect — but it is the first place to look if component supply on the market ever needs explaining.
     - **It lives in its own leaf module, `services/biomorphic_service.py`, and it has to.** `patrol_service` imports `ally_service`, so `ally_service` can never import `patrol_service` back — and an ally visit is one of the three activities. The module imports only `inventory_service` (itself a true leaf), `patreon_service` and `utils.icons`, so all three callers can reach it from anywhere in the graph. **Do not import an activity service from it**; that recreates exactly the cycle it exists to avoid.
     - **Each activity's flavor line says *where* it happened** (`ACTIVITY_FLAVOR`, keyed by the three activity constants, ≥2 lines each). A component silently appearing in the inventory reads as a bug rather than a perk — the same failure the combat component roll had before 2026-08-21. `_flavor_for()` **raises** on an unknown activity rather than rendering an unexplained line, so a fourth activity can't be wired up without copy.
     - **The roll fires after each activity's own commit**, so a bonus can never roll back a completed activity. `inventory_service.add_item` commits and does not validate `item_key`, which is the other reason the table is a module constant rather than a caller-supplied string.
     - **The gate is `tier_rank`, threaded from the cog like every other perk**, with a `TIER_RANK_NONE` default so a caller that forgets to thread it fails *closed*. `roll_ambient_scavenge` returns `None` both for "not a subscriber" and for "the roll missed", so there is no way to tell the two apart from the outside.
   - **The perk copy is scoped to match.** `BIOMORPHIC_WEBBING_LINE` promised all three rolls flatly (2026-08-22 fix), which told a subscriber who mostly runs quiet patrols to expect two rolls that can never fire for them. It now covers all four: cash unqualified (it genuinely is rolled on both patrol paths), component and photo carrying the **combat patrols** qualifier, and the ambient scavenge as "it keeps helping itself wherever else you go — tutoring, visiting, selling photos". Phrased as "wherever else you go" rather than a strict command list because the list would go stale the moment a fourth activity is wired up, and the flavor line on the pickup itself always says where it happened. Deliberately not qualified further — the photo roll also needs a camera equipped, but that's already true of every photo in the game and spelling it out here would read as a second restriction rather than the same one.
   - Verified: `scratch/check_ambient_scavenge.py` — the loot table agrees with `items.json` (both keys exist, names match, both `category == "component"`, spandex weighted higher, electronics priced higher), every activity has flavor and there are no orphan keys, the gate never leaks (1,200 rolls each at no-tier and Arachnid, zero fires), Symbiote fires at **29.0–30.5%** with the 3:1 split holding, end-to-end through all three services, and all three default to no perk when `tier_rank` is omitted. The rate is asserted as an **exact** value (`AMBIENT_SCAVENGE_CHANCE == 0.30`) and the ±2.2pp band is derived from it, so a future move has to come to that script and choose a new band rather than sliding under a tolerance that keeps passing.
3. **Stealth Mode** — full (100%) `/shakedown` immunity while the *target* has been inactive ≥ `STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS (20 min)` (via `User.last_active_at`). Deliberately **not permanent** (rejected earlier as pay-to-win) — reads as "protected while you're not even playing," since shakedowns can hit online or offline players alike. When protected, the attacker's attempt fails with **no fail-penalty charged** ("they back off before getting close enough to get caught" — distinct flavor text from a normal failed attempt).
   - **The 20 minutes is instrumented but still UNVALIDATED (instrumentation shipped 2026-08-23).** It is a reasoned guess, and unlike Venom Blast's `2×` it **cannot be settled by simulation**: it depends entirely on how long real players step away from Discord, and there's no ground truth for that to simulate against. This is the one open validation item where building a sim would be answering a different question than the one asked.
   - **Why it was previously unmeasurable at all.** A protected attempt returns before charging anyone, so unlike a success (2 `transactions` rows) or a fail (1 row) it left **no trace whatsoever** — not even a row saying it happened. The perk's real firing rate was unobservable, so "is 20 right?" had no evidence available either way.
   - **`shakedown_attempts` (new table, migration `a7c41e93b508`)** now records one row per attempt that reaches the resolver: outcome, the target's idle seconds, their tier rank, cash moved, and their pre-attempt wallet.
   - **The design decision that matters: idle time is recorded on EVERY attempt, not just protected ones.** That's what makes any candidate threshold scoreable against data already collected, instead of needing a fresh instrumentation deploy per number under consideration. A bare `was_protected` boolean would answer for 20 minutes and for nothing else. Non-subscribers are logged too — subscribers are few, so the broader population is the better estimate of how long players are typically idle when targeted.
   - **`target_wallet` is the pre-attempt wallet** — read before `add_wallet` runs, so a successful steal can't make the sample look like the target was poorer than they were when they got picked.
   - **`target_idle_seconds` is nullable, and NULL ≠ 0**: the target has never run a command, which is itself why Stealth Mode can't fire for them. Collapsing that into 0 would misreport "unknown" as "just active".
   - **Instrumentation is strictly non-load-bearing.** `log_shakedown_attempt` opens its **own** session rather than joining the caller's, and swallows every exception. A telemetry write must never roll back real cash movement, be rolled back by it, or fail a player's `/shakedown` — including when the migration hasn't been run yet. Losing a measurement is acceptable; losing a command to record one is not. Both failure paths are tested by deliberately breaking the insert.
   - **The gate's behaviour is unchanged.** `_stealth_mode_active(session, target)` split into `target_idle_seconds()` (the reading) + `stealth_mode_active(tier_rank, idle_seconds)` (a pure predicate, no DB and no clock). Reasons: the logged idle value is now guaranteed to be *the one the gate actually judged* rather than a second reading taken milliseconds later, and the analysis script can replay the real rule at other thresholds without duplicating it. Boundary behaviour is pinned by test at exactly the threshold, one second short, non-subscriber, Arachnid, and never-active.
   - **How to actually answer the question:** `python scratch/analyze_stealth_mode.py`. It reports the firing rate against Symbiote targets (the honest denominator — measuring against all attempts would understate the perk just because most players don't subscribe), the idle-time distribution, and the counterfactual protection rate at every value in `STEALTH_MODE_CANDIDATE_THRESHOLDS_MINUTES` (5/10/15/20/30/45/60/120). It refuses to conclude anything from an empty table and prints a sample-size caveat. **The target band is a threshold protecting a clear minority of attempts** — high enough that actively-playing subscribers stay targetable (otherwise it's the rejected pay-to-win immunity), low enough that it fires often enough to be worth paying for.
   - **Not counted:** attempts refused by `pvp_cog` before reaching the resolver (thief cooldown, the target's 2-min `shakedown_target` protection, target under `MIN_TARGET_WALLET`, self/bot). Those are input validation rather than attempts. Note the target-protection cooldown *is* still set on a protected attempt, so a second try within 2 minutes is turned away by the cog and never appears in the data.
   - **The perk was invisible to the person paying for it until 2026-08-25, and that is now fixed across three surfaces** — the thief's panel, a pull (`/patreon perks`), and a push (a DM to the target), each of which is a separate bullet below. This is the only perk in the tier that fires where its owner cannot see it: a protected attempt renders a panel for the **thief** and nothing at all for the target — no DM, no ping, not even a `transactions` row — and by construction the gate only opens once the target has been idle 20+ minutes. "It worked" and "you weren't watching" are *the same condition*. A subscriber could hold the tier a full month, have a dozen shakedowns turned away, and have no way to learn any of it happened. Every other perk in §9.2/§9.3 announces itself the moment it fires.
     - **The thief's panel is now a perk, not a refusal.** The branch was an `error_embed`, and `error_embed` titles all five of this command's refusals **"Parker Luck."** — so the one Symbiote perk a thief ever watches fire was dressed as the thief's own bad luck, and a legacy embed can carry neither the accent bar nor a thumbnail. It's now a `StaticView` titled "Stealth Mode" with the `stealth_mode` thumbnail. The other two branches are deliberately untouched: a successful shakedown was already a `StaticView`, and a *failed* one is a genuine refusal that `error_embed` fits.
     - **All three attribution marks name the target, not the reader** — glyph, badge, **and now the accent**, which is the easiest of the three to get wrong. `make_container()`'s default accent comes from an ambient `ContextVar` (`utils/tier_accent.py`, set once per command by `utils/first_run.py`'s `before_invoke` hook) read from the **invoking** user, i.e. the thief. Left to default, a subscribed thief would see their *own* tier colour on a message about somebody else's subscription, and an unsubscribed thief would see no bar at all on a panel whose entire subject is a subscription — and nothing on screen would look broken either way. So the branch passes `accent=accent_for_rank(result.target_tier_rank)` explicitly. Safe unguarded only because `stealth_mode_active()` gates on Symbiote, so the rank is never low enough for `accent_for_rank` to return `None` (which `make_container` would read as "use the ambient one", i.e. back to the thief's).
     - **`/patreon perks` now reads the rows back to the subscriber:** `count_stealth_protections(target_id)` counts their `OUTCOME_PROTECTED` rows, and `_stealth_mode_line()` appends "It's turned away N attempts so far — you weren't there to see any of them." No new table, no new write — the instrumentation shipped two days earlier for threshold analysis turned out to be exactly the data the perk's owner had a stake in. The **welcome DM** carries it too, since `/patreon link` re-sends that card on demand and a long-standing subscriber re-running it should see the same number.
     - **Zero renders no clause at all**, not "blocked 0 attempts" — a brand-new subscriber reading the welcome DM shouldn't be handed a nil stat about a perk they haven't had time to benefit from. And the 20 is **interpolated from the constant**, not typed into the copy: hardcoding it is precisely what went stale on the Venom Blast line when its multiplier moved, and the check proves the copy tracks the constant by moving it to 45 and re-rendering.
     - **The read is as non-load-bearing as the write.** `count_stealth_protections` opens its **own** session and swallows everything, for both of `log_shakedown_attempt`'s reasons: the table is allowed to be missing (migration not yet run), and a failed query on a borrowed session poisons the caller's transaction. A perk *count* must never be able to take down the panel that lists the perks. It returns 0 on any error, which renders as the pre-2026-08-25 line rather than as a wrong number. Tested by breaking the query outright, same as the insert.
     - **`/patreon perks` is pull-only, so a DM to the target is the push half (added 2026-08-25).** The card only helps a subscriber who thinks to go and look, and nobody goes looking for a perk they don't know fired. When Stealth Mode turns an attempt away, `pvp_cog._notify_stealth_target` DMs the target: which perk stopped it, who came looking, that nothing was taken, the 20-minute rule interpolated from the constant, and the same all-time count `/patreon perks` shows.
       - **Scoped to the protected branch only, deliberately.** A *successful* shakedown against a Symbiote subscriber does not DM them, and that's not an omission — being robbed while away is a gap that affects every player equally, so filling it would be a new game-wide feature (or a new perk), not part of making this one visible. This DM's entire subject is the perk firing.
       - **The thief is named, and that leaks nothing.** `/shakedown`'s response is **not ephemeral**, so the channel already saw "Stealth Mode — you back off before you even get close" while the target was away. The DM reports a message that was posted publicly where they weren't looking, and who came sniffing is the only part of it with any value to them.
       - **§9 attribution applies in its normal orientation here** — unlike the thief's panel, which is the codebase's one inversion. The reader *is* the payer, so the glyph leads and their own badge trails. But **the accent is the same trap**: `_stealth_dm_view` is built inside the *thief's* command context, so `make_container()`'s ambient default would paint a subscriber's own perk DM in the thief's colour, or in no colour at all. It takes `accent=accent_for_rank(target_tier_rank)` explicitly. The parameter is named `target_tier_rank` rather than `tier_rank` for the same reason the field on `ShakedownResult` is — and concretely, because `scratch/check_symbiote_icons.py` asserts the string `accent_for_rank(tier_rank)` never appears anywhere in `pvp_cog.py`.
       - **Throttled to one DM per 15 minutes per target** (`STEALTH_DM_COOLDOWN_SECONDS`, key `stealth_dm`, §20.6). Needed because of the perk's defining property: it only fires while its owner is idle 20+ minutes, so a run of attempts by different thieves during one absence would arrive as a stack of near-identical DMs waiting for them rather than as a live feed. The real floor is tighter than the constant alone suggests — `TARGET_PROTECTION_SECONDS` already caps any target at one attempt per 2 minutes from anyone, so 15 minutes turns a worst case of ~30 DMs/hour into at most 4, while an ordinary once-in-a-while attempt still always gets its own.
       - **Throttling is lossless in aggregate, which is the only reason it's acceptable.** The DM quotes the **all-time** total, not "this is number N" — so a subscriber attempted five times and DMed once still learns the true figure, and it's the same figure `/patreon perks` gives them.
       - **The window is claimed in the service, on the protected branch only, and after the log write.** `claim_stealth_dm_slot` is named for its side effect: the check and the write have to be one step or two thieves resolving back-to-back both pass it. It sets `ShakedownResult.notify_target`, so the cog does only the Discord half and can't forget the throttle. **An ordinary shakedown must never consume the window** — the target would then miss the next attempt the perk actually turned away, which is the one thing this DM exists to report, so the fix would have reintroduced the bug it was fixing. And it runs *after* `log_shakedown_attempt`, because the count in the copy has to include the attempt being reported.
       - **Unlike the other two Stealth Mode helpers, this one takes the caller's session and is allowed to raise.** Those are telemetry, where losing a row beats failing a command. This is a throttle on player-visible behaviour, writing to a `cooldowns` table `/shakedown` already touches twice in the same transaction — if the write fails, the shakedown should fail with it rather than silently leave the throttle unset and DM on every attempt from then on.
       - **The send itself can't break anything.** It runs *after* `ctx.respond`, because answering an interaction has a 3-second deadline and this needs a `fetch_user` round-trip plus a DM send. It catches `discord.HTTPException` (closed DMs — the expected failure, logged at info) and then everything else. A bounced send still burns the 15-minute window, which is correct rather than merely tolerable: closed DMs are a persistent state, so releasing the slot would just mean retrying a doomed send on every future attempt.
   - Verified: `scratch/check_stealth_instrumentation.py`, **105 checks** — the gate, the instrumentation, the count, how the perks line reads at 0/1/N, and the DM. On the DM specifically: that a protected attempt asks for one and a second inside the window doesn't while still being protected and counted; that the gate is a real cooldown and not a once-ever flag (the window is aged into the past by editing the row, *not* by lowering the constant — the constant is only read when a slot is claimed, so lowering it wouldn't expire an open window and the test would pass for the wrong reason); that an active Symbiote target and a non-subscriber leave **no `stealth_dm` row at all**; the copy at 0/1/N with the threshold proven to track its constant; the accent read off the target with the ambient-default counterfactual; and that closed DMs and an arbitrary exception are both swallowed — asserted via a stub that records the `send` was actually reached, so a helper that returned early couldn't pass as "swallowed". Plus `scratch/check_symbiote_icons.py`, 144 checks, which pin the panel conversion — V2 not embed, `error_embed` gone *from that branch only*, the title, the thumbnail key, that the PNG is actually on disk (a missing one degrades silently to a plain title, which would make the whole conversion a no-op), the accent read off the target and **not** off the invoker, and the counterfactual that an ambient-default container has no colour at all.
4. **The suit overrides you (drawback)** — shipped 2026-08-23, **widened 2026-08-24, then doubled the same day on the owner's explicit instruction**. `SYMBIOTE_OVERRIDE_CHANCE = 0.30`: each time a Symbiote subscriber presses **Evade** *or* uses a **gadget**, a 30% roll replaces the whole action with an Attack instead. This is now the tier's **only unconditional drawback** — the Sonic Dampener that used to share the job was deleted (see the tombstone below) and the ally-decay penalty is inherited from Arachnid (§9.2) rather than being Symbiote's own. One mechanic carries the entire "the bond costs you something" side of the tier, which is why it is priced this carefully — and at 0.30 it is priced *past* the bar this file used to enforce, deliberately and with the measurement on record. Read the three bullets under "what 0.30 actually costs" before touching it in either direction.
   - **Scoped to Evade and Gadget, never to Attack, and that scope is the design.** The rule the copy states is *the suit overrides you when you try to hold back*. It never overrides aggression, because there's nothing there to override — a player who only ever attacks is already fighting the way it wants, so the mechanic correctly never fires for them.
   - **Gadgets were exempt until 2026-08-24, and the objection was answered rather than overruled.** The recorded reason for exempting them was that they're scarce and carry durability, so swallowing one "reads as a lost *item* rather than a lost impulse". So the hijack sits at the very **top** of `resolve_gadget`, above `roll_gadget_effect` and `roll_gadget_wearout`: a hijacked press costs you the round but **never the gadget** — you lose the effect and the tempo, not the gear. **Do not move that branch below either roll.** Billing durability for a button the suit wouldn't let you press is the exact thing that kept gadgets exempt in the first place.
   - **Extending it there is also what makes this a real cost outside boss fights**, which it previously wasn't. In a crime patrol suit integrity is cosmetic (it only bills you for repairs afterward), so losing an Evade's damage reduction costs almost nothing — but losing a gadget's damage costs the same there as anywhere. At an unchanged 0.10 the crime cost went from **−0.51% to −2.36%** purely from adding the gadget surface.
   - **Both halves of the Evade cost bite.** The override forfeits the `EVADE_DAMAGE_MULTIPLIER` reduction (verified at a **4.02×** measured suit-damage ratio against the 4.00× the constant implies) *and* banks no combo for next round, because `combo_ready = True` sits below the early return. It delegates to `resolve_attack` rather than duplicating it, so Enhanced Strength still applies to the round the suit stole. **Venom Blast no longer does** — it left `resolve_attack` when it became a button (item 1), so a hijacked Evade is now an ordinary attack and nothing more. That's the intended reading: the suit can steal a defensive impulse, but it can't spend a charge the player didn't press.
   - **One deliberate upside**: it still *consumes* a combo banked by a previous Evade, so the suit occasionally cashes in an opening the player was about to waste by dodging again. Already priced into the numbers below.
   - **What 0.30 actually costs (`scratch/combat_sim.py override` and `... package`, re-measured 2026-08-24 after the rate change and the Venom Blast rework).** Against the identical fight with the override off:

     | rate | boss b10, evade-only | boss b10, 3 gadget presses | crime gold, evade-only | crime gold, gadget |
     |---|---|---|---|---|
     | 0.10 | −2.16% | −10.59% | −0.51% | −2.36% |
     | 0.15 | −3.28% | −15.91% | −0.80% | −3.73% |
     | 0.20 | −4.64% | −20.16% | −0.89% | −4.87% |
     | **0.30** | **−6.82%** | **−29.02%** | **−1.74%** | **−6.88%** |

     Visibility at 0.30 is **1.20 hijacks per bracket-10 boss fight**, with at least one in **78.8%** of them — so only about a fifth of fights pass without the suit taking a round off the player. (At the original 0.10 that was 0.43 per fight and 62.8%.) The visibility figures are exact: that's the real constant rolled by the real resolvers.
   - **The cost is not spread evenly — it is concentrated almost entirely in gadget presses, and that is the important number here.** A hijacked **Evade** forfeits a damage reduction; a hijacked **gadget** forfeits an effect that *negates the enemy counter outright* (`negate_damage` / `group_defense` / `shock_burst` all do), which is worth far more per press. Hence −6.82% versus −29.02% at the same rate. Two consequences follow. First, **whether the tier is worth having in a boss fight depends on whether you press gadgets**: gadget-free, Symbiote is net **+26.54% / +9.99% / +9.07%** against no Patreon at brackets 5 / 10 / 20 (and −0.38% at bracket 1); with three gadget presses per fight it is net **−11.86% / −6.41% / −3.35%** at those same brackets. That means the drawback lands hardest on the player who bought the most gear, which is backwards. Second, **if this ever needs softening, narrow the hijack back toward Evade-only rather than lowering the rate for everybody** — the asymmetry is in the *surface*, not the number, so dropping to 0.20 costs a gadget-light player visibility they were enjoying while still charging a gadget-heavy one 20 points.
   - **Why 0.30, given the above.** It is the owner's call, made on 2026-08-24 with the measurement in hand, and it **knowingly breaks the rule this file enforced twice before**: that a paying subscriber should never be pushed below the 70–75% boss win rate an unsubscribed player with a fully-upgraded gadget kit gets (the bar that rejected 0.15 in 2026-08-23 at −7.87%, and 0.20 at −8.76%). Under a gadget-using policy 0.30 fails that bar and fails plan criterion 1 outright — `combat_sim.py package` reports all three high brackets as findings, by design, so nobody rediscovers this by accident. What the rate buys is the thing the earlier rates measurably failed to deliver: at 0.10 the drawback the tier *charges for in its own copy* was invisible in 63% of boss fights, so subscribers reported it as unimplemented. **Do not "restore" this to 0.15 as a cleanup**; it is a deliberate trade of win rate for legibility, and the lever to reach for first is the previous bullet.
   - **One caveat on the boss numbers, and it cuts both ways.** `combat_sim` models gadget presses synthetically (it is otherwise gadget-free — see its docstring) with **no fumble rate**, so it *overstates* the hijack's cost; real-play cost is somewhat less than −29.02%. Against that, the delta's *magnitude* scales with how much headroom the baseline has — an evade-only bracket-10 player wins 21.09% before the override and a gadget player 63.13%, which is most of why the same rate reads as −6.82% for one and −29.02% for the other. Compare rates within a column, never across.
   - **Two shapes were priced and rejected — recorded so they don't get re-proposed.** (a) Making the override hit *harder* (a guaranteed 1.5×, borrowing the combo constants) is a straight **buff** at every rate: `+3.16%` boss, `+10.41%` crime at 0.15. (b) Adding a suit tear on top of that moves crime-tier win rate by **exactly 0.00%**, because suit integrity is cosmetic *inside* a crime fight. Both fail the same structural test, and it's worth stating plainly: **in crime patrols defense is worthless, so any override toward aggression that also improves the swing is free-to-positive there.** That is the reason the shipped shape is a plain attack. (Those two figures were measured at 0.15 under the pre-button configuration and have not been re-run; the sign is what they establish, and nothing about the button changes why a bigger swing is a buff.)
   - **Reverting, and what each lever does.** The mechanic is the early-return block at the top of `resolve_evade` plus the one at the top of `resolve_gadget`; `SYMBIOTE_OVERRIDE_CHANCE = 0.0` disables both without touching code (verified). `SYMBIOTE_OVERRIDE_CHANCE` is read in **exactly two places** — those two blocks — and `scratch/check_override.py` asserts that count, so a third reader can't appear silently. Deleting the `resolve_gadget` block alone is the "narrow it back to Evade-only" lever from the asymmetry bullet above.
   - Emits `SYMBIOTE_OVERRIDE_LINES` (8 variants, Dodge-shaped) or `SYMBIOTE_GADGET_OVERRIDE_LINES` (8 variants) + the tier badge. **Two lists, not one**, because the two hijacked actions read differently: an overridden Dodge is the suit refusing to retreat, an overridden gadget is the suit refusing to let Peter solve the problem with engineering — using the Dodge lines for a gadget press ("you move to break away") would describe something the player didn't do. Every line has to make it unmistakable that the button is about to read as an attack; a player who can't tell why reads it as a bug, which is the worst possible outcome for a cost they're paying for. Disclosed in `SYMBIOTE_DRAWBACKS`, so it surfaces in both `/patreon perks` and the welcome DM under "What It Costs You", and **only** for Symbiote — an Arachnid subscriber is never shown a cost they don't pay.
5. **Suit repair reads differently (reskin, no mechanical change)** — shipped 2026-08-24, Symbiote only. For a subscriber the thing on Peter's back isn't a suit he sewed: it's alive, he doesn't understand it, and it *still* wants exactly the two components a fabric suit wanted. `/workbench` becomes **"The Bond"**, "Full Repair Cost" becomes "What It Wants", "Components on Hand" becomes "What You Can Give It", `/workbench repair`'s "Cost"/"Restored" become "What It Took"/"Closed Up", and all six `repair_suit` messages plus both `repair_readiness_warning` branches get Symbiote variants, off a second footer pool.
   - **The mechanics are byte-identical at every rank** — same `REPAIR_COST_PER_POINT`, same single Spandex Fabric, same Micro-Electronics threshold, same eviction gate, same refusal *conditions*. That is the whole constraint, and it's enforced by test rather than by reading: `scratch/check_symbiote_repair.py` runs the identical scenario at all three ranks and asserts one distinct outcome tuple across them (wallet delta, integrity restored, both inventory deltas, `used_electronics`) for a light repair, a heavy repair, and all five refusals.
   - **The reskin cannot drift into implying a different cost.** A subscriber who reads it as its own mechanic will go hunting for a shop item that doesn't exist, so every Symbiote string names the same two components and points at the same commands — asserted directly: the no-spandex refusal still says "Spandex Fabric" and `/shop buy spandex_fabric`, no-electronics still says "Micro-Electronics" and `/shop buy micro_electronics`, eviction still says `/apartment pay`, and the broke refusal still quotes the exact dollar figure (the one number a player acts on).
   - **Deliberately NOT reskinned: the words "Suit Integrity".** That label is the game's shared stat name and also renders in `/balance`, the boss gate and `/admin profile` — reskinning it in one panel of four would make the same number look like two different stats. What gets reskinned is the *act of repairing*, which is what was asked for. This is a documented deviation from the original plan, which had listed the label.
   - **Threaded, not looked up** — `repair_suit(session, user, tier_rank=TIER_RANK_NONE)` and `repair_readiness_warning(...)` take the rank as a parameter, matching the house pattern, with a fail-closed default. `patrol_cog`'s two readiness-warning call sites pass the rank they already computed.
   - Attribution: `/workbench status` gains a one-line Symbiote intro carrying the badge, because a reskin nobody can attribute is indistinguishable from the bot having been rewritten. **No perk glyph** — this isn't one perk firing (§9's two-glyph note).

**⛔ Removed: Sonic Dampener (drawback), deleted 2026-08-24.** It was a `+30%` incoming-counter-damage penalty scoped to boss fights against **one** of the twenty bosses ("the Shocker"), reachable at brackets 4/24/44/64 — reputation levels 20/120/220/320. It is gone, along with `SONIC_DAMPENER_BOSS_NAME`, `SONIC_DAMPENER_DAMAGE_INCREASE`, `_is_sonic_dampener_boss()`, `SONIC_DAMPENER_LINES`, `CounterOutcome.dampener_note` and all four render-site appends.
   - **Why it went, and what not to replace it with.** One boss in twenty made it invisible to almost every player almost all the time, and the tier's cost is now the combat override above, which fires on every kind of fight. **Do not reintroduce a per-boss drawback**: all 20 named bosses share one identical stat block, so there is no attack-type system to hang a thematically broader version on — which is exactly what made this one so narrow. The tombstone comment above `VENOM_BLAST_DAMAGE_MULTIPLIER` in `battle_service.py` says the same thing at the call site.
   - **Two real bugs it accumulated, worth keeping on record.** (a) The boss check was an exact `state.enemy_name == SONIC_DAMPENER_BOSS_NAME`, but `patrol_service.boss_name()` suffixes repeat encounters `" (Round N)"` once the roster wraps — so `"the Shocker (Round 2)"` never matched and the dampener fired **exactly once in the entire game**, at reputation level 20, for every player (fixed 2026-08-22 via a prefix match). (b) It applied the multiplier in **total silence** until 2026-08-21, which read as the boss being tuned unfairly rather than as the tier's own stated cost — the worst failure mode for a *drawback*, since the player couldn't tell it existed. Both are the reason the removal isn't a loss: the mechanic spent most of its life either not firing or not saying so.
   - **Removing it is a small buff** (one boss in twenty, so small on average), and it was priced as part of one package with the override widening and the Venom Blast change — all three move the same number, and the net was measured together rather than per-change. `scratch/combat_sim.py package` is that measurement and it is still the right place to look; note that the package it now prices is the **second** round of that day's changes (override 0.30, Venom Blast as a button), not the first.

### 9.4 Explicitly NOT part of the Patreon roster (dormant, for a separate track)
`PatreonLink.growth_perk_choice` (`"xp"` / `"allies"` / `None`), `ACCELERATED_GROWTH_XP_MULTIPLIER = 1.3` (economy.py), `SUPPORTIVE_ALLIES_DECAY_MULTIPLIER = 0.7` (ally_service.py) — this mechanism was originally wired to Patreon tier_rank via a `/patreon choose` command, which was a mistake later corrected (2026-08-18): it actually belongs to a **separate, not-yet-rebuilt Discord Server Booster perk track** (was built once, fully reverted per user instruction — see commits `76dbc26`/`a05e719`/`10a2c8f` area). `/patreon choose` has been removed. The code stays intact and dormant — do not delete it, do not re-wire it to Patreon; when the booster track gets rebuilt, gate it on Server Booster status instead. §9.5 now records which specific perks that rebuilt track owns.

### 9.5 Not-yet-implemented perk designs (numbers locked, track assigned, no code yet)
Four perks are fully speced with locked numbers but unbuilt, all four on the Booster track. **Track ownership was decided 2026-08-21** — this was the long-standing open question (two tracks exist and had to not be conflated, see §9.4); it is now settled and no longer needs re-asking:

**Server Booster track** (discord.gg/spider-man Nitro boosting — the track that was built once then fully reverted, commits `76dbc26`/`a05e719`; rebuilding it requires re-enabling the Discord **Members intent** to read boost status, so it is a live-bot config change, not just code):
- **Higher Suit Integrity**: 25–35% less suit damage in crime-tier patrols only (boss fights untouched — that curve is tuned around full-strength numbers). Rejected alternative: raising the 100 integrity cap — `100` is hardcoded in 5 places including `repair_suit()`, which would silently reset a raised cap back to flat 100.
- **Higher Reputation XP**: 25–35% bonus, applied at §3's `add_reputation` chokepoint — self-limiting by construction, since that function also enforces the boss-gate ceiling, so it can never skip a gate. Explicitly **no effect on boss-clear promotions** (those snap straight to the next level's floor, bypassing `add_reputation` entirely). Concretely: level 10→15 is ~144 patrols unboosted vs ~106 at +35%.
- **Supportive Allies**: 25–35% decay reduction on `DECAY_PER_HOUR`. Renormalized 2026-08-22 against the new 24h baseline (§12) — full drain stretches from 24h to **32–37h**, and thriving-to-neglected from 9.6h to **12.8–14.8h**. The dormant code already carries the midpoint, `SUPPORTIVE_ALLIES_DECAY_MULTIPLIER = 0.7` (a 30% reduction → 34.3h full drain, 13.7h thriving-to-neglected), so the band is documentation around a shipped number rather than an open choice. **Mutually exclusive with Higher Reputation XP** — stacking them compounds toward ~1.5–1.6x total XP rate instead of either perk's standalone ~1.3x, because ally happiness already gates ±20% XP/earnings on its own. This exclusivity is exactly what `growth_perk_choice` already encodes (one field, one value) and must be enforced at the grant site, not just documented.
- **Quicker Web Brewing**: `BREW_DURATION` 5min → 3min. Note: the earlier locked design had this as ungated/everyone; per the 2026-08-21 decision it belongs to the Booster track. 3 minutes was chosen deliberately — it raises theoretical max vial coverage from ~30% to ~50% of back-to-back patrol demand, while 1.5min would let a non-subscriber replicate Organic Webbing (§9.2) through effort alone and undercut that perk. Cutting brew *time* by X% is **not** +X% throughput — it's inverse (-30% time = +43% throughput).

**Symbiote tier** (Patreon rank 2 — the live, built track): nothing outstanding here. **Camera Gold shipped 2026-08-22** — see §6.2 for the ladder table, the four wiring points, and the two silent-failure modes. Its 0.80 bump superseded an originally-locked ~45–55%, which was written before Silver shipped at 0.60 and so sat *below* the lower tier, inverting the ladder; Gold had to beat 0.60 and does. It is a separate `"tool"` item, **not** the `upgrade_level`-on-one-item shape this section originally sketched (superseded by §6.2). **The tier's own always-on cost shipped 2026-08-23** (§9.3 item 4) — it previously had no drawback that fired on an ordinary patrol — and **Venom Blast's 2× is validated rather than assumed** (§9.3 item 1).

**A seven-item pass closed the tier out on 2026-08-24**, and the theme running through all seven is worth stating because it predicts where the *next* problem will be: every one of them was a perk that existed in code but that a paying subscriber could not see. Three were reported as "not implemented" and were in fact implemented and firing — the override at 9.94% of Evade presses, Venom Blast at 3.4% of bracket-1 boss fights, Stealth Mode on every idle victim — they were just unobservable, unattributed, or scoped to a surface nobody visits. **When a Patreon perk is reported missing, measure its fire rate and check its attribution before touching its logic.** The pass shipped: the Gold Camera admin-grant fix (§6.2), the Sonic Dampener removal and the override widening to gadget presses (§9.3 item 4), Venom Blast's earlier trigger (§9.3 item 1), Biomorphic Webbing's ambient scavenge across the three non-patrol activities (§9.3 item 2), the Symbiote repair reskin (§9.3 item 5), and the two-glyph attribution rule that put all four custom Symbiote glyphs on screen (§9).

**A second round followed the same day, on three further reports, and it is the one that moved the balance.** (a) The retired camera body is now **deleted** rather than unequipped-and-kept — the kept row was a lapse fallback that never worked and showed up in `/inventory` as apparently-working gear (§6.2). (b) Venom Blast became a **player-pressed button** instead of an automatic interception (§9.3 item 1). (c) `SYMBIOTE_OVERRIDE_CHANCE` doubled to **0.30** (§9.3 item 4). Read both of those items before adjusting any of it: **(b) and (c) together take the tier from net-positive to net-negative in boss fights for any player who presses gadgets** — measured, recorded, and a consequence of the design that was asked for rather than a defect to hunt. What remains outstanding for this tier is not design work: it's the **live smoke test on a real pledge**, which is the only thing that can confirm the Patreon cache, the tier gate and the emoji upload all line up in production.

**Explicitly cut, do not re-propose**: **Lower Cooldowns** (shortening `/patrol`'s 30s cooldown) was considered and dropped outright — even safe-looking percentages risk making the core loop feel spammy.

---

## 10. Daily Streak (`/daily`)

`services/daily_service.py`, `data/daily_rewards.json`. `DAILY_COOLDOWN_SECONDS = 24h`. Miss the claim window for more than `STREAK_BREAK_HOURS = 48h` and the streak resets to 1.

- Base reward: `cash = rand_range([40,90]) + round(scale_days * 6)`, `xp = rand_range([8,16]) + round(scale_days * 1)`, where `scale_days = min(streak - 1, 29)` (`streak_scaling_cap_days`) — scaling caps out at day 30.
- **Bonus table** (weighted): `gift` (20) → a random gift item (`gift_flowers` 60 / `gift_dinner` 30 / `gift_jewelry` 10 weighted); `web_fluid` (20) → 2–4 vials; `component` (18) → 1–2x a random repair component; `cash_jackpot` (15) → +$80–200; `gadget_upgrade` (6) → free +1 upgrade level on a random eligible equipped gadget, or +$150–300 cash if nothing's eligible; `collectible` (2) → 1x `unstable_web_fluid` (purely a flex item, no mechanical effect); `none` (19) → nothing extra.
- **Milestones** (by absolute streak day, cash/xp added on top of base): day 7 "One Week Strong" (+$150-250, +30-50xp, +1 jewelry gift); day 14 "Two Weeks In" (+$250-400, +50-80xp, +3 of each component); day 30 "A Month of Web-Slinging" (+$400-600, +80-120xp, free gadget upgrade); day 60 "Two Months Running" (+$700-1000, +150-220xp, +1 unstable web-fluid); day 100 "Triple Digits" (+$1500-2200, +300-450xp, +1 unstable web-fluid AND a free gadget upgrade).

---

## 11. Chem Lab (`/lab`)

`services/brewing_service.py`. One active `Brew` per user at a time. `BREW_COST = $30`, `BREW_DURATION = 5 minutes` (candidate for the not-yet-shipped 3-minute perk, §9.5). Yield: `rand_range([2,4])` `web_fluid_vial`. `MUTATION_CHANCE = 0.08` → also grants 1x `unstable_web_fluid` (flex collectible).

---

## 12. Allies — Aunt May & MJ (`/ally`)

`services/ally_service.py`. `ALLY_NAMES = {"aunt_may": "Aunt May", "mj": "MJ"}`. Happiness (0–100) decays continuously (computed from `last_visited_at`, not a background tick). The rate is expressed as a **full-drain duration**, since that's the actual design decision and the per-hour rate is just what falls out of it: `FULL_DECAY_HOURS = 24.0` → `DECAY_PER_HOUR = 100 / 24 ≈ 4.17` (retuned 2026-08-22 from a 16h/6.0-per-hour baseline, which drained too fast to be a thing you could realistically keep up with). At 24h, thriving (70+) erodes to neglected (<30) after 9.6h of not visiting, and a full meter drops out of thriving after 7.2h. Arachnid+ multiplies the rate by `1 + ARACHNID_ALLY_DECAY_INCREASE (0.5)` = 50% faster, i.e. **full drain 16h** and thriving→neglected in 6.4h (§9.2 drawback) — the old baseline duration is now the subscriber's drawback duration.

- **Plain visit** (no gift): `+20` (`PLAIN_VISIT_BOOST`), resets `consecutive_gift_visits` to 0.
- **Gift visit**: boost = `round(gift.happiness_boost * max(0.2, 0.7^times_given))` — diminishing returns per repeated gift (`GIFT_DIMINISH_RATE = 0.7`, floored at `MIN_GIFT_MULTIPLIER = 0.2`, tracked per-gift via `GiftUsage.times_given`).
- **Gift streak backfire**: 3+ consecutive gift-bearing visits (`GIFT_STREAK_THRESHOLD`) without a gift-free visit in between → the *next* gift visit backfires: `-15` (`GIFT_BACKFIRE_PENALTY`) instead of the normal boost.
- **Visit duration**: `MIN_VISIT_SECONDS (30)` to `MAX_VISIT_SECONDS (180)`, scaled by how neglected the ally currently is — `round(30 + (100 - happiness)/100 * 150)`. Blocks `/patrol` for that long (shared "busy" lock with `/tutoring`).
- **No crime-level cost**: visiting used to raise city crime by `round(visit_seconds * CRIME_RISE_PER_SECOND (0.1))`. Removed 2026-08-22 — `/tutoring` is now the single source of `crime_level` and `/patrol` the single sink (§5.7). The time cost is the cost: a visit blocks `/patrol` via the shared `busy` lock, which is enough on its own without a second, less legible penalty stacked on top.
- **Biomorphic Webbing's ambient scavenge** fires here for Symbiote subscribers (2026-08-24, §9.3 item 2) — a 30% roll for one repair component (0.20 on arrival, raised 2026-08-25), with copy about the walk over to May's or MJ's. `visit_ally` takes `tier_rank` and returns `scavenged_item` on `VisitResult`; the roll happens **after** the visit's own commit, so a bonus can never roll back a completed visit. **A giftless visit is free and still rolls** — see §9.3 item 2 on why this is the perk's fastest farm and what bounds it.

**Thresholds & payoffs**: `THRIVING_HAPPINESS_THRESHOLD = 70` (both allies) → `+20%` reputation XP (`XP_BONUS_MULTIPLIER`) on patrol/tutoring. `LOW_HAPPINESS_THRESHOLD = 30` (either ally) → `-20%` earnings (`EARNINGS_PENALTY_MULTIPLIER`) on Bugle sales and tutoring cash, AND doubles the related ally-hazard's chance by `NEGLECT_HAZARD_MULTIPLIER (2.5)`.

---

## 13. Tutoring (`/tutoring`)

`services/tutoring_service.py`. Safe, steady cash — locks `/patrol` for `TUTORING_LOCK_SECONDS = 2 minutes` (shared "busy" system with `/ally visit`). Base: `cash = round(rand_range([80,140]) * reputation_multiplier * earnings_penalty_multiplier)`, `xp = round(rand_range([10,20]) * ally_xp_multiplier)`, `crime_rise = rand_range([8,15])` — **the only thing in the game that raises `crime_level`** (§5.7). A 12% "jam" event (`JAM_CHANCE`, shared pattern with Bugle) can fire: handled cleanly by an equipped gadget → +$15-35 bonus; unhandled → `+10` extra crime_level (`JAM_PENALTY_CRIME_RISE`) and no bonus. **Biomorphic Webbing's ambient scavenge** also fires here for Symbiote subscribers (2026-08-24, §9.3 item 2) — `run_tutoring_session` takes `tier_rank` and returns `scavenged_item`, rolled after the session's own commit, with copy about the trip to the study session.

---

## 14. Daily Bugle (`/bugle`)

`services/bugle_service.py`. Sells every banked `PendingPhoto` at once. `BUGLE_COOLDOWN_SECONDS = 60`. Per-photo payout: `round(rand_range(bugle_payouts[quality]) * jjj_multiplier_roll * reputation_multiplier * earnings_penalty_multiplier)`, where `bugle_payouts = {bronze:[50,150], silver:[150,350], gold:[300,600]}` and `jjj_multiplier` (JJJ's haggling) is `rand_float_range([0.8, 1.3])`, rolled independently per photo. Same 12% jam mechanic as tutoring: handled → +$15-35; unhandled → lose `JAM_LOSS_FRACTION (0.2)` of the total sale. **Biomorphic Webbing's ambient scavenge** fires here too for Symbiote subscribers (2026-08-24, §9.3 item 2) — `submit_photos` takes `tier_rank` and returns `scavenged_item`, rolled after the sale commits, with copy about the walk to the Bugle. Note this is a *different* Biomorphic roll from the bonus-photo one in §9.3: that one happens on patrol and gives you an extra photo to sell, this one happens when you sell and gives you a component.

---

## 15. Shakedown / PvP (`/shakedown`)

`services/shakedown_service.py`. `SHAKEDOWN_COOLDOWN_SECONDS = 2 min` (attacker), `TARGET_PROTECTION_SECONDS = 2 min` (victim can't be re-targeted right after). Minimum target wallet to be worth attempting: `MIN_TARGET_WALLET = $50`.

- `success_chance(target_wallet) = max(0.15, 0.65 - min(0.45, (wallet/4000) * 0.45))` — bigger scores are harder to pull off (`BASE_CHANCE=0.65`, `MAX_WALLET_PENALTY=0.45`, `WALLET_PENALTY_SCALE=4000`), floored at 15%.
- On success: steal `round(target.wallet * rand_float_range([0.10, 0.25]))` (`STEAL_PERCENT_RANGE`) from the *wallet only* (bank is always safe).
- On failure: attacker pays `rand_range([20, 60])` (`FAIL_PENALTY_RANGE`).
- **Stealth Mode** (Symbiote perk, §9.3) checked first — if active, attempt fails with zero penalty and distinct flavor text.
  - **The one place in the codebase where a perk belongs to somebody other than the reader**, and the one deliberate inversion of §9's attribution rule: the badge on that refusal is the **target's** subscription, not the invoker's. That's why `ShakedownResult` carries a `target_tier_rank` field — the cog physically cannot look it up off `ctx.author`. It's returned on the result rather than re-read in the cog because `get_tier_rank` consults the Patreon cache, which the background re-check can move between the gate and the render; the badge has to name the rank the gate actually judged. Populated on **both** branches, because a field set on only the protected path is a trap for the next caller. **Do not "fix" this to the invoker's rank** — the thief's tier has no bearing on whether Stealth Mode fires, and `scratch/check_symbiote_icons.py` asserts against exactly that regression.
  - The thief still pays the **cooldown** on this path (`pvp_cog` sets it unconditionally, above the branch), which is the reason the refusal has to be attributed at all: an unexplained refusal that burns a cooldown and moves no cash reads as a bug rather than as somebody else's perk.

---

## 16. Suit Repair (`/workbench repair`)

`services/suit_service.py`. `REPAIR_COST_PER_POINT = $6` per missing integrity point. Requires 1x `spandex_fabric` always; if missing ≥ `ELECTRONICS_THRESHOLD (50)` points, also requires 1x `micro_electronics`. Fully restores to 100%. Blocked entirely if `eviction_meter >= 100` (§8). A post-patrol warning (`repair_readiness_warning`) surfaces once `suit_integrity <= LOW_SUIT_WARNING_THRESHOLD (30)` if the player lacks the components they'd need.

- **Symbiote subscribers see the whole thing reskinned** (2026-08-24, §9.3 item 5) — `/workbench` reads as "The Bond", and it's the symbiote *demanding* components rather than Peter patching fabric. `repair_suit` and `repair_readiness_warning` take `tier_rank` as a parameter (house pattern, fail-closed default), and **every number and gate is byte-identical at every rank** — same cost per point, same component requirements, same threshold, same refusal conditions. `scratch/check_symbiote_repair.py` runs the identical scenario at all three ranks and asserts one distinct outcome across them, so a copy change can never quietly become a mechanical one.
- **"Suit Integrity" is deliberately not reskinned.** It's the shared stat name and also renders in `/balance`, the boss gate and `/admin profile` — renaming it in one panel of four would make one number look like two stats.

---

## 17. Trade Post (`/market`)

`services/market_service.py`. Player-to-player listings, items held in **escrow** (removed from seller's inventory the moment listed, not on sale). `MAX_ACTIVE_LISTINGS_PER_USER = 10`. `NOT_TRADEABLE_CATEGORIES = ("tool", "gadget")` — tools/gadgets track equip state and per-copy durability/upgrades, not tradeable through this system. Selling uses a modal (`SellModal`) for quantity + price input. No platform fee — full `quantity * price_per_unit` goes to the seller on `/market buy`. Cancelling refunds the item to the seller's inventory.

---

## 18. Items Catalog (`data/items.json`)

| Key | Category | Price | Notes |
|---|---|---|---|
| `camera` | tool | $150 | Starter item, auto-equipped on first contact (`get_or_create_user`). Durability 100. Part of `CAMERA_FAMILY_KEYS` (§6.2). |
| `camera_silver` | tool | $1,000 | Arachnid+-gated (§6.2). Installing it **deletes** every lower-tier camera body you own. |
| `camera_gold` | tool | $3,000 | Symbiote-gated (§6.2), top of the family. Same destructive install; a *downgrade* purchase back to Silver is refused. |
| `spandex_fabric` | component | $80 | Suit repair; also patrol scavenge drop. |
| `micro_electronics` | component | $150 | Suit repair (heavy damage); also patrol scavenge drop. |
| `web_fluid_vial` | collectible | not sold | Brewed only (`/lab brew`), or Trade Post. |
| `unstable_web_fluid` | collectible | not sold | Flex-only, no mechanical effect. Brew mutation or daily jackpot pull. |
| `gift_flowers` | gift | $25 | +25 happiness base. |
| `gift_dinner` | gift | $60 | +40 happiness base. |
| `gift_jewelry` | gift | $150 | +70 happiness base. |
| `web_shooters` | gadget | $200 | Unlock lvl 1. |
| `web_grabber` | gadget | $350 | Unlock lvl 5. |
| `ricochet_web` | gadget | $500 | Unlock lvl 10. |
| `upshot` | gadget | $650 | Unlock lvl 15. |
| `concussion_burst` | gadget | $900 | Unlock lvl 20. |
| `spider_bots` | gadget | $550 | Unlock lvl 8. Arachnid+-gated (§6.1). |
| `electric_webbing` | gadget | $750 | Unlock lvl 14. Arachnid+-gated (§6.1). |

---

## 19. Shop, Gadgets, Bank, Inventory & Leaderboard commands

### 19.1 `/shop` (`services/shop_service.py`, `cogs/shop_cog.py`)
`list_shop_items()` = every `Item` row where `price IS NOT NULL` (i.e. every item in §18's table — `web_fluid_vial`/`unstable_web_fluid` are excluded, they're never shop-sellable, only brewed/dropped).

Three commands:
- **`/shop list`** — paginated read-only catalog, split into the same 3 sections `/shop browse` uses (Gear = tool+component, Gifts, Gadgets). Locked gadgets (reputation level too low) show a locked-icon note instead of a price — visible to everyone, never hidden, so non-qualifying players see what they're missing. Patreon-gated items (`GATED_ITEM_MIN_RANK`) show a "Patreon exclusive" branding line ahead of the description, badged with the gating tier's emoji — still fully visible and priced, only the *purchase* is blocked (see below).
- **`/shop browse`** — interactive `ShopBrowseView`: Prev/Next flips between the 3 sections, a `Select` picks an item within the section, a `Buy` button purchases in place and re-renders with a result banner. Locked gadgets are filtered out entirely here (not just marked) since there's nothing useful to select; Arachnid+-gated items stay selectable (same branding note shown when selected) so the buy attempt itself surfaces the subscribe message.
- **`/shop buy <item>`** — direct one-shot buy via autocomplete (`shop_item_autocomplete`, live-queries the catalog, excludes anything locked for the calling user — Arachnid-gated items still included, same reasoning as browse).

**`buy_item(session, user, item_key)` purchase logic** — this is the one real chokepoint for every purchase:
1. Item must exist and have a price (else "not sold here").
2. Gadgets: blocked if `unlock_level` isn't met yet.
3. Patreon-gated items (`GATED_ITEM_MIN_RANK`, independent of category): blocked unless `get_tier_rank(...) >= min_rank` for that specific key, with a "subscribe and /patreon link" message that names the required tier via `patreon_service.tier_requirement_label()` — `"Arachnid+"` for anything below the top rank (higher tiers satisfy it), plain `"Symbiote"` for the top rank, since `"Symbiote+"` would imply a tier that doesn't exist.
4. Tools: blocked if you already own an *equipped* copy of that exact key — you can't double-buy a working camera. (If your existing copy is unequipped/broken, buying replaces it in place — same row, quantity reset to 1, durability reset to max, re-equipped.) Camera-family tools (`CAMERA_FAMILY_KEYS`, §6.2) are the one exception where buying a *different* key still succeeds — but only **upward**: an *upgrade* goes through and **deletes** every lower-tier body you own, and a **downgrade is refused outright** with the price it would have destroyed, because installing is destructive (2026-08-24). Nothing in the shop can lose you a better camera.
5. Wallet must cover `item.price` (bank funds are **not** touched by `/shop buy`).
6. On success: `add_wallet(-price)`, then branch by category —
   - **tool**: `install_tool()` — upsert a single `InventoryItem` row per item_key, always `equipped=True`, `quantity=1`. Camera-family purchases additionally **delete** every lower-tier camera-family row first (§6.2). Same helper the admin grant uses; there is deliberately only one.
   - **gadget**: always **inserts a brand-new row** — quantity 1, `equipped=False`, `durability=max`, `upgrade_level=0` — never merges into an existing stack. This is deliberate: each gadget copy tracks its own durability/upgrade level, and buying a spare after one breaks has to be a real, distinct purchase.
   - everything else (components, gifts): stacks via `add_item()` (`services/inventory_service.py`).

### 19.2 `/gadget` (`services/gadget_service.py`, `cogs/gadget_cog.py`)
Covers everything not already in §6 (proc chances, wearout, upgrade cost formula):
- **`/gadget status`** — read-only list, split into Equipped vs In Storage. A tier-locked gadget gets an `Inactive` subtext line plus a footer (§9.1.2).
- **`/gadget panel`** — interactive `GadgetPanelView`: a `Select` of every *distinct* owned gadget key (deduped to the "best copy" — highest upgrade level, then highest durability, matching the same convention `list_all_owned_gadgets` uses for boss fights), plus Equip/Unequip/Upgrade buttons that act on whichever copy is selected and re-render in place. A tier-locked selection adds a `Status` field and **disables Upgrade only** — Equip/Unequip stay live, since freeing the slot is the one useful action.
- **`/gadget equip <gadget>`** / **`/gadget unequip <gadget>`** — direct commands, same underlying `equip_gadget`/`unequip_gadget` as the panel. Equip fails if both of the `MAX_EQUIPPED_GADGETS (2)` slots are already full, or if you don't own an unequipped copy, or if you haven't hit the gadget's `unlock_level` yet.
- **`/gadget upgrade <gadget>`** — must already be equipped (upgrading only ever applies to the equipped copy). Fails past `MAX_UPGRADE_LEVEL (3)`, on insufficient wallet cash, or if the gadget is tier-locked (§9.1.2 — it's a cash spend on something that can't fire). Cost formula and per-gadget bonus/level table are in §6.

### 19.3 `/lab` (Chem Lab — `services/brewing_service.py`, `cogs/lab_cog.py`)
Thin wrapper over §11's brewing mechanics: `/lab status` (time remaining or "ready"), `/lab brew` (starts a batch, fails if one's already active or wallet's short of $30), `/lab collect` (fails if nothing's brewing or it's not ready yet — `/admin bypass` skips the ready-time check for testing).

### 19.4 `/workbench` (Suit repair — `services/suit_service.py`, `cogs/suit_cog.py`)
`/workbench status` shows current integrity, full-repair cost breakdown, and components on hand. `/workbench repair` executes it — see §16 for the cost/component formula. Both are blocked (with an explicit message) if `eviction_meter >= 100` (§8).

### 19.5 `/balance`, `/inventory`, `/bank` (`cogs/economy_cog.py`)
- **`/balance`** — wallet, bank/capacity, reputation level+XP, suit integrity. Read-only.
- **`/inventory`** — every owned `InventoryItem` row, grouped by category (gadget, then tool, then component, then collectible, then gift, then any other category alphabetically after).
  - **Gadgets show "— Equipped" / "— Stowed"; other equippable items show "— Equipped" and stay silent when they aren't.** Gadgets get the extra word because there are only `MAX_EQUIPPED_GADGETS (2)` slots, so which copies are live is the thing you opened the list to check. This **replaced "Battle-Grade"/"Swinging-Grade" on 2026-08-23** — those read as *item quality grades* rather than slot states, which is wrong twice over: no such grades exist in the catalog, and Spider Bots are combat gear whether or not they're in a slot, so "Swinging-Grade Spider Bots" described the wrong thing entirely.
  - **Render-identical rows are folded into one line with their quantities summed.** Gadget purchases are never merged in the database (`shop_service` gives each buy its own `quantity=1` row, and §19.2's Select dedupes to a best copy rather than the table doing it), so two spare Spider Bots printed two byte-identical bullets — and losing one to a break left a list that looked **completely unchanged**. That is the actual bug behind "my Spider Bots broke and the inventory still shows two." The fold key is `(rendered name, durability)`, and the name carries `(Lv. N)` for any upgraded copy, so copies differing in anything a player can act on still get their own line.
  - **`— Inactive`** marks an item whose Patreon tier has lapsed (`patreon_service.locked_item_keys`), plus a footer explaining it. This is the **only** surface that explains a revoked *camera* — cameras have no `/gadget status` equivalent of their own. See §9.1.2.
- **`/bank deposit <amount>`** / **`/bank withdraw <amount>`** — moves cash between wallet (stealable via `/shakedown`) and bank (safe, capped at `bank_capacity`, §7). Deposit auto-expands capacity if needed (§7); withdraw has no such logic since the bank can only ever hold up to its own capacity.

### 19.6 `/leaderboard` (`services/leaderboard_service.py`, `cogs/leaderboard_cog.py`)
Three categories, top 20 each (`LEADERBOARD_LIMIT = 20`), paginated 5-per-page:
- **Wealth**: sorts by `wallet + bank` combined.
- **Reputation**: sorts by raw `reputation_xp` (not derived level) so ties within a level still break sensibly.
- **Daily Streak**: sorts by *current* `daily_streak`, not `daily_longest_streak` — deliberate, since only the current streak has real ongoing stakes (miss a day, drop off the board).

If the calling user isn't in the visible top-20 list, a footer line shows their real rank (`get_rank` — 1-based standard competition ranking: count of users strictly above them, +1) and value, computed via a single `COUNT(*) WHERE column > my_value` query rather than fetching the whole table.

### 19.7 `/admin` (owner/granted-admin only — `cogs/admin_cog.py`, `services/admin_service.py`)
Not player-facing (deliberately excluded from `/help`). Full command groups: `economy` (give-cash, set-bank-capacity), `profile` (set-reputation, set-boss-clears, set-suit, set-crime-level, set-eviction, set-rent-due, set-streak), `inventory` (give-item, remove-item, set-durability, set-upgrade), `cooldown` (reset, set), `ally` (set-happiness, reset), `brew` (force-ready, clear), `market` (delete-listing), `admins` (add/remove/list — runtime-grantable access on top of the hardcoded root `ADMIN_DISCORD_IDS`), plus general tools: `bypass`/`bypass-status` (cooldown+brew-time skip for testing), `user-info` (full profile dump with live cooldowns), and **`/admin wipe`** — the one truly destructive command, requiring a second confirm-button click, permanently erasing a target's entire profile via `wipe_user()` (§7, §20.12).

---

## 20. Full Database Schema

All tables live in `db/models.py`, one SQLAlchemy `Base` (with `AsyncAttrs` so relationships can be lazy-awaited via `.awaitable_attrs.x`). The economy is **global** — no per-guild/per-server scoping anywhere; `discord_id` alone is the identity key. This section is the authoritative column-by-column reference — treat it as the source of truth for "will this data survive," not assumption.

### 20.1 `users` — one row per player, created on first contact
| Column | Type | Default | Notes |
|---|---|---|---|
| `discord_id` | BigInteger | — | **Primary key.** |
| `wallet` | Integer | 0 | Stealable cash. |
| `bank` | Integer | 0 | Safe cash. |
| `bank_capacity` | Integer | 5000 | Auto-expands, §7. |
| `reputation_xp` | Integer | 0 | Drives level (§3). |
| `suit_integrity` | Integer | 100 | 0-100. |
| `crime_level` | Integer | 0 | 0-100 city heat (§5.4). |
| `boss_clears` | Integer | 0 | Boss gates cleared (§4). |
| `eviction_meter` | Integer | 0 | 0-100 (§8). |
| `next_rent_due` | DateTime | now+7d | Indexed — the rent scheduler queries `WHERE next_rent_due <= now`. |
| `created_at` | DateTime | now | Set once, never touched again. |
| `daily_streak` | Integer | 0 | (§10) |
| `daily_longest_streak` | Integer | 0 | Never decreases. |
| `daily_last_claimed` | DateTime? | null | |
| `last_active_at` | DateTime? | null | Stamped every command (Stealth Mode, §9.3). |

Relationships: `inventory_items` and `pending_photos` cascade `all, delete-orphan` — deleting a `User` row (only ever via `wipe_user`, never any other path) auto-deletes these two automatically through the ORM. Every *other* table referencing a user (`Cooldown`, `Ally`, `GiftUsage`, `Brew`, `MarketListing`, `PatreonLink`) does **not** have a cascade relationship defined — `wipe_user()` deletes those explicitly table-by-table (see §20.12) specifically because ORM cascades weren't reliable enough for every deletion path here to depend on.

`get_or_create_user()` (`services/economy.py`) is the only creation path — also grants a starter `camera` (equipped, full durability) on first-ever contact.

### 20.2 `items` — static catalog, seeded once from `data/items.json`
| Column | Type | Notes |
|---|---|---|
| `key` | String | **Primary key.** Matches `data/items.json`'s `"key"` field 1:1 — this is the stable identifier used everywhere else (inventory rows, market listings, gift usage, admin commands). |
| `name` | String | Display name. |
| `category` | String | tool \| consumable \| component \| collectible \| gadget \| gift. |
| `description` | String | Default empty string. |
| `max_durability` | Integer? | null = doesn't wear out (components, gifts, vials). |
| `price` | Integer? | null = **not sold in `/shop`** (this is exactly how `web_fluid_vial`/`unstable_web_fluid` are excluded — see §19.1). |
| `happiness_boost` | Integer? | Gifts only. |
| `unlock_level` | Integer? | Gadgets only — min reputation level to buy/equip. |

Never written to at runtime by normal gameplay — this table is effectively read-only application data, refreshed only by re-running the seed step against `data/items.json`. Safe to treat as config, not player data.

### 20.3 `inventory_items` — every owned copy of every item
| Column | Type | Notes |
|---|---|---|
| `id` | Integer | Autoincrement PK. |
| `user_id` | BigInteger FK→users | |
| `item_key` | String FK→items | |
| `quantity` | Integer | Default 1. Stackable items (components, gifts, vials) live as **one row per item_key**, quantity incremented/decremented (`inventory_service.add_item`/`remove_item`) — a stack hits 0, the row is deleted outright, not left at 0. |
| `durability` | Integer? | null for non-decaying items. |
| `equipped` | Boolean | Default False. |
| `upgrade_level` | Integer | Default 0. Gadgets only. |

**Gadgets are the one exception to stacking**: every gadget purchase inserts a brand-new row (never merged) so each physical copy tracks its own durability/upgrade_level independently — see §19.1. This is why `list_all_owned_gadgets`/`_owned_copies` order by `(item_key, upgrade_level desc, durability desc)` and dedupe to the "best copy" per key rather than assuming one row per key.

Two consequences that both bit on 2026-08-23, worth knowing before touching either path:
- **A stacked gadget row can still arrive** — via `/admin` grants or seeded data, never via `/shop`. `roll_gadget_wearout` used to `session.delete()` the row on a break, destroying every copy at once; it now decrements when `quantity > 1`. The bug was invisible for exactly the reason it was dangerous: only admin-granted stacks could hit it, so nobody would have connected the loss to a break.
- **Two identical unmerged rows render identically.** Owning two spare Spider Bots meant `/inventory` printed two byte-identical bullets, so losing one to a break left a list that looked unchanged — which reads as "the break didn't register" rather than "you had two." §19.5 folds render-identical rows and sums their quantities.

### 20.4 `pending_photos` — captured-but-unsold Bugle photos
| Column | Type | Notes |
|---|---|---|
| `id` | Integer | Autoincrement PK. |
| `user_id` | BigInteger FK→users | |
| `quality` | String | bronze \| silver \| gold. |
| `created_at` | DateTime | |

Deleted individually the moment `/bugle submit` sells them (§14) — this table only ever holds the *unsold* queue, never a historical log.

### 20.5 `transactions` — append-only audit log
| Column | Type | Notes |
|---|---|---|
| `id` | Integer | Autoincrement PK. |
| `user_id` | BigInteger FK→users | |
| `balance_type` | String | wallet \| bank. |
| `amount` | Integer | The signed delta actually applied (post-clamp — see §7's `add_wallet`/`add_bank` contract). |
| `reason` | String | A `source:detail` tag, e.g. `"patrol_battle:crime_gold"`, `"shop:buy:camera"`, `"admin:123456"`. |
| `created_at` | DateTime | |

**Never deleted, not even by `wipe_user()`** — this is the one table explicitly excluded from the wipe, by design (§20.12). It's the permanent record of every cash movement that ever happened, independent of whether the user profile still exists.

### 20.6 `cooldowns` — active command locks
| Column | Type | Notes |
|---|---|---|
| `user_id` | BigInteger | Composite PK with `command_key`. |
| `command_key` | String | e.g. `"patrol"`, `"shakedown"`, `"busy:tutoring"` (the "busy" locks in `services/busy.py` are just cooldowns under a `busy:` prefix). |
| `expires_at` | DateTime | |

Purely transient/derived state — always safe to delete or reset (that's exactly what `/admin cooldown reset` does), never carries anything that needs preserving long-term.

**Two keys in here are not command locks, and both belong to `/shakedown`.** `shakedown_target` is set on the *victim* — it stops a player being hit repeatedly, and is written on every outcome including a protected one (§9.3 item 3). `stealth_dm` (added 2026-08-25) is set on the victim too, and is a **notification** throttle rather than an action lock: it caps Stealth Mode's DM to its owner at one per 15 minutes, so a run of attempts during one absence collapses into one message instead of a stack of near-identical ones. Two consequences worth knowing before touching either. Because both are keyed on the target rather than the invoker, `/admin bypass` on the *account being shaken down* is what defeats them for testing, not bypass on the tester. And because `/admin cooldown reset` clears `stealth_dm` like anything else, resetting a subscriber's cooldowns re-arms their next protection DM early — harmless, since the DM quotes an all-time total rather than a per-window count.

### 20.7 `market_listings` — Trade Post escrow
| Column | Type | Notes |
|---|---|---|
| `id` | Integer | Autoincrement PK — this is the listing ID players reference in `/market buy`/`/market cancel`. |
| `seller_id` | BigInteger FK→users | |
| `item_key` | String FK→items | |
| `quantity` | Integer | |
| `price_per_unit` | Integer | |
| `created_at` | DateTime | |

The listed items are already removed from the seller's `inventory_items` the instant a listing is created (escrow) — cancelling or an admin force-delete both refund via `add_item()` back into inventory. §17.

### 20.8 `allies` / `gift_usage` — Aunt May & MJ relationship state
`allies`: composite PK `(user_id, ally_key)`. `banked_happiness` (Integer, default 100) + `last_visited_at` (DateTime) is all that's stored — current happiness is always *computed* on read (`_decayed_happiness`), never stored directly, so nothing needs a background tick. `consecutive_gift_visits` (Integer, default 0) tracks the gift-streak-backfire mechanic (§12).

`gift_usage`: composite PK `(user_id, ally_key, gift_key)`. `times_given` (Integer, default 0) — powers the per-gift diminishing-returns formula (§12). `/admin ally reset` clears this table (and `consecutive_gift_visits`) for one ally without touching `banked_happiness` itself.

### 20.9 `brews` — one active Chem Lab batch
`id` (PK), `user_id` (FK, **unique** — enforces "one active brew at a time" at the schema level, not just application logic), `started_at`, `ready_at`. Deleted the moment `/lab collect` succeeds — this table only ever holds the *in-progress* batch, same "queue not log" shape as `pending_photos`.

### 20.10 `patreon_links` — Patreon account connection + entitlement cache
| Column | Type | Notes |
|---|---|---|
| `discord_id` | BigInteger FK→users | **Primary key** — one link per Discord account. |
| `patreon_user_id` | String | Patreon's own account ID. |
| `tier` | String? | Raw tier title string as last reported by Patreon's API. **null = linked but no active pledge** (a valid, common state — not an error). |
| `growth_perk_choice` | String? | "xp" \| "allies" \| null. Dormant field for the separate, not-yet-rebuilt Server Booster track (§9.4) — not reachable via any current Patreon command. |
| `access_token` / `refresh_token` | String | OAuth tokens. Both are rewritten by the background poll when it has to refresh — Patreon rotates the refresh token on every use, so the new pair is committed before the poll does anything else with it (§9.1.1). |
| `token_expires_at` | DateTime | |
| `linked_at` | DateTime | Set once. |
| `last_checked_at` | DateTime | Last time this link was re-synced against Patreon — by `handle_callback` on a (re-)link, or by `refresh_stale_links()` on the background poll (§9.1.1). Doubles as the poll's queue key: rows are picked oldest-first, and a *failed* check still stamps it so a broken link requeues at the back instead of being retried every tick. |

`/patreon unlink` deletes this row entirely (does not touch the actual Patreon pledge, only the bot's record of it) — matches what `PRIVACY_POLICY.md` promises.

### 20.11 `admin_users` — runtime-granted `/admin` access
`discord_id` (PK), `granted_by` (BigInteger — who granted it), `granted_at` (DateTime). Entirely separate from `config.ADMIN_DISCORD_IDS` (`.env`, root, unrevocable via command) — this table is *only* the runtime-added layer, managed via `/admin admins add/remove/list`.

### 20.11a `shakedown_attempts` — Stealth Mode threshold instrumentation
One row per `/shakedown` that reaches the resolver. Exists for exactly one question: is Stealth Mode's 20-minute inactivity threshold the right number (§9.3)? Before this, a protected attempt charged nobody and so left no trace at all — not even a `transactions` row — making the perk's firing rate unobservable.

| Column | Type | Notes |
|---|---|---|
| `id` | Integer | PK, autoincrement. |
| `thief_id` / `target_id` | BigInteger FK→users | **Two FKs to `users`** — the only table with that shape, which is why `wipe_user` orphaning is worth thinking about (§20.12). |
| `outcome` | String | `protected` \| `success` \| `caught` (`shakedown_service.OUTCOME_*`). |
| `target_idle_seconds` | Integer? | **null = the target has never run a command**, which is itself why Stealth Mode can't fire for them. Distinct from 0 ("just active"). Recorded on *every* attempt, not only protected ones — that's what makes alternative thresholds scoreable from data already collected. |
| `target_tier_rank` | Integer | The target's rank at attempt time. Separates "wasn't idle enough" from "wasn't subscribed". |
| `amount` | Integer | Cash moved (0 when protected). |
| `target_wallet` | Integer | The **pre-attempt** wallet, read before `add_wallet` runs, so a successful steal can't make the target look poorer than they were when picked. |
| `created_at` | DateTime | |

Written by `log_shakedown_attempt()` in its own session, with every exception swallowed — a telemetry write must never roll back real cash movement or fail a player's command, including when this migration hasn't been run. Read by `scratch/analyze_stealth_mode.py`. This is a **log, not state**: nothing in gameplay reads it.

### 20.12 What actually gets deleted, and when
This is the part that matters for "data doesn't get deleted" — three distinct answers depending on what's meant:

1. **Normal gameplay never deletes a `users` row.** There is no auto-expiry, no inactivity purge, nothing that silently removes a player's profile. The only two ways a `users` row disappears are (a) `/admin wipe` (destructive, requires a second confirm click, admin-only) and (b) direct manual DB surgery outside the bot entirely.
2. **`wipe_user(session, user_id)`** (`services/economy.py`) is the one function that erases a profile. It explicitly, individually deletes from `InventoryItem`, `PendingPhoto`, `Cooldown`, `Ally`, `GiftUsage`, `Brew` (all `WHERE user_id = ...`) plus `MarketListing WHERE seller_id = ...`, then deletes the `User` row itself. **`Transaction` rows are deliberately left untouched even on a full wipe** — the cash-movement audit log outlives the profile that generated it, on purpose. (Not currently wired to also delete `PatreonLink`, `AdminUser`, or `ShakedownAttempt` rows for that discord_id — worth knowing if a wipe is ever run on a linked/admin account, since those rows would become orphaned rather than cleaned up. `ShakedownAttempt` is the awkward one: it has **two** FKs to `users` (`thief_id` and `target_id`), so wiping either party orphans the row, and it's a measurement log rather than personal state — the `Transaction` precedent argues for leaving it, the same as the audit log.)
3. **Individual item-level "deletion" is normal and expected**, not data loss: an `InventoryItem` row disappears when a stack hits 0 (`remove_item`), a gadget breaks (`roll_gadget_wearout` — deletes a `quantity=1` row outright, so the gadget must be rebought; **decrements** a stacked row instead, fixed 2026-08-23 after a single break was found to wipe a `quantity=3` row down to nothing), or a camera breaks (same). A `PendingPhoto`/`Brew`/`MarketListing`/`Cooldown` row disappearing when its lifecycle completes (sold, collected, bought/cancelled, expired) is the table doing exactly what it's for — none of these are logs, they're queues of in-flight state.

### 20.13 Migration history (Alembic, `alembic/versions/`)
In order: baseline → admin_users (add table) → reputation XP curve rescale (the `LEVEL_GROWTH=1.12` accelerating curve, replacing a flat-100 system) → boss gates (add `boss_clears`, existing users grandfathered past gates they'd already cleared) → patreon_links (add table) → growth_perk_choice (add column to `patreon_links`) → last_active_at (add column to `users`, for Stealth Mode) → **shakedown_attempts (add table, `a7c41e93b508`, 2026-08-23 — Stealth Mode threshold instrumentation, §9.3)**. All eight are applied to the local dev DB (`alembic current` confirms head = `a7c41e93b508`) **and all eight files are tracked in git** (verified 2026-08-21 via `git ls-files alembic/versions/` — an earlier note here claimed two were untracked; that was stale, there is no repo/DB gap).

**Known and pre-existing: `alembic upgrade head` from a genuinely empty database does NOT work** (confirmed 2026-08-23; it fails at `44f8f54cbf0b` with `duplicate column name: boss_clears`, and has been broken since that migration was written — nothing to do with the newer ones). Cause: the baseline revision runs `Base.metadata.create_all()` against the **current** models, so a fresh DB gets every table with every modern column already present, and each later `op.add_column` then collides with a column that already exists. The supported path is the one actually used — an existing DB stamped at some revision, upgraded forward, where the baseline never re-runs. To bootstrap a new environment, let `create_all` build the schema and then `alembic stamp head`. Fixing this properly means freezing the baseline to the schema as it stood in Aug 2026 rather than tracking `db/models.py`; not worth doing until someone actually needs a from-zero migration path.


---

## 21. Local Dev Environment Gotchas (Windows)

- **Windows Smart App Control blocks a freshly-downloaded, unsigned `ngrok.exe`** outright (`WinError 4556`). No per-file exception exists once the policy's on. Fix in `cogs/tunnel_cog.py`: on Windows, check `shutil.which("ngrok")` first and reuse a `winget install ngrok.ngrok`-installed copy (a trusted channel) instead of auto-downloading. Linux deployment still auto-downloads fine, untouched.
- **ngrok's free tier shows a browser interstitial** to anonymous visitors unless the request carries `ngrok-skip-browser-warning` — real users *will* see a warning page mid-`/patreon link` requiring one extra click. Only a paid ngrok plan removes it; not fixable bot-side.
- **`ngrok config add-authtoken` hangs indefinitely** on the real Linux deployment (likely a background update-check that never returns on a network-restricted host) — `_write_config()` writes ngrok's v3 YAML config file directly instead of shelling out, sidestepping the subprocess entirely on both platforms.
- Python `logging` output was unreliable through a `Start-Process -RedirectStandardOutput/Error` + hidden-window local diagnostic setup — `log.info()` calls often silently vanished even though the underlying code worked (verified via a real `/health` HTTP hit returning 200). If a "hang" won't show any log output, verify with a real functional signal before trusting the silence as proof.
