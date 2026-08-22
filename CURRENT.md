# Current Session State — 2026-08-23

Snapshot of exactly where things stand. Read [GAME_DESIGN.md](GAME_DESIGN.md) first for the actual mechanics — this file is about *status*: what's committed, what's still open, and what to do next.

The previous snapshot (2026-08-22, the Arachnid+ tier work and the two balance retunes) is preserved in git: `git show cc432e8:CURRENT.md`.

## Headline: all six of the owner's decisions are closed. Working tree clean, `main` is **18 commits ahead of `origin/main` and unpushed**.

The session opened with a review of the Symbiote tier that surfaced six problems; the owner was given options for each and picked one per problem ("okay present me with options to how you recommend we deal with each problem, ill give you the answer"). Every one of them has now shipped.

| # | Problem | Decision | Commit |
|---|---|---|---|
| 1 | No Symbiote purchase gate existed — `ARACHNID_GATED_ITEM_KEYS` was one flat set with a single rank check | **Per-key rank map** | `faed3d0` |
| 2 | A lapsed pledge kept every perk forever — tier was written at link time and never re-read | **Background refresh loop** | `cd06e3a` |
| 3 | Symbiote had no always-on cost, only a single boss drawback | **"The suit overrides you in combat"** | `49ad4f9` |
| 4 | Venom Blast's `2x` was never validated | **Validate it** | `49ad4f9` |
| 5 | Stealth Mode's 20 minutes was never measured | **Validate it** | `5c41181` |
| 6 | Biomorphic Webbing promised 3 rolls; 2 are combat-only | **Add the "(combat patrols)" qualifier** | `635c104` |

Ten commits landed in total. The four beyond that table:

| Commit | What |
|---|---|
| `e33de83` | Patreon welcome rebuilt as an embed ("bonded with this spider"), DM'd, and **re-sent on re-link** so someone who subscribed before linking still gets it |
| `f330cda` | Ally-decay drawback reframed — allies want visits because they're holding onto Peter Parker, not to unleash what's inside |
| `ad01949` | Spider Bots / Electric Webbing proc rates fixed (they were failing so often they read as broken), and Biomorphic Webbing made to subsume Organic |
| `eee4590` | Sonic Dampener now fires on Shocker **rematches**, not just the first encounter |
| `d88cc29` | Camera Gold shipped — the deferred item from the last snapshot, unblocked by #1 |

**Repo state**: branch `main`, tree clean (only gitignored `scratch/` artifacts remain), `alembic heads` = **`a7c41e93b508`**. One migration this session, applied to the dev DB after a backup at `scratch/spidey.db.bak-before-a7c41e93b508`; verified purely additive, with 6 users and 179 transactions preserved and 14 tables total.

## What each of the six actually did

**#1 — per-key rank map.** `ARACHNID_GATED_ITEM_KEYS` is gone; it's now `shop_service.GATED_ITEM_MIN_RANK`, a `{item_key: min_rank}` map, with `GATED_ITEM_KEYS` *derived* from it for the callers that only ask "is this gated at all?" — so no second list has to be kept in step. `get_tier_rank()` stays the single chokepoint, and ranks are always compared with `>=` / `<`, never `==`, so a Symbiote subscriber automatically clears every Arachnid gate.

**#2 — the refresh loop.** `refresh_stale_links()` in `services/patreon_service.py` drains an oldest-checked-first queue; `SchedulerCog.patreon_tick` runs it every 15 min, 25 links per tick, re-reading anything past a 6-hour staleness window. The governing rule is written at the top of that section: **only a successful read of Patreon's identity endpoint may change a stored tier.** A 500, a 503, a DNS blip, a timeout — all fail open, stamp `last_checked_at`, and leave the tier exactly as it was. Erring this way means a lapsed pledge keeps perks for up to one extra interval, which is the right side to be wrong on.

The one exception is deliberate and necessary: a **4xx on the refresh grant** is Patreon stating the grant is gone, which no retry can fix, so that alone clears the tier (`_DeadLinkError`). Pure fail-open would have reopened the hole through the back door — an unverifiable credential would grant paid perks forever. The row is kept, so one `/patreon link` repairs it.

