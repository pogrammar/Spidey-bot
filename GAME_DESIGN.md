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

**Wearout**: `GADGET_BASE_BREAK_CHANCE = 0.05` per encounter while equipped/used, scaled by `min(0.9, 0.05 * difficulty_multiplier)` — uses the **raw**, uncapped difficulty, not the combat-soft-capped one. Breaking deletes the `InventoryItem` row outright (must be rebought).

Passive contexts (`/tutoring`, `/bugle submit` "jam" events) call `roll_gadget_effect`/`roll_gadget_wearout` with `gadget_key=None`, which picks randomly among whatever's equipped rather than letting the player choose.

### 6.1 Arachnid+ Patreon-exclusive gadgets — Spider Bots & Electric Webbing
Mechanically **ordinary gadgets** — same `"gadget"` category, same shop/equip/upgrade path, same `GADGET_EFFECTS`-driven Select/button flow in battle as the five in the table above (including normal wearout via `roll_gadget_wearout`, since they go through `resolve_gadget` like everything else). The only thing different about them is **purchase is gated**: `shop_service.GATED_ITEM_MIN_RANK = {"spider_bots": TIER_RANK_ARACHNID, "electric_webbing": TIER_RANK_ARACHNID, "camera_silver": TIER_RANK_ARACHNID}` — `buy_item` looks the item's key up in that map and refuses anyone below the mapped rank with a "subscribe and /patreon link" message. Everyone still **sees** them in `/shop list`/`/shop browse` at full price (same visibility as a reputation-locked gadget, plus an inline 🕷️ "Patreon exclusive" branding note in `/shop browse`'s selected-item view) — only the purchase itself is blocked.

(Earlier this session these briefly existed as always-on, tier-gated *passive* procs with no equip step at all — the original always-on shape §9.2 used to describe. That was reworked into the ordinary-gadget shape described here per explicit direction, since a passive/no-action version meant no button ever showed up for them in a patrol battle, which read as broken/missing rather than intentional.)

