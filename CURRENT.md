# Current Session State — 2026-08-21

Snapshot of exactly where the Patreon Arachnid/Symbiote perk work stands right now. Read [GAME_DESIGN.md](GAME_DESIGN.md) first for the actual mechanics — this file is about *status*: what's done, what's uncommitted, what's still open, and what to do next.

## Headline: a full feature is sitting uncommitted on `main`

`git log` shows the last commit (`40f99c6`) is an unrelated status-rotation bugfix. **Everything described below — the entire Patreon Arachnid/Symbiote perk system, Stealth Mode, `last_active_at`, and the Windows ngrok fixes — is uncommitted working-tree changes on top of that.** `git diff --stat` confirms: 16 files changed, 610 insertions, 82 deletions, plus new untracked files. Nothing has been committed or pushed since `40f99c6`.

**Modified (tracked, uncommitted):**
```
cogs/admin_cog.py         cogs/ally_cog.py           cogs/patreon_cog.py
cogs/patrol_cog.py        cogs/pvp_cog.py            cogs/tunnel_cog.py
config.py                 db/models.py               services/ally_service.py
services/battle_service.py                            services/economy.py
services/patreon_service.py                            services/patrol_service.py
services/shakedown_service.py                          utils/first_run.py
utils/icons.py
```

**Untracked (new files):**
```
alembic/versions/2190a35c174f_add_growth_perk_choice.py
alembic/versions/df532d94924d_add_last_active_at.py
assets/icons/Electric_Webbing.png
assets/icons/Spider_Bots.png
assets/icons/biomorphic_webbing.png
assets/icons/organic_webbing.png
assets/icons/stealth_mode.png
assets/icons/venom_blast.png
scratch_stderr.log
scratch_stdout.log
```

**DB state**: the local dev SQLite DB (`spidey.db`) is already migrated to head (`df532d94924d`) — both new migrations have been applied and tested against. Committing the migration files is just catching the repo up to what the DB already reflects.

`scratch_stderr.log` / `scratch_stdout.log` are leftover debug output from the ngrok/Windows hang investigation earlier this session (see GAME_DESIGN.md §20) — look safe to delete once confirmed unneeded, but haven't been checked or removed yet.

## What's actually done and verified

Per live functional testing this session (not just code review):
- **Tier detection** — real bug found & fixed: Patreon API v2 needs `fields[tier]=title` explicitly requested or tier data arrives empty. Verified against the real API with a real stored access token.
- **Arachnid roster correction** — the roster went through a real correction pass mid-session (an earlier implementation had Organic Webbing as a 25%-chance roll and a wrong "Enhanced Resilience" perk; both were wrong and got replaced). GAME_DESIGN.md §9.2 reflects the corrected, current version only.
- **Stealth Mode** — functionally tested: unsubscribed+active = normal, Symbiote+active = normal (threshold not met), Symbiote+inactive 25min = fully protected with the thief's wallet genuinely untouched.
- **Sonic Dampener** — functionally tested: no-tier vs Shocker unaffected, Symbiote vs Shocker scales 20→26 damage exactly (the +30%), Symbiote vs any other boss unaffected.
- **`PATREON_ARACHNID_TIER_NAME`** confirmed working against the real `.env` — exact value `"Arachnid"`, verified live via the user's own trial subscription + `/patreon link`.
- A stray test artifact (`growth_perk_choice='allies'` left on a test account from earlier manual testing) was found and manually cleared — worth checking other test accounts if this comes up again.

**Not yet simulation-verified** (real gap, not just caution):
- **Venom Blast**'s `2x attack roll` bonus damage is a formula-based guess, not tuned. A Monte Carlo reconstruction attempt this session didn't match the documented 70-75%/17-25% boss win-rate benchmarks even after fixing gadget-priority bugs, so the original tuning script's exact policy isn't recoverable — the shipped number is "what the copy literally promises," not a validated balance point. Worth a real check once there's actual boss-fight play data with Venom Blast live.
- **Electric Webbing / Spider Bots** 20% proc chances were picked by comparison to existing gadget base_chance values (0.25-0.55), not a binary-search balance pass the way boss fights themselves got.

## What's designed but has zero code yet

All numbers are locked in [booster_perk_tier_design.md](file:///C:/Users/Vatsal%20Goel/.claude/projects/d--VS-Code-spidey-bot/memory/booster_perk_tier_design.md) (this session's memory) and summarized in GAME_DESIGN.md §9.5:
- **Higher Suit Integrity** (25-35% less crime-tier patrol damage)
- **Higher Reputation XP** (25-35%, mutually exclusive with Supportive Allies)
- **Supportive Allies** (25-35% decay reduction, mutually exclusive with Higher Reputation XP)
- **Quicker Web Brewing** (5min → 3min, not tier-gated — everyone gets it)
- **Camera tiers** (Bronze/Silver/Gold via `upgrade_level` on the `camera` item — break-chance reduction + photo-quality-bump chance, costs $1,000/$3,000). Icons are done; nothing else built. User explicitly said "just log the icons" when last asked whether to build this now — so this is parked, not forgotten.

**Important**: none of the five above are part of the Patreon Arachnid/Symbiote roster. They belong to a track that hasn't been assigned yet — see the open question below.

## Real open question: which perk track do the still-unbuilt perks belong to?

Two tracks exist and must not be conflated (a mistake that was actually made and corrected once already, 2026-08-18):
1. **Patreon tiers** (Arachnid/Symbiote) — the one that's built and live, described in GAME_DESIGN.md §9.1-9.3.
2. **Server-boost-exclusive perks** (discord.gg/spider-man Nitro boosting) — a separate track. Was built once (a suit-damage-reduction perk gated on Discord Server Booster status via the Members intent), then **fully reverted** per explicit user instruction (see commits `76dbc26` "Revert Server Booster perk", `a05e719` "Revert Members intent hold-off", both already committed and on `main`). Has not been rebuilt since. `get_growth_choice`/`set_growth_choice` in `patreon_service.py` were originally built for *this* track — a past session mistakenly wired them to Patreon tier_rank instead (corrected 2026-08-18, see GAME_DESIGN.md §9.4).

The five not-yet-built perks above were designed without a final call on which track they land in. Before writing any code for them, that needs deciding — likely worth asking the user directly rather than guessing, since the Server Booster track was explicitly reverted once already and re-adding server-boost-gated code (which needs the Members intent re-enabled) is exactly the kind of change that warrants a check-in before starting.

## Suggested next steps, in order

1. **Commit the uncommitted work.** This is the highest-value, lowest-risk next action — a full working feature (Arachnid/Symbiote perks + Stealth Mode + Windows tunnel fixes) has been sitting uncommitted through an entire design/build/test cycle. Splitting into a few logical commits (e.g. "Add Patreon Arachnid/Symbiote perk tiers", "Fix ngrok on Windows dev machines", "Add last_active_at for Stealth Mode") would probably read better in history than one giant commit, but that's a style call for whoever commits it.
2. Decide on the scratch log files (`scratch_stderr.log`/`scratch_stdout.log`) — delete if genuinely just debug leftovers, or `.gitignore` them if this kind of file will keep recurring during local Windows debugging.
3. Get a decision from the user on the open question above (which track owns the 5 unbuilt perks) before writing any code for them.
4. Once real playtest data exists with Venom Blast live, revisit its tuning against the documented boss win-rate benchmarks.