Two traps were caught before shipping and are both under test. Patreon **rotates the refresh token on every use**, so the new pair is committed *immediately*, before the identity call — otherwise a crash in between leaves a spent token stored and the next cycle declares a *paying* subscriber dead. And a refresh response that omits `refresh_token` retains the old one rather than blanking the field.

`RefreshOutcome` exists because a bare `tier: str | None` return cannot express the difference between "they have no pledge" and "we couldn't tell", and that distinction *is* the fail-open contract. `reached_patreon` is the load-bearing field.

**#3 — the Symbiote's own cost.** `SYMBIOTE_OVERRIDE_CHANCE = 0.10` in `services/battle_service.py`: one Evade in ten, the suit takes over and attacks instead — no dodge, no combo banked. Priced by simulation at roughly one fight in four. 0.15 was rejected because it costs -7.87% on a boss, dropping a paying subscriber *below* the 70–75% an unsubscribed player with a full gadget kit gets, and a perk you pay for should never do that. Two other shapes were priced and rejected outright; the reasons are recorded in the comment so they don't get re-proposed.

**#4 — Venom Blast validated.** Simulated in `scratch/combat_sim.py`. Combat is a closed system, so this one *was* answerable by simulation, and the `2x` holds.

**#5 — Stealth Mode made measurable, and deliberately NOT declared validated.** This is the honest outcome and it matters: the 20-minute threshold **cannot be settled by simulation**, because it depends entirely on how long real players step away from Discord. There's no ground truth to simulate against. So what shipped is the *measuring apparatus*, not a verdict:

- New `shakedown_attempts` table (migration `a7c41e93b508`). Before it, a stealth-protected attempt returned early and charged nobody — so it left **no trace at all**, not even a `transactions` row, and the perk's real firing rate was unobservable.
- `target_idle_seconds` is stamped on **every** attempt, protected or not. That's the design decision worth keeping: any *future* candidate threshold is scoreable from data already collected, instead of needing a fresh instrumentation deploy per number under consideration.
- The gate was split into a reading (`target_idle_seconds`) plus a pure predicate (`stealth_mode_active`) so the logged value is guaranteed to be the one the gate actually judged, and so the analysis script can replay the real rule rather than a copy of it. **Gate behaviour is unchanged** — every boundary is under test.
- Instrumentation is provably non-load-bearing, not merely intended to be: its own session, a blanket `except`, and tests that deliberately break the insert and assert the player's command still resolves and the cash still moves.
- **The constant was not touched, and the code says so** — `STEALTH_MODE_INACTIVITY_THRESHOLD_SECONDS` carries a `STILL UNVALIDATED` note pointing at the analysis script.

**#6 — Biomorphic copy scoped.** The perk advertised 3 rolls; 2 only fire in combat. Copy now says "(combat patrols)" rather than the mechanic being changed, since the mechanic was right and the promise wasn't.

## The backlog, in the order it's actually ready to be picked up

1. **The 4 Server Booster perks** — Higher Suit Integrity, Higher Reputation XP, Supportive Allies, Quicker Web Brewing. Numbers locked in GAME_DESIGN.md §9.5. **Blocked on the owner** (both items below). Supportive Allies' *hours* moved with the 24h retune (full drain 32–37h now); its locked 25–35% band did not.
2. **`.env.example` is missing `PATREON_ARACHNID_TIER_NAME` / `PATREON_SYMBIOTE_TIER_NAME`, with no startup validation.** If either is unset, every subscriber silently resolves to rank 0 — every paid perk switches off with no error anywhere. Both *are* set in the local `.env` (`Arachnid` / `Symbiote`), so nothing is broken today; this is a deploy-time landmine. Small fix: add them to `.env.example` and fail loudly at startup if unset.
3. **`alembic upgrade head` from an empty DB is broken** (pre-existing, not from this session — confirmed empirically by reproducing it at the *old* head `df532d94924d`). `alembic/versions/e83fd19455a5_baseline.py` calls `Base.metadata.create_all()` against *current* models, so a fresh DB gets every modern column and later `op.add_column` migrations collide — it dies at `44f8f54cbf0b` with `duplicate column name: boss_clears`. Workaround for a fresh DB: `create_all` then `alembic stamp head`. A real fix means freezing the baseline to the Aug 2026 schema instead of tracking `db/models.py`. Documented in GAME_DESIGN.md §20.13.
4. **`wipe_user` doesn't clean `PatreonLink` or `AdminUser` rows.** `ShakedownAttempt` is deliberately excluded — it's a log, not state, and the `Transaction` precedent argues for leaving it. Noted in §20.12.