| Gadget | Unlock lvl | Price | GADGET_EFFECTS kind | base_chance | bonus/level | Notes |
|---|---|---|---|---|---|---|
| Spider Bots | 8 | $550 | `bonus_damage` | 0.45 | +0.13 | Normal attack roll + `[5,12]` bonus damage; counter still resolves normally (no defensive component). |
| Electric Webbing | 14 | $750 | `shock_burst` | 0.45 | +0.145 | Normal attack roll + `[8,15]` bonus damage, AND fully blocks the counter for that round (same "no counter" shape as Concussion Burst's `group_defense`). |

Trigger chance formula is the same `min(0.9, base_chance + bonus_per_level * upgrade_level)` as the table above.

**Both were re-rated 2026-08-22 (0.20/+0.12 → the numbers above), per direct player-facing feedback that they "fail WAY too often, it's like they don't even work."** The original rates were chosen by comparison alone (explicitly *below* the free ladder's 0.25–0.55 baseline, never simulation-verified) as an anti-pay-to-win guard. They overshot: at 0.20 base and 0.56 fully upgraded, these were the **least reliable gadgets in the game** outside the deliberately-exempt Web Shooters, and Spider Bots cost **more** than Ricochet Web ($550 vs $500) while firing at **under half** its rate. They also broke §6's own documented convention that `bonus_per_level` scales with unlock level — a flat +0.12 at unlock 8/14, where unlock 10 gets +0.14.

The correction is a change of *which lever* guards the tier, not a removal of the guard: what keeps a paid gadget from being pay-to-win is a modest **effect**, not unreliable **delivery**. Their effects are flat damage adds (`[5,12]`/`[8,15]`) — the weakest kind in the table, below Ricochet Web's scavenge boost and Upshot's +50% XP — and `base_chance` in this codebase is rated against effect *strength*, not unlock level (that's why Web Grabber sits at 0.55 and Upshot at 0.30). So they belong at the reliable end. Resulting ladder, L0→L3: Spider Bots **45/58/71/84%**, Electric Webbing **45/59.5/74/88.5%** — seated just above Ricochet Web's 0.44 base, under Web Grabber's 0.55, and Concussion Burst keeps the 90% ceiling. `MAX_EQUIPPED_GADGETS = 2` means bringing both still costs the entire loadout.

### 6.2 Camera tiers — base camera & Silver-Grade Camera (Arachnid+ Patreon-exclusive)
Implemented as **separate `"tool"` items**, not the `upgrade_level`-on-one-item design §9.5 originally sketched (that design is superseded by this — kept simpler to ship and to show up as a real second entry in `/shop browse`'s Gear section next to the base camera, per explicit direction). `patrol_service.CAMERA_FAMILY_KEYS = ["camera", "camera_silver"]` (ordered lowest to highest tier).

- **Equip exclusivity**: buying either camera in the family auto-unequips whatever other camera-family tool was equipped before (handled in `shop_service.buy_item`'s tool branch) — only one is ever equipped at a time. `get_equipped_camera` (`patrol_service.py`) now searches the whole family and returns the highest-tier equipped row.
- **`camera_silver`** — $1,000, Arachnid+-gated (`GATED_ITEM_MIN_RANK`), same durability/break-on-delete shape as the base camera. `patrol_service.CAMERA_TIER_STATS["camera_silver"] = {"break_chance_reduction": 0.70, "quality_bump_chance": 0.60}`:
  - **Break-chance reduction**: `battle_service.finalize_battle` multiplies the existing break-chance formula (§5.5) by `(1 - break_chance_reduction)` before rolling.
  - **Quality-bump chance**: `0.60` — **"3 in 5 times"**, set 2026-08-21, replacing the 0.325 that shipped as the midpoint of §9.5's original "30-35%" range. This one is **copy, not a balance knob**: the subscriber is told "3 in 5" outright, so it stays exactly 0.60 and the promise changes before the number does. Independent roll (`patrol_service.bump_photo_quality`) bumping the banked photo up one tier (bronze→silver→gold, capped at gold) *before* it's banked.
  - **Attribution** (implemented 2026-08-21): `BattleReport.photo_quality_bumped` / `.photo_quality_before_bump` carry it out of `finalize_battle`, and `patrol_cog._render_final` renders a dedicated **"🕷️ Photo Upgraded"** row spelling out `Bronze → Silver`. `photo_quality_before_bump` is only set when the tier **actually changed** — on a gold-tier encounter the roll still fires but can't go higher, and the card must not claim an upgrade that didn't happen (verified: 0 false flags in 150 gold encounters).
  - **Biomorphic Webbing's bonus photo** (§9.3) now takes **its own independent bump roll** off the same camera (changed 2026-08-21; it previously banked at the encounter's raw un-bumped quality). Same fight, same camera, two photos — the lens doesn't stop working for the second shot. Rolled separately rather than copying the first photo's result, so the two can legitimately bank at different qualities. This is a small **buff to the Symbiote tier**; one `if` in `finalize_battle` reverts it.
- Camera **Gold** tier shipped 2026-08-22 — base + Silver + Gold all exist. Gold is gated to the **Symbiote** tier (decided 2026-08-21), a step above Silver's Arachnid+ gate. Its bump chance is **0.80 — "4 in 5"** (decided 2026-08-22; this **closed** the inverted-ladder question that was open here). Like Silver's 0.60 it is **copy, not a balance knob** — "4 in 5" deliberately parallels Silver's "3 in 5" so the two read as one ladder, while staying visibly short of a guarantee.

**Locked camera ladder** (both knobs, all three tiers):

| Tier | Break-chance reduction | Photo-bump chance | Price | Gate |
|---|---|---|---|---|
| `camera` | 0% | 0.00 | $150 | none (starter) |
| `camera_silver` | -70% | 0.60 ("3 in 5") | $1,000 | Arachnid+ |
| `camera_gold` | -85% | 0.80 ("4 in 5") | $3,000 | Symbiote |

Neither knob ever reaches its extreme (-100% / 1.00) at any tier, on purpose: camera-break tension and the value of a genuine gold-crime photo both have to survive the top tier.

**Gold's gate is no longer blocked — and Gold is now built (2026-08-22).** Purchase gating used to be a flat `ARACHNID_GATED_ITEM_KEYS` set behind a single `>= TIER_RANK_ARACHNID` check, which had no way to express "Symbiote only" — so Gold couldn't be gated correctly no matter how it was built. That set is now `shop_service.GATED_ITEM_MIN_RANK`, a per-key `{item_key: min_rank}` map, and `"camera_gold": TIER_RANK_SYMBIOTE` is the whole gate. `GATED_ITEM_KEYS` is derived from the map for the callers that only ask "is this gated at all?", so no second list has to be kept in step.

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

Helpers: `battle_service._arachnid_tag()`, `._symbiote_tag()`, and `._gated_gadget_tag(key)`. All three return `""` when the emoji is missing rather than raising, so a not-yet-uploaded emoji degrades to an untagged line instead of a broken fight.

**Audit 2026-08-21 — the rule was aspirational; it is now implemented.** An audit found that of ten perk trigger points, only Enhanced Strength complied. Every gap below except the last was closed the same day:

| Perk | Where | Status |
|---|---|---|
| Enhanced Strength | `battle_service.resolve_attack` | ✅ was already compliant — the reference pattern |
| Silver camera photo bump | `finalize_battle` → `patrol_cog._render_final` | ✅ fixed — dedicated "Photo Upgraded" row, §6.2 |
| Venom Blast | `_apply_counter_with_venom_blast` | ✅ fixed — `_symbiote_tag()` on the blast line |
| Sonic Dampener | `_apply_counter_with_venom_blast` | ✅ fixed — emitted **no line at all** while silently multiplying incoming damage by 1.3. See §9.3 |
| The suit overrides you | `battle_service.resolve_evade` | ✅ compliant from the start — `SYMBIOTE_OVERRIDE_LINES` + `_symbiote_tag()`. A drawback, so the line is doing double duty: without it, an ignored Dodge reads as a bug rather than as the tier's stated cost |
| Biomorphic cash | `finalize_battle`, `finish_noncombat_patrol` | ✅ fixed — `biomorphic_cash` on both `BattleReport` and `PatrolResult`, rendered as subtext under Cash |
| Biomorphic component | `finalize_battle` | ✅ fixed — was byte-identical to a base-game drop; now a subtext line under Scavenged |
| Biomorphic bonus photo | `finalize_battle` | ✅ fixed — "Second Shot" row, and it now gets its own bump roll (§6.2) |
| Spider Bots / Electric Webbing | `resolve_gadget` | ✅ tagged via `_gated_gadget_tag()`. A **taste call**, not a rule requirement — no tier check runs at use time and a lapsed subscriber keeps firing the ones they bought, so what's being attributed is *owning* them. Drop the one call in `resolve_gadget` to revert |
| Combat-Ready Patrols | `patrol_service._roll_patrol_outcome` | ⚠️ **accepted gap** — a weight bonus applied *before* any outcome exists, so there is no moment to attribute. Naming it would mean claiming a specific encounter was caused by the perk, which isn't knowable |

The sweep also fixed a real numeric bug it surfaced: Sonic Dampener multiplied the counter *inside* `_apply_counter_with_venom_blast` while all four call sites printed the **pre-multiplier** roll, so a dampened hit displayed less suit damage than it actually dealt. The helper now returns a `CounterOutcome` carrying the actually-applied `damage`, and every call site formats from that.

### 9.1 OAuth linking flow
`/patreon link` → `build_authorize_url()` generates a random `state` token (kept in an in-memory `_pending` dict, 10-min TTL — a bot restart mid-link just means re-running the command, an acceptable tradeoff to avoid a throwaway DB table) → sent as a DM (or inline if already in DMs, or inline as fallback if DMs are closed) with a link-style button → Patreon redirects to `/patreon/callback` (served by the shared aiohttp app) → `handle_callback()` exchanges the code, fetches identity **with `fields[tier]=title`** (a v2 API quirk: without this explicit field request, a resource is stripped to just `type`+`id`, and tier detection silently breaks — this was a real bug found and fixed 2026-08-18) → upserts `PatreonLink` → sends a tier-specific welcome DM (`ARACHNID_INTRO`/`SYMBIOTE_INTRO`/`NO_PLEDGE_INTRO` in `patreon_cog.py`).

`/patreon status` shows the raw Patreon-reported tier string plus the resolved perk-tier label. `/patreon unlink` deletes the DB row only — does not touch the actual Patreon pledge.

`/patreon perks` is the richer companion to `/patreon status` — a plain-English checklist of every perk actually active for the caller right now (built from `ARACHNID_PERKS`/`SYMBIOTE_PERKS` plus the `ORGANIC_WEBBING_LINE`/`BIOMORPHIC_WEBBING_LINE` pair in `patreon_cog.py`, gated by the same `tier_rank` comparisons as everything else), plus an ownership check (owned vs. not-owned, per `InventoryItem`) for the gated purchasable items since a tier only unlocks the *ability* to buy those, it doesn't grant them. That checklist iterates `shop_service.GATED_ITEM_MIN_RANK` directly and filters to `tier_rank >= min_rank`, so a newly gated item appears without a second edit and an Arachnid subscriber is never shown a Symbiote-only item under a heading that reads "Yours to Buy" (`patreon_cog.GATED_ITEM_LABELS` supplies only the wording, and falls back to the raw key if someone forgets to add one). Those same lists are the single source for the post-link welcome DM, so the two surfaces cannot drift.

#### 9.1.1 Tier re-checking — implemented 2026-08-23
**The hole this closed:** a tier was written once at link time and *never read again*. Cancelling a pledge kept every perk forever, and the `PatreonLink` docstring claimed a periodic re-check that did not exist. `refresh_stale_links()` + `SchedulerCog.patreon_tick` are that re-check.

- **The one rule: only a successful identity read may rewrite a stored tier.** Every other path leaves it exactly as it was. This is not defensive coding, it's the actual product decision — a Patreon 503 must never take perks away from someone who is paying. The direction of error is deliberate: a lapsed pledge keeps its perks for up to one extra `REFRESH_STALE_AFTER` window, which is the correct side to be wrong on.
- **Why a `RefreshOutcome` dataclass and not a `str | None` return.** The whole contract hinges on the difference between *"they have no pledge"* and *"we couldn't tell"* — both of which are `tier=None`. `reached_patreon` is what separates them, and a bare tier return literally cannot express it. That distinction is the fail-open guarantee; everything else is bookkeeping.
- **Cadence.** `PATREON_TICK_INTERVAL_MINUTES = 15` drains the queue, `REFRESH_STALE_AFTER = 6h` decides what's *in* it, `REFRESH_BATCH_SIZE = 25` caps one tick. These are three separate knobs on purpose: the tick is drain speed, the staleness window is per-link re-read frequency. 25 × 96 ticks/day = 2,400 checks, which re-reads every subscriber many times over at any realistic count while staying far under Patreon's rate limits. Rows are taken **oldest-`last_checked_at` first**, so nothing can be starved.
- **A failed check still stamps `last_checked_at`.** Without that, a permanently-failing link sits at the head of the queue and gets retried every single tick forever, hammering Patreon and starving everyone behind it. The tier is untouched, so the fail-open guarantee is unaffected — only the retry cadence changes. The one exception is missing local credentials (`PATREON_CLIENT_ID`/`SECRET` unset), which doesn't stamp at all: that's a misconfiguration rather than an attempt, and the queue should still be in its true order once it's fixed.
- **Transient vs. dead, and why "dead" is allowed to remove perks.** `_TransientPatreonError` (network error, timeout, 5xx, 429, malformed payload) fails open. `_DeadLinkError` — a **4xx on the refresh grant**, i.e. Patreon authoritatively saying this grant is gone — does not, and clears the tier. Reasoning: an outage is temporary and retrying fixes it; a revoked grant can *never* be verified again by any number of retries, and continuing to grant paid perks off a credential we can't check is the exact hole this loop exists to close. The row is kept rather than deleted so `/patreon status` still shows something and one `/patreon link` repairs it.
- **The rotated-token trap, and the guard against it.** Patreon issues a *new* refresh token every time one is used. So the new pair is committed **immediately** on a successful refresh, before the identity call — if the process died between the two, the stored token would already be spent, the next cycle would read `invalid_grant`, and the loop would declare a **paying subscriber** dead. Relatedly, a refresh response with no `refresh_token` field keeps the old one rather than blanking it, which would manufacture the same dead link. Both cases are covered in `scratch/check_patreon_refresh.py`.
- **A 401 on the *retry* stays fail-open.** If Patreon rejects a token it minted seconds earlier, that's Patreon's problem, not a dead grant — so it's reclassified transient rather than falling through to the dead-link branch.
- **The tick catches broadly on purpose.** `tasks.loop` kills itself permanently after one uncaught exception (see the note at `cogs/status_cog.py:87`), and *this* loop going quietly dead reopens the very hole it closes. The service already fails open per-link, so the cog's `except Exception` is only for the unexpected — a DB error, or something malformed getting past the service.
- **Deliberately NOT included: notifying the user.** A tier change is logged (at `warning` level when it costs someone perks — the only record that a background job removed something a player paid for, and the first thing to check if they ask why), but no DM is sent. The existing welcome copy is wrong for a lapse (`NO_PLEDGE_INTRO` reads as a greeting), and lapse-notification copy is its own decision with its own tone question. Log first, decide the copy later.
- **`patreon_user_id` is never rewritten by the loop.** If it ever differs, the row belongs to a different Patreon account than it did — `/patreon link` handles that properly, and silently reassigning it in a background job would hide it.
- **Verified:** `scratch/check_patreon_refresh.py`, 74 checks against a fake `aiohttp.ClientSession` and a throwaway DB. Covers lapse, downgrade, upgrade, no-change, five outage shapes (500/503/429/connection error/timeout), 5xx and malformed responses on the refresh grant, missing local credentials, the refresh-and-retry happy path, refresh-token rotation and retention, all three dead-link statuses, the 401-on-retry reclassification, batch ordering and cap, an empty table, and end-to-end that `get_tier_rank()` actually reflects a refreshed downgrade (and that a stored `growth_perk_choice` goes inert with it).


### 9.2 Arachnid tier (rank 1) — implemented, live
1. **Organic Webbing** — deterministic, not a chance roll: Arachnid+ patrols never touch `web_fluid_vial` inventory or the no-fluid cash tax, 100% of the time. `/lab brew` output is unaffected/still sellable. **This is the lower grade of a two-grade perk, not a standalone one** — at Symbiote it becomes Biomorphic Webbing (§9.3), which does everything it does plus more. See "The webbing ladder" below.
2. **Enhanced Strength** — `+30%` Attack damage (`ENHANCED_STRENGTH_DAMAGE_BONUS`), **crime-tier patrols only** (excluded from boss fights — that difficulty curve is tuned around full-strength numbers).
3. **Combat-Ready Patrols** — flat `+15` weight bonus to each crime-tier patrol-roll entry (`COMBAT_READY_PATROLS_WEIGHT_BONUS`).
4. **Drawback (the only one)**: ally happiness decays `+50%` faster (`ARACHNID_ALLY_DECAY_INCREASE`), always-on, no opt-in — full drain 24h → 16h (§12). **Narrative framing (set 2026-08-22, and it is the load-bearing one):** the allies aren't neglected and they aren't needy — they are deliberately holding onto Peter Parker, because the further the bond takes him the less of him comes back. Visiting is what keeps Peter *Peter* rather than letting the thing underneath off its leash. This framing is what makes the drawback scale-with-tier read as *story* rather than as a tax: Symbiote inherits it (§9.3) and there the monster is literal, so the same mechanic lands harder without a second number. Do not re-frame this as "your allies watch you more closely" (the pre-2026-08-22 wording) — that read as surveillance and gave the cost no stakes.

**The webbing ladder — Organic is Biomorphic's prerequisite (set 2026-08-22).** Organic Webbing and Biomorphic Webbing are **one perk with two grades**, not two perks that stack. Biomorphic is what Organic *grows into*: it does everything Organic does (vial-free patrols, no cash tax) and adds the three bonus rolls in §9.3. The prerequisite needs no enforcement code — the rank ladder already guarantees it, since every `>= TIER_RANK_ARACHNID` check passes for Symbiote too.

Mechanically this changed nothing (a Symbiote subscriber always had both behaviours); what it changed is **attribution**, which was actively wrong before:
- `_perk_sections()` (`patreon_cog.py`) listed both as **peer bullets** on a Symbiote card, advertising the same vial-free patrol twice under two names and reading like Biomorphic was a sidegrade beside Organic. Now exactly one webbing line renders per card — `ORGANIC_WEBBING_LINE` or `BIOMORPHIC_WEBBING_LINE`, never both — and it leads the list, since it's the perk that touches every single patrol.
- Both patrol-card sites (`_noncombat_view`'s Web Fluid field and the battle-start banner) hardcoded the **Arachnid** emoji and the literal text "Organic Webbing", so a Symbiote subscriber was told a *lower tier's* perk was what saved them the vial. Both now route through `patrol_cog._webbing_note()`, driven by `PatrolStart.biomorphic_webbing_active` / `PatrolResult.biomorphic_webbing_active` (`tier_rank >= TIER_RANK_SYMBIOTE`, set alongside `organic_webbing_active` in `_begin`). That flag is **naming-only** — it can never be true unless `organic_webbing_active` is, and it changes no behaviour, only which perk gets the credit and therefore which tier emoji appears.

**Electric Webbing and Spider Bots moved out of this list** (were originally a free always-on part of this tier, changed 2026-08-20): they're now Arachnid+-gated *purchasable* gadgets — see §6.1 — rather than something every Arachnid+ subscriber gets automatically. Their proc rates were re-tuned 2026-08-22 (0.20 → 0.45 base); the original below-baseline numbers and why they were wrong are recorded in §6.1.

**Visibility of the gated items — verified 2026-08-21, no work needed.** All four (`spider_bots`, `electric_webbing`, `camera_silver`, `camera_gold`) are visible to **everyone**, subscriber or not, by design: `shop_service.list_shop_items()` applies **no tier filter at all** (only `Item.price.is_not(None)`), and `GATED_ITEM_MIN_RANK` is consulted *exclusively* inside `buy_item`. `shop_cog._patreon_branding()` marks them "🕷️ Patreon exclusive" in `/shop list` and `/shop browse` — the badge is the emoji of whichever tier gates that specific item, so a Symbiote-only item wears the Symbiote emoji rather than misreporting an Arachnid pledge as sufficient — and a non-subscriber's purchase attempt fails with a message pointing at `/patreon link`. The only thing that hides a gadget from `/shop browse` and the `/shop buy` autocomplete is the ordinary reputation lock (`_is_locked`), which applies identically to every gadget — Spider Bots `unlock_level 8`, Electric Webbing `14`, sitting inside the existing free-gadget ladder (5/10/15/20). Nothing tier-related suppresses them anywhere.

### 9.3 Symbiote tier (rank 2) — implemented, live — includes everything above plus:
1. **Venom Blast** — boss fights only, once per fight. The hit that *would* deplete suit integrity to 0% is absorbed instead; player counters for `VENOM_BLAST_DAMAGE_MULTIPLIER` (= 2) × a normal attack roll. Checked *before* the boss-clear-promotion logic, applied inside `_apply_counter_with_venom_blast()`, which returns a `CounterOutcome` (`damage` / `venom_line` / `dampener_note`) rather than a bare string — the three compose differently, and callers must format their damage readout from `outcome.damage` so a Sonic-Dampener-boosted hit doesn't print the pre-multiplier roll.
   - **Validated 2026-08-23** (`scratch/combat_sim.py venom`), closing the "flagged for real validation" note that stood here from when `scratch/boss_tune2.py` was lost. The sim drives the *real* resolvers rather than re-deriving the damage math, and replicates only `_advance`'s end-of-round conditions (those live in a cog). It is **gadget-free** — `resolve_gadget` is async and hits the DB per round — so its absolute win rates are **not** comparable to the 70–75% benchmark in `ENEMY_STATS["boss"]`, which assumed a fully-upgraded kit. What it measures is *deltas* between otherwise-identical configurations, where the missing gadget contribution cancels.
   - **The "2×" is now a named constant** (`VENOM_BLAST_DAMAGE_MULTIPLIER`), not the bare `* 2` it was until 2026-08-22. It is **copy-bound, not a free tuning knob**: `VENOM_BLAST_LINES` promises "twice as hard" / "pays it back double" and `SYMBIOTE_PERKS` echoes it, so moving the number means rewriting all of that too.
   - **The self-scaling claim holds.** Because it multiplies `attack_damage_range`, which scales on the same difficulty curve as enemy HP, the blast is worth a near-constant share of the boss health bar all the way up the ladder: **33.3% / 32.3% / 31.8% / 31.7%** at brackets 1 / 5 / 10 / 20 — a 1.67% spread across the whole game. This is the entire reason it's a multiple of an attack roll rather than a flat figure that would need its own tuning pass per bracket.
   - **It is the single most load-bearing perk in the game, and far from cosmetic.** Gadget-free boss win rate, Symbiote off → on: bracket 1 `97.27% → 98.19%`, bracket 5 `26.48% → 92.47%`, bracket 10 `3.78% → 70.13%`, bracket 20 `2.47% → 62.47%`. Sensitivity at bracket 10 (×0 → ×4): `17.55% / 37.32% / 69.95% / 91.65% / 98.21%` — each single step of the multiplier is worth 20–33 points of win rate, which is exactly the balance fragility §9.2's Enhanced Strength comment cites as its reason for excluding boss fights. **Do not treat this constant as safe to nudge.**
   - **Two consequences worth knowing, neither a bug.** (a) The perk is a *comeback* mechanic, so its value is inversely proportional to how well you'd otherwise do — it's worth +0.92 points at bracket 1 and +66 at bracket 10, because at bracket 1 there's no headroom. (b) The copy ("the hit that *would* end a boss fight") implies rarity, but past bracket 5 the blast fires in **76–98% of fights** — at high brackets it is effectively a passive extra life, while at bracket 1 a subscriber will almost never see it (3.4%). Both tails are inherent to a trigger keyed on "would this hit kill you", not something a different number fixes.
2. **Biomorphic Webbing** — **the Symbiote tier's webbing perk, and the only one it has**: Organic Webbing's evolved grade, not an addition beside it (see §9.2's "webbing ladder" — Organic is its prerequisite, enforced free by the rank ladder). It carries everything Organic did (vial-free patrols, no cash tax) *plus* three independent rolls (not one shared roll — copy promises "coins, photos, AND parts"). All three now report themselves on the result card (§9 audit); each roll's own `BattleReport` field exists purely for that:
   - `BIOMORPHIC_WEBBING_CASH_CHANCE = 0.25` → +$15–35 (applies to combat *and* non-combat patrols — lives in `patrol_service.py`, imported into `battle_service.py`). Reported via `BattleReport.biomorphic_cash` / `PatrolResult.biomorphic_cash` as subtext under the Cash value, since it isn't separate income — just a share of one number the player otherwise couldn't attribute.
   - `BIOMORPHIC_WEBBING_COMPONENT_CHANCE = 0.20` → bonus component drop, **only rolled if the base drop_chance roll already missed** (never stacks into a guaranteed double-drop). Combat only. Reported via `biomorphic_component` — before 2026-08-21 this was byte-identical to a base-game drop, so the entire perk was invisible.
   - `BIOMORPHIC_WEBBING_PHOTO_CHANCE = 0.20` → bonus second `PendingPhoto`, only if a camera's equipped and a photo was already banked this fight. Combat only. Reported via `biomorphic_photo` as a "Second Shot" row, and it now takes its own quality-bump roll off the equipped camera (§6.2).
   - **The perk copy is scoped to match (2026-08-22).** `BIOMORPHIC_WEBBING_LINE` promised all three rolls flatly, which told a subscriber who mostly runs quiet patrols to expect two rolls that can never fire for them — 2 of the 3 are combat-only. It now reads "an extra chance at bonus cash on every patrol, plus a bonus component and a bonus photo on **combat patrols**": cash stays unqualified because it genuinely is rolled on both paths, and only the two combat-only rolls carry the qualifier. Deliberately not qualified further — the photo roll also needs a camera equipped, but that's already true of every photo in the game and spelling it out here would read as a second restriction rather than the same one.
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
   - Verified: `scratch/check_stealth_instrumentation.py`, 46 checks.
4. **Sonic Dampener (drawback)** — `+30%` incoming counter damage (`SONIC_DAMPENER_DAMAGE_INCREASE`), scoped specifically to boss fights against **"the Shocker"** (`SONIC_DAMPENER_BOSS_NAME`) — the only one of the 20 bosses that thematically fits, since all 20 mechanically share one stat block (no per-boss attack-typing system exists). Applied *before* the Venom Blast check, so a dampened hit correctly factors into whether Venom Blast would even trigger. Since brackets cycle through all 20 names, this recurs for long-term players — brackets 4/24/44/64, i.e. reputation levels 20/120/220/320.
   - **Rematch-matching bug, fixed 2026-08-22.** The check was an exact `state.enemy_name == SONIC_DAMPENER_BOSS_NAME`, but `patrol_service.boss_name()` suffixes repeat encounters `" (Round N)"` once the roster wraps — so `"the Shocker (Round 2)"` never matched and **the dampener fired exactly once in the entire game, at reputation level 20**, silently, for every player. The "recurs for long-term players" claim above was therefore false for two days. Now routed through `_is_sonic_dampener_boss()` (exact match OR `"the Shocker ("` prefix, so it can't over-match a hypothetical `"the Shockerville"`). Verified against all 80 brackets: fires at 4/24/44/64, zero false positives. **Consequence worth knowing:** this is a real nerf to the Symbiote tier that restores an intended cost — before the fix the tier was near-pure upside past level 20, since its only stated drawback was unreachable. Reverting is one line in `_is_sonic_dampener_boss`.
   - **Now emits a line** (`SONIC_DAMPENER_LINES` + 🕸️, fixed 2026-08-21). It previously applied the multiplier in total silence, which read as the boss being tuned unfairly rather than as the tier's own stated cost — the worst possible failure mode for a *drawback*, since the player couldn't even tell it existed. The note is deliberately **suppressed when Venom Blast fires**: that path negates the hit outright, so there's no extra damage left to explain and "the blow never lands" would contradict itself. The dampener still counted — it's what pushed the hit lethal enough to trigger the blast.
   - Copy is kept literal against the math: Venom Blast says "twice as hard" and is exactly 2×, so the dampener's lines say "uglier than it should" rather than claiming a multiple that +30% doesn't earn.
5. **The suit overrides you (drawback)** — shipped 2026-08-23. `SYMBIOTE_OVERRIDE_CHANCE = 0.10`: each time a Symbiote subscriber chooses **Evade**, a 10% roll replaces the whole action with an Attack instead. This is the tier's **own always-on cost**, and it exists because it was the one thing the tier didn't have — every other Symbiote drawback is conditional (Sonic Dampener is 1 boss in 20; the ally-decay penalty is inherited from Arachnid, §9.2), so on an ordinary patrol the top tier cost its owner nothing.
   - **Scoped to Evade only, and that scope is the design, not a shortcut.** The rule the copy states is *the suit overrides you when you try to hold back* — it never overrides aggression, because there's nothing there to override, and a player who only ever attacks is already fighting the way it wants. Gadgets are deliberately exempt too: they're scarce, deliberately chosen, and carry durability, so swallowing one reads as a lost *item* rather than a lost impulse — and the boss benchmark assumes proactive defensive gadget use, which the gadget-free sim can't re-measure.
   - **Both halves of the cost bite.** The override forfeits the `EVADE_DAMAGE_MULTIPLIER` reduction (verified at a **4.02×** measured suit-damage ratio against the 4.00× the constant implies) *and* banks no combo for next round, because `combo_ready = True` sits below the early return. It delegates to `resolve_attack` rather than duplicating it, so Enhanced Strength, Venom Blast and the Sonic Dampener all still apply to the round the suit stole.
   - **One deliberate upside**: it still *consumes* a combo banked by a previous Evade, so the suit occasionally cashes in an opening the player was about to waste by dodging again. Already priced into the numbers below.
   - **Measured cost, 60k trials per cell** (`scratch/combat_sim.py override`), against an Evade→Attack policy versus the identical fight with the override off: boss brackets 1/5/10/20 `-1.14% / -1.58% / -5.63% / -4.58%`; crime bronze `-0.68..-0.99%`, silver `-1.14..-1.30%`, gold `-0.18..-0.73%`. Visible in **29.3%** of boss fights and **24.8%** of crime patrols. Negative everywhere, worst exactly where the stakes are highest.
   - **Why 0.10 and not higher.** At 0.15 the cost is `-7.87%` on a boss, which drops a *paying* subscriber below the 70–75% a non-subscriber with a fully-upgraded gadget kit gets. A perk you pay for should never do that.
   - **Two shapes were priced and rejected — recorded so they don't get re-proposed.** (a) Making the override hit *harder* (a guaranteed 1.5×, borrowing the combo constants) is a straight **buff** at every rate: `+3.16%` boss, `+10.41%` crime at 0.15. (b) Adding a suit tear on top of that moves crime-tier win rate by **exactly 0.00%**, because suit integrity is cosmetic *inside* a crime fight — it only bills you for repairs afterward. Both fail the same structural test, and it's worth stating plainly: **in crime patrols defense is worthless, so any override toward aggression that also improves the swing is free-to-positive there.** That is the reason the shipped shape is a plain attack.
   - Emits `SYMBIOTE_OVERRIDE_LINES` + 🕸️ (8 variants), and every line has to make it unmistakable that a Dodge is about to read as an attack — a player who can't tell why the button did something else reads it as a bug, which is the worst possible outcome for a cost they're paying for. Disclosed to the player in `SYMBIOTE_DRAWBACKS`, so it surfaces in both `/patreon perks` and the welcome DM under "What It Costs You", and **only** for Symbiote — an Arachnid subscriber is never shown a cost they don't pay.
   - Reverting is the one early-return block at the top of `resolve_evade`; setting `SYMBIOTE_OVERRIDE_CHANCE = 0.0` disables it without touching code (verified).

### 9.4 Explicitly NOT part of the Patreon roster (dormant, for a separate track)
`PatreonLink.growth_perk_choice` (`"xp"` / `"allies"` / `None`), `ACCELERATED_GROWTH_XP_MULTIPLIER = 1.3` (economy.py), `SUPPORTIVE_ALLIES_DECAY_MULTIPLIER = 0.7` (ally_service.py) — this mechanism was originally wired to Patreon tier_rank via a `/patreon choose` command, which was a mistake later corrected (2026-08-18): it actually belongs to a **separate, not-yet-rebuilt Discord Server Booster perk track** (was built once, fully reverted per user instruction — see commits `76dbc26`/`a05e719`/`10a2c8f` area). `/patreon choose` has been removed. The code stays intact and dormant — do not delete it, do not re-wire it to Patreon; when the booster track gets rebuilt, gate it on Server Booster status instead. §9.5 now records which specific perks that rebuilt track owns.

### 9.5 Not-yet-implemented perk designs (numbers locked, track assigned, no code yet)
Four perks are fully speced with locked numbers but unbuilt, all four on the Booster track. **Track ownership was decided 2026-08-21** — this was the long-standing open question (two tracks exist and had to not be conflated, see §9.4); it is now settled and no longer needs re-asking:

**Server Booster track** (discord.gg/spider-man Nitro boosting — the track that was built once then fully reverted, commits `76dbc26`/`a05e719`; rebuilding it requires re-enabling the Discord **Members intent** to read boost status, so it is a live-bot config change, not just code):
- **Higher Suit Integrity**: 25–35% less suit damage in crime-tier patrols only (boss fights untouched — that curve is tuned around full-strength numbers). Rejected alternative: raising the 100 integrity cap — `100` is hardcoded in 5 places including `repair_suit()`, which would silently reset a raised cap back to flat 100.
- **Higher Reputation XP**: 25–35% bonus, applied at §3's `add_reputation` chokepoint — self-limiting by construction, since that function also enforces the boss-gate ceiling, so it can never skip a gate. Explicitly **no effect on boss-clear promotions** (those snap straight to the next level's floor, bypassing `add_reputation` entirely). Concretely: level 10→15 is ~144 patrols unboosted vs ~106 at +35%.
- **Supportive Allies**: 25–35% decay reduction on `DECAY_PER_HOUR`. Renormalized 2026-08-22 against the new 24h baseline (§12) — full drain stretches from 24h to **32–37h**, and thriving-to-neglected from 9.6h to **12.8–14.8h**. The dormant code already carries the midpoint, `SUPPORTIVE_ALLIES_DECAY_MULTIPLIER = 0.7` (a 30% reduction → 34.3h full drain, 13.7h thriving-to-neglected), so the band is documentation around a shipped number rather than an open choice. **Mutually exclusive with Higher Reputation XP** — stacking them compounds toward ~1.5–1.6x total XP rate instead of either perk's standalone ~1.3x, because ally happiness already gates ±20% XP/earnings on its own. This exclusivity is exactly what `growth_perk_choice` already encodes (one field, one value) and must be enforced at the grant site, not just documented.
- **Quicker Web Brewing**: `BREW_DURATION` 5min → 3min. Note: the earlier locked design had this as ungated/everyone; per the 2026-08-21 decision it belongs to the Booster track. 3 minutes was chosen deliberately — it raises theoretical max vial coverage from ~30% to ~50% of back-to-back patrol demand, while 1.5min would let a non-subscriber replicate Organic Webbing (§9.2) through effort alone and undercut that perk. Cutting brew *time* by X% is **not** +X% throughput — it's inverse (-30% time = +43% throughput).

**Symbiote tier** (Patreon rank 2 — the live, built track): nothing outstanding here. **Camera Gold shipped 2026-08-22** — see §6.2 for the ladder table, the four wiring points, and the two silent-failure modes. Its 0.80 bump superseded an originally-locked ~45–55%, which was written before Silver shipped at 0.60 and so sat *below* the lower tier, inverting the ladder; Gold had to beat 0.60 and does. It is a separate `"tool"` item, **not** the `upgrade_level`-on-one-item shape this section originally sketched (superseded by §6.2). **The tier's own always-on cost shipped 2026-08-23** (§9.3 item 5) — it previously had no drawback that fired on an ordinary patrol, and **Venom Blast's 2× is now validated rather than assumed** (§9.3 item 1).

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

**Thresholds & payoffs**: `THRIVING_HAPPINESS_THRESHOLD = 70` (both allies) → `+20%` reputation XP (`XP_BONUS_MULTIPLIER`) on patrol/tutoring. `LOW_HAPPINESS_THRESHOLD = 30` (either ally) → `-20%` earnings (`EARNINGS_PENALTY_MULTIPLIER`) on Bugle sales and tutoring cash, AND doubles the related ally-hazard's chance by `NEGLECT_HAZARD_MULTIPLIER (2.5)`.

---

## 13. Tutoring (`/tutoring`)

`services/tutoring_service.py`. Safe, steady cash — locks `/patrol` for `TUTORING_LOCK_SECONDS = 2 minutes` (shared "busy" system with `/ally visit`). Base: `cash = round(rand_range([80,140]) * reputation_multiplier * earnings_penalty_multiplier)`, `xp = round(rand_range([10,20]) * ally_xp_multiplier)`, `crime_rise = rand_range([8,15])` — **the only thing in the game that raises `crime_level`** (§5.7). A 12% "jam" event (`JAM_CHANCE`, shared pattern with Bugle) can fire: handled cleanly by an equipped gadget → +$15-35 bonus; unhandled → `+10` extra crime_level (`JAM_PENALTY_CRIME_RISE`) and no bonus.

---

## 14. Daily Bugle (`/bugle`)

`services/bugle_service.py`. Sells every banked `PendingPhoto` at once. `BUGLE_COOLDOWN_SECONDS = 60`. Per-photo payout: `round(rand_range(bugle_payouts[quality]) * jjj_multiplier_roll * reputation_multiplier * earnings_penalty_multiplier)`, where `bugle_payouts = {bronze:[50,150], silver:[150,350], gold:[300,600]}` and `jjj_multiplier` (JJJ's haggling) is `rand_float_range([0.8, 1.3])`, rolled independently per photo. Same 12% jam mechanic as tutoring: handled → +$15-35; unhandled → lose `JAM_LOSS_FRACTION (0.2)` of the total sale.

---

## 15. Shakedown / PvP (`/shakedown`)

`services/shakedown_service.py`. `SHAKEDOWN_COOLDOWN_SECONDS = 2 min` (attacker), `TARGET_PROTECTION_SECONDS = 2 min` (victim can't be re-targeted right after). Minimum target wallet to be worth attempting: `MIN_TARGET_WALLET = $50`.

- `success_chance(target_wallet) = max(0.15, 0.65 - min(0.45, (wallet/4000) * 0.45))` — bigger scores are harder to pull off (`BASE_CHANCE=0.65`, `MAX_WALLET_PENALTY=0.45`, `WALLET_PENALTY_SCALE=4000`), floored at 15%.
- On success: steal `round(target.wallet * rand_float_range([0.10, 0.25]))` (`STEAL_PERCENT_RANGE`) from the *wallet only* (bank is always safe).
- On failure: attacker pays `rand_range([20, 60])` (`FAIL_PENALTY_RANGE`).
- **Stealth Mode** (Symbiote perk, §9.3) checked first — if active, attempt fails with zero penalty and distinct flavor text.

---

## 16. Suit Repair (`/workbench repair`)

`services/suit_service.py`. `REPAIR_COST_PER_POINT = $6` per missing integrity point. Requires 1x `spandex_fabric` always; if missing ≥ `ELECTRONICS_THRESHOLD (50)` points, also requires 1x `micro_electronics`. Fully restores to 100%. Blocked entirely if `eviction_meter >= 100` (§8). A post-patrol warning (`repair_readiness_warning`) surfaces once `suit_integrity <= LOW_SUIT_WARNING_THRESHOLD (30)` if the player lacks the components they'd need.

---

## 17. Trade Post (`/market`)

`services/market_service.py`. Player-to-player listings, items held in **escrow** (removed from seller's inventory the moment listed, not on sale). `MAX_ACTIVE_LISTINGS_PER_USER = 10`. `NOT_TRADEABLE_CATEGORIES = ("tool", "gadget")` — tools/gadgets track equip state and per-copy durability/upgrades, not tradeable through this system. Selling uses a modal (`SellModal`) for quantity + price input. No platform fee — full `quantity * price_per_unit` goes to the seller on `/market buy`. Cancelling refunds the item to the seller's inventory.

---

## 18. Items Catalog (`data/items.json`)

| Key | Category | Price | Notes |
|---|---|---|---|
| `camera` | tool | $150 | Starter item, auto-equipped on first contact (`get_or_create_user`). Durability 100. Part of `CAMERA_FAMILY_KEYS` (§6.2). |
| `camera_silver` | tool | $1,000 | Arachnid+-gated (§6.2). Equipping it retires whatever camera-family tool was equipped before. |
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
4. Tools: blocked if you already own an *equipped* copy of that exact key — you can't double-buy a working camera. (If your existing copy is unequipped/broken, buying replaces it in place — same row, quantity reset to 1, durability reset to max, re-equipped.) Camera-family tools (`CAMERA_FAMILY_KEYS`, §6.2) are the one exception where buying a *different* key still succeeds — it just also unequips whichever other camera-family tool was equipped first.
5. Wallet must cover `item.price` (bank funds are **not** touched by `/shop buy`).
6. On success: `add_wallet(-price)`, then branch by category —
   - **tool**: upsert a single `InventoryItem` row per item_key, always `equipped=True`, `quantity=1`. Camera-family purchases additionally unequip any other equipped camera-family row first.
   - **gadget**: always **inserts a brand-new row** — quantity 1, `equipped=False`, `durability=max`, `upgrade_level=0` — never merges into an existing stack. This is deliberate: each gadget copy tracks its own durability/upgrade level, and buying a spare after one breaks has to be a real, distinct purchase.
   - everything else (components, gifts): stacks via `add_item()` (`services/inventory_service.py`).

### 19.2 `/gadget` (`services/gadget_service.py`, `cogs/gadget_cog.py`)
Covers everything not already in §6 (proc chances, wearout, upgrade cost formula):
- **`/gadget status`** — read-only list, split into Equipped vs In Storage.
- **`/gadget panel`** — interactive `GadgetPanelView`: a `Select` of every *distinct* owned gadget key (deduped to the "best copy" — highest upgrade level, then highest durability, matching the same convention `list_all_owned_gadgets` uses for boss fights), plus Equip/Unequip/Upgrade buttons that act on whichever copy is selected and re-render in place.
- **`/gadget equip <gadget>`** / **`/gadget unequip <gadget>`** — direct commands, same underlying `equip_gadget`/`unequip_gadget` as the panel. Equip fails if both of the `MAX_EQUIPPED_GADGETS (2)` slots are already full, or if you don't own an unequipped copy, or if you haven't hit the gadget's `unlock_level` yet.
- **`/gadget upgrade <gadget>`** — must already be equipped (upgrading only ever applies to the equipped copy). Fails past `MAX_UPGRADE_LEVEL (3)` or on insufficient wallet cash. Cost formula and per-gadget bonus/level table are in §6.

### 19.3 `/lab` (Chem Lab — `services/brewing_service.py`, `cogs/lab_cog.py`)
Thin wrapper over §11's brewing mechanics: `/lab status` (time remaining or "ready"), `/lab brew` (starts a batch, fails if one's already active or wallet's short of $30), `/lab collect` (fails if nothing's brewing or it's not ready yet — `/admin bypass` skips the ready-time check for testing).

### 19.4 `/workbench` (Suit repair — `services/suit_service.py`, `cogs/suit_cog.py`)
`/workbench status` shows current integrity, full-repair cost breakdown, and components on hand. `/workbench repair` executes it — see §16 for the cost/component formula. Both are blocked (with an explicit message) if `eviction_meter >= 100` (§8).

### 19.5 `/balance`, `/inventory`, `/bank` (`cogs/economy_cog.py`)
- **`/balance`** — wallet, bank/capacity, reputation level+XP, suit integrity. Read-only.
- **`/inventory`** — every owned `InventoryItem` row, grouped by category (gadget, then tool, then component, then collectible, then gift, then any other category alphabetically after). Gadgets show "Battle-Grade" (equipped) vs "Swinging-Grade" (stored); other equippable items just show "— Equipped".
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
3. **Individual item-level "deletion" is normal and expected**, not data loss: an `InventoryItem` row disappears when a stack hits 0 (`remove_item`), a gadget breaks (`roll_gadget_wearout` deletes the row outright — must be rebought), or a camera breaks (same). A `PendingPhoto`/`Brew`/`MarketListing`/`Cooldown` row disappearing when its lifecycle completes (sold, collected, bought/cancelled, expired) is the table doing exactly what it's for — none of these are logs, they're queues of in-flight state.

### 20.13 Migration history (Alembic, `alembic/versions/`)
In order: baseline → admin_users (add table) → reputation XP curve rescale (the `LEVEL_GROWTH=1.12` accelerating curve, replacing a flat-100 system) → boss gates (add `boss_clears`, existing users grandfathered past gates they'd already cleared) → patreon_links (add table) → growth_perk_choice (add column to `patreon_links`) → last_active_at (add column to `users`, for Stealth Mode) → **shakedown_attempts (add table, `a7c41e93b508`, 2026-08-23 — Stealth Mode threshold instrumentation, §9.3)**. All eight are applied to the local dev DB (`alembic current` confirms head = `a7c41e93b508`) **and all eight files are tracked in git** (verified 2026-08-21 via `git ls-files alembic/versions/` — an earlier note here claimed two were untracked; that was stale, there is no repo/DB gap).

**Known and pre-existing: `alembic upgrade head` from a genuinely empty database does NOT work** (confirmed 2026-08-23; it fails at `44f8f54cbf0b` with `duplicate column name: boss_clears`, and has been broken since that migration was written — nothing to do with the newer ones). Cause: the baseline revision runs `Base.metadata.create_all()` against the **current** models, so a fresh DB gets every table with every modern column already present, and each later `op.add_column` then collides with a column that already exists. The supported path is the one actually used — an existing DB stamped at some revision, upgraded forward, where the baseline never re-runs. To bootstrap a new environment, let `create_all` build the schema and then `alembic stamp head`. Fixing this properly means freezing the baseline to the schema as it stood in Aug 2026 rather than tracking `db/models.py`; not worth doing until someone actually needs a from-zero migration path.


---

## 21. Local Dev Environment Gotchas (Windows)

- **Windows Smart App Control blocks a freshly-downloaded, unsigned `ngrok.exe`** outright (`WinError 4556`). No per-file exception exists once the policy's on. Fix in `cogs/tunnel_cog.py`: on Windows, check `shutil.which("ngrok")` first and reuse a `winget install ngrok.ngrok`-installed copy (a trusted channel) instead of auto-downloading. Linux deployment still auto-downloads fine, untouched.
- **ngrok's free tier shows a browser interstitial** to anonymous visitors unless the request carries `ngrok-skip-browser-warning` — real users *will* see a warning page mid-`/patreon link` requiring one extra click. Only a paid ngrok plan removes it; not fixable bot-side.
- **`ngrok config add-authtoken` hangs indefinitely** on the real Linux deployment (likely a background update-check that never returns on a network-restricted host) — `_write_config()` writes ngrok's v3 YAML config file directly instead of shelling out, sidestepping the subprocess entirely on both platforms.
- Python `logging` output was unreliable through a `Start-Process -RedirectStandardOutput/Error` + hidden-window local diagnostic setup — `log.info()` calls often silently vanished even though the underlying code worked (verified via a real `/health` HTTP hit returning 200). If a "hang" won't show any log output, verify with a real functional signal before trusting the silence as proof.