## Open — needs the owner, not an implementation decision

- **The Discord Members intent.** Rebuilding the Booster track requires re-enabling it to read boost status: a live-bot config change, not just code. The track was also built once and then *fully reverted* per explicit instruction (`76dbc26` / `a05e719`), so it warrants a check-in before any code even though the ownership question is settled.
- **Quicker Web Brewing's track.** Originally locked as ungated/everyone; the 2026-08-21 grouping ("the first 4 are for server boosting") moved it under Booster. Recorded as Booster-gated, but worth one confirmation in case that was a broad-stroke grouping rather than a deliberate change to that specific perk.

## Awaiting data, not work

- **Stealth Mode's 20-minute threshold.** The apparatus is shipped and the constant is honestly labelled unvalidated. Once real `/shakedown` traffic accumulates, run `python scratch/analyze_stealth_mode.py` — it reports the firing rate against Symbiote targets (the honest denominator), an idle-time distribution, and a counterfactual table for every candidate threshold. It refuses to conclude from an empty table, and it flags small samples as directional only. Target band: a threshold that protects a clear *minority* of attempts — high enough that actively-playing subscribers stay targetable (otherwise it's the pay-to-win immunity that was already rejected), low enough that it fires often enough to be noticed.
- **What that data will *not* contain**, so it isn't mistaken for a complete census of intent: cog-level refusals never reach the resolver, and the 2-minute `shakedown_target` cooldown masks a second attempt on the same target.

## Numbers still not simulation-verified

- **Spider Bots / Electric Webbing proc chances.** Retuned in `ad01949` against the existing 0.25–0.55 gadget baseline, not by a binary-search pass.

Venom Blast's `2x` came off this list in `49ad4f9`. The crime source/sink balance came off it in `c91f4fb`.

## Verification pattern for this project — don't skip it

Run in this order; all of it was clean at the end of this session.

```bash
python -m compileall -q cogs services db utils alembic
```

Then load every extension end-to-end (23 of them) using the project venv — `.venv/Scripts/python.exe`, **not** bare `python`, which has no `discord` module:

```bash
./.venv/Scripts/python.exe -c "import discord, bot; b = discord.Bot(intents=discord.Intents.default()); [b.load_extension(e) for e in bot.EXTENSIONS]; print(f'{len(bot.EXTENSIONS)} loaded')"
```

Then the functional checks in `scratch/` (gitignored, so they live only in the working copy — `check_rank_map`, `check_camera_gold`, `check_override`, `check_patreon_refresh`, `check_stealth_instrumentation`). Each builds a throwaway DB, drives the real service functions, and exits non-zero on failure. `check_patreon_refresh` is 74 checks and `check_stealth_instrumentation` is 46; both were passing at the end of the session.

**Every scratch script must set `SPIDEY_DB_URL` before anything imports `db.base`.** `config.py` reads `SPIDEY_DB_URL` — there is no `DATABASE_URL` — and it defaults to `./spidey.db`, so a script that sets the wrong name silently runs against the live dev database. Each script carries this as a comment.

Two Windows gotchas, both still true: **sqlite can't open a Git Bash `/tmp/...` path** (keep throwaway DBs inside the project dir), and a scratch script run from `/tmp` won't find the `services` package for the same reason. Standalone scripts need `sys.path.insert(0, '.')` since the repo root isn't installed. Also, the console can't print em-dashes or custom emoji — the `?` you see is a display artifact, not a data bug, but it **does** corrupt `Edit` `old_string` matching, so never build one from console output on this platform.

One coupling worth knowing before touching `db/base.py`: `async_sessionmaker(engine, expire_on_commit=False)` is **load-bearing** for `refresh_stale_links`, which commits per-link inside a loop over ORM objects. With expiry on, reading the next link's `access_token` would need implicit IO and raise `MissingGreenlet` under asyncio. The comment in `refresh_stale_links` says what to do if that setting ever changes.
