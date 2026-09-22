# How PlayMoreBlitz works, and how to fix it

This is the map of the whole system: what runs where, what is stored, how a game becomes an analysis, who is allowed to
do what, what the alerts mean, and what to do when something breaks. It is written for the person running the bot and
for whoever (a person or an AI assistant) has to change it later. `README.md` is the reference for commands and
settings; this file explains how the parts fit together. The private, machine-specific details (host names, paths,
IDs of the real test server) are not in this public repository: see "Local details" at the end.

Contents: 1 The picture · 2 The machines · 3 The bot · 4 The database · 5 Monthly totals · 6 The analysis flow ·
7 The method · 8 The worker · 9 Reviews, exports, usage · 10 Who can do what, where · 11 Watching over it ·
12 Runbook · 13 Developing and testing · 14 Decisions and limits · 15 File map

---

## 1. The picture

```
   Chess.com API ─┐                                     ┌── Discord (the server, DMs, slash commands)
   Lichess API  ──┤                                     │
                  ▼                                     ▼
            ┌──────────────────────────  MINIX (always on)  ──────────────────────────┐
            │  bot.py  (discord.py)                                                    │
            │    commands ──► SQLite database  ◄── refresh every 30 min (totals)      │
            │    loops:  refresh · obit delivery · health · heartbeat · daily posts    │
            │                    │                                                     │
            │   analysis queue  = table game_analysis  (one row per game, both sides)  │
            │                    ▲   ▲                                                 │
            │   worker_gateway.py│   │ bot puts new games in (analysis_feed.py)        │
            └────────────────────┼───┴─────────────────────────────────────────────────┘
                        SSH (key locked to the gateway; over the private network)
            ┌────────────────────┼─────────────────────────────────────────────────────┐
            │  ELITEDESK (Windows, faster CPU)                                          │
            │  worker.py  pulls jobs ─► fetches each game itself ─► Stockfish ─►        │
            │             analysis.py figures ─► sends them back                        │
            └───────────────────────────────────────────────────────────────────────────┘
   The developer's PC: edit code, run tests, commit, push to GitHub; the Minix pulls.
```

Two separate jobs share the bot:

1. **Monthly totals** (the original bot): who has played how many rated blitz games this month, their record and rating
   change, and the optional "100GOB" challenge (100 Games Of Blitz). Comes straight from the sites.
2. **Game analysis** (added later): every game a registered player plays is put in a queue; a stronger machine runs the
   Stockfish engine over it and reports accuracy and mistakes back. Members can then see it (`!lastgame`, `!mystats`),
   get a private review (`!obit`, `/obit`) or a spreadsheet (`!export`, `/export`).

The bot never trusts the worker with anything beyond figures, and never keeps a game's moves.

## 2. The machines

| What | Role | How to look at it |
| --- | --- | --- |
| **Minix** (small always-on Linux box) | Runs the bot (systemd service `playmoreblitz`), holds the database and the queue, runs the gateway the worker calls, runs the nightly backup timer. | `ssh MINIX`; log: `journalctl -u playmoreblitz -f`; code in `~/playmoreblitz`; its own venv `venv/`. |
| **EliteDesk** (Windows 11) | Runs the analysis worker as a Windows scheduled task (`PlayMoreBlitzWorker`, starts at boot, runs as SYSTEM, restarts on failure, below-normal priority). Has Stockfish. | `ssh ELITEDESK`; log: `C:\ProgramData\pmb-worker\worker.log`; files in `C:\Users\<user>\pmb-worker\`; its Python venv `C:\Users\<user>\pmb-analysis\venv` (keep it: the service uses it). |
| **Developer PC** | Editing, tests, git. | `C:\Users\<user>\playmoreblitz-bot` (has the private test fixtures; never commit them). |
| **GitHub** | The copy both machines pull from (`main`). | `git push` from the PC; `git pull` on the Minix. |
| **Discord** | The application/bot, the server, channels, slash commands. | Developer Portal for the token and invite; Server Settings for roles. |
| **Storage** | The database on the large drive (`PLAYMOREBLITZ_DB` in the Minix `.env`); nightly backups on a separate USB drive, kept `BACKUP_KEEP_DAYS` (100) days. | See section 4. |

The Minix and the EliteDesk reach each other over a private network. The worker connects **to** the Minix (never the
other way round), so the Minix needs no open door to the EliteDesk.

## 3. The bot

`bot.py` is the whole Discord side. Key ideas:

**Start-up (`on_ready`).** Logs who it is and where the database is, warns about settings problems, then starts these
loops (each starts only once, even if Discord reconnects):

| Loop | How often | What it does |
| --- | --- | --- |
| `refresh_loop` | every `REFRESH_INTERVAL_MINUTES` (30) and once at start | Closes any finished month still open, refreshes every active player's running totals (which also feeds new games to the analysis queue), posts month-end things if due, prunes old usage counts. Tracks failing cycles. |
| `obit_loop` | every 60 s | Sends the reviews people asked for once their game is analysed (see 9). |
| `health_loop` | every 5 min | Checks the analysis worker, and the backups; sends alerts (see 11). |
| `heartbeat_loop` | every 60 s, only if `HEARTBEAT_URL` is set | Pings the outside monitor to say the bot is alive. |
| `daily_posts` | 9am UK | The 100GOB sign-up call and the month-end table, if due. |

It also registers the Delete button for DMs (so old buttons keep working after a restart) and registers the slash
commands (`/obit`, `/export`) in each server that has an allowed channel. If Discord refuses that with "Missing Access",
the bot was invited without the `applications.commands` scope: re-authorise it (README, "Slash commands").

**Where a command may be used** is decided by one global check, `_in_allowed_channel`, registered with `@bot.check`
(it once got detached from its function by a bad edit: `tests/test_obit.py` now runs the registered checks the way
discord.py does, so that cannot go unnoticed again):

- In a server: only in `ALLOWED_CHANNEL_IDS`, and never the system commands.
- In a DM to the bot: `!obit` and `!export` (for anyone who is on the server and registered), `!clear` (deletes the bot's
  own messages in that DM, old ones included: Discord only lets a bot delete its own messages there), and the admin system
  commands `!analysisq`, `!queuemonth`, `!closemonth`, `!setowner`, `!usage` (admins only). Nothing else.
- Slash commands check the channel themselves.

**Ownership.** `players.added_by` is the Discord ID of the member the account belongs to. "My account" in `!mystats`,
`!obit`, `!export` means the accounts with `added_by` equal to the person asking. An admin who registers someone else's
account without naming them becomes its owner; `!setowner <username> <member> [site]` fixes that (the member must be on
the server; non-admins keep to one account per site).

**Membership.** "On the server" is checked with `guild.fetch_member`. In a DM the check is strict: not confirmed means
refused. For an action that isn't a DM (`!add @member`, delivering a queued review) only a confirmed "not a member" blocks.

**Keeping the channel quiet.** `!obit` and `!export` reply by DM; `/obit` and `/export` answer only the person who asked
(ephemeral). Admin commands are DM-only. The bot never pings anyone by default (`AllowedMentions.none()`), except the one
person whose review couldn't be delivered.

**In memory only:** the per-player game cache behind `!mystats` (`gamecache.py`, bounded, lost on restart), the alert rate
limiter, throttles for site lookups and exports.

## 4. The database

One SQLite file (`playmoreblitz.db`). Every access opens its own short connection through `store.transaction()`
(`store.py`); the schema is `CREATE TABLE IF NOT EXISTS`, run on every connection, so a new table appears on the next
start with no migration step. Column additions to old tables are in `store._migrate`. Writes that must not race use
`BEGIN IMMEDIATE`. Times are UTC (ISO text in the old tables, epoch seconds in `game_analysis` and the newer ones).

| Table | One row per | Holds |
| --- | --- | --- |
| `players` | account (`site`, `username`) | `added_by` (owner's Discord ID), `active` (0 after `!remove`; history kept). Username matching ignores case. |
| `monthly_results` | account and month (`YYYY-MM`) | start and end rating, games, wins, draws, losses, `in_100gob`, `last_game_at` (the watermark: only games after it are fetched), `closed_at`, `refreshed_at`, `refresh_error`. A player's first row is the month they registered: **history begins at registration, nothing earlier is filled in.** |
| `gob_signups` | account and month | Sign-ups for a month whose row doesn't exist yet; applied when the row is made. |
| `announcements` | kind and month | What the bot has posted on a schedule (`signup_call`, `month_end`), so a restart never posts twice. |
| `game_analysis` | **game** (`site`, `game_id`) | The queue and the results in one table, both sides of the game. See 6. |
| `analysis_workers` | worker name | When each worker last asked for work (used by `!analysisq` and the alerts). |
| `obit_requests` | (person, game) | A review asked for and not yet sent. Deleted when it is sent or given up on. |
| `usage_daily`, `usage_seen` | day and command; day and person | Counts of use for `!usage`. Nothing else; dropped after 35 days. |

`game_analysis` columns, grouped: identity (`site`, `game_id`, `month`, `ended_at`); the game (`time_control`, `result`
= white/black/draw, `ending`, `opening_site`, `eco_site`, both usernames, both ratings and rating changes); the queue
(`status`, `skip_reason`, `priority`, `attempts`, `last_error`, `queued_at`, `claimed_at`, `claimed_by`, `analysed_at`);
the run (`engine`, `nodes`, `method_version`, `plies`); the figures per side (`*_accuracy`, `*_acc_opening/middle/end`,
`*_inaccuracies/mistakes/blunders`, `*_acpl`); `middle_ply`, `end_ply`, `eval_ply20` (engine score after 10 moves,
centipawns, White's view); `evals` (the score after every ply, packed two bytes each); `clocks` (the mover's clock after every ply, in tenths of a second, two bytes each; NULL if the site gave none: a daily game, or a game analysed before method 3); `moments` (JSON list of
`[ply, "i"|"m"|"b", points lost]`, both sides); `site_*_accuracy` (the site's own numbers, kept apart); `shape` (unused).
**The moves themselves are never stored** (the clocks are: they say how long each move took, not what it was).

**Looking inside** (on the Minix; the file is where `PLAYMOREBLITZ_DB` in `.env` says):

```bash
sqlite3 "$DB" "select site, username, added_by, active from players"
sqlite3 "$DB" "select status, count(*) from game_analysis group by status"
sqlite3 "$DB" "select * from analysis_workers"
sqlite3 "$DB" "select game_id, status, skip_reason, attempts, last_error from game_analysis where status in ('failed','skipped') limit 20"
```

**Backups.** `backup.py` (systemd timer `playmoreblitz-backup.timer`, nightly) makes a consistent copy with SQLite's own
backup, checks it, and only then puts it in place as `playmoreblitz-YYYY-MM-DD.db` in `BACKUP_DIR`, then removes copies
older than `BACKUP_KEEP_DAYS`. It refuses to run if `BACKUP_DIR` is missing (an unmounted disk) rather than fill the wrong
disk. To restore: stop the bot, copy a backup over the live file, start the bot. Check with
`systemctl list-timers playmoreblitz-backup.timer` and `ls` of the folder.

## 5. Monthly totals

1. **Register** (`!add name site`): the name is checked against the site and stored in the site's spelling; the start
   rating is read; a `monthly_results` row for the current month is created. Only the current month's games so far are
   counted (no backfill).
2. **Refresh** (`refresh.py`, every 30 min, and once right after `!add`): fetches only games that ended after the
   watermark (`last_game_at`), adds them to the totals, moves the watermark, all in one write. A failed refresh changes
   nothing except `refresh_error`. Every fetched game is also offered to the analysis queue (`analysis_feed.feed`), which
   can never break the totals (its failures are logged and swallowed).
3. **Close a month** (`monthend.py`): when a month has ended, every active player's **whole month is refetched** and the
   totals rewritten from that (the running totals are not trusted for the final figure); next month's rows are created;
   the final table is posted at 9am UK. If any player's fetch fails, nothing is written and the failures are named.
   `!closemonth` (admin, DM) runs the same job by hand; it never closes or posts twice.
4. **Show it**: `!results [month]`, `!mystats`, `!mystatsfull`, `!history`, `!lastgame`. `!results` reads the stored totals only.
   `!mystats` fetches a month's games (through `gamecache`) because openings and splits need the details, so it is
   throttled (`COOLDOWN_SECONDS`) and polite to the sites (`sources.py`: serial per site, timeouts, spacing).

Only **rated standard blitz** games count. Anything unexpected from a site (an unknown result code, a missing rating
change) raises instead of being guessed at.

## 6. The analysis flow

The path of one game, and every place it can stop:

```
 1 played on Chess.com/Lichess
 2 refresh (30 min) or !add or !queuemonth or /obit's "look at the sites" fetches it
 3 analysis_feed.feed → game_records.record → analysis_queue.queue_games   → row in game_analysis, status "pending"
 4 worker asks (SSH → worker_gateway "claim N")                             → status "claimed" (attempts+1)
 5 worker fetches the game from the site itself (moves live in its memory only; the clocks come with it and are kept)
 6 worker: standard start? long enough? → else "release" with a skip reason → status "skipped"
 7 Stockfish evaluates every position (200,000 nodes each) → analysis.summarise → figures + moments
 8 worker "submit" (evals base64, in chunks ≤ 60,000 characters)             → validated (analysis_queue._problem)
 9 stored: figures, evals, moments, method_version, analysed_at              → status "done"
10 shown by !lastgame / !mystats / !obit / !export
```

**What gets queued (step 3).** One row per game (both sides), if either player is a registered member (`queue_games`
reports `not_a_member` otherwise). **Monthly tiers per player:** their 1st to `ANALYSIS_FULL_PRIORITY_GAMES` (500) games
of the month are normal priority; up to `ANALYSIS_MAX_GAMES` (1000) go to the back (`priority` 1, analysed when nothing
else waits); beyond that the row is stored as `skipped` with reason `over_monthly_limit` and not analysed. A game already
queued is left alone (safe to offer again).

**Status machine.**

| From → to | When |
| --- | --- |
| (new) → `pending` | queued, within the tiers |
| (new) → `skipped` (`over_monthly_limit`) | beyond the monthly maximum |
| `pending` → `claimed` | a worker claims it: `attempts` +1, `claimed_at`, `claimed_by`. Order: `priority` (−1 urgent, 0 normal, 1 low), then newest first. |
| `claimed` → `done` | the worker submits a valid result |
| `claimed` → `skipped` | the worker releases it with a reason: `not_standard_start`, `too_short` (under `MIN_PLIES` 6), `unavailable` (couldn't get the moves) |
| `claimed` → `pending` | released without a reason (an error), or the claim lease (`ANALYSIS_CLAIM_MINUTES`, 30) ran out (worker died) |
| `claimed`/`pending` → `failed` | `attempts` reached `ANALYSIS_MAX_ATTEMPTS` (5) |
| `failed` or `skipped: over_monthly_limit` → `pending`, priority −1 | a member asked for that game's review (`prioritise`): an explicit request overrides the limit and retries a failure |
| `done` → `claimed` → `done` | **re-run**: once nothing else waits, the claim also hands out `done` games analysed by an older `method_version`, so a method change re-analyses everything, newest first. The old figures stay in place until the new ones replace them (so "analysed" means "has figures", i.e. `analysed_at` is set, not "status is done"). |

**Priority −1 (urgent)** is only ever set by `analysis_queue.prioritise`, from `!obit`/`/obit`.

**Validation (step 8).** `analysis_queue.submit` rejects a result whose figures are out of range, whose `evals` length
doesn't match `plies`, or whose moments don't agree with the side's counts (each side's list must have exactly as many
inaccuracies/mistakes/blunders as its counts say). A rejected result leaves the game `claimed` (it returns to `pending`
when the lease expires).

**The gateway (`worker_gateway.py`).** The worker's SSH key on the Minix is locked in `~/.ssh/authorized_keys` to one
program:

```
command="<venv python> <repo>/worker_gateway.py desk",restrict ssh-ed25519 AAAA...  pmb-worker
```

The worker's name (`desk`) is fixed by that line, so one worker can't pretend to be another. It understands only
`hello` (protocol and method version, the server's clock), `claim N`, `submit`, `release`; JSON in and out; it reads only
a whitelist of settings from `.env` and never the Discord token. No shell. The worker checks the gateway's
`method_version` at start-up and refuses to run if the two differ, which is why updates are done in a fixed order (section 8).

**Consumers.** `analysis_reports.py` reads the table (`player_games`, `month_summary`, `monthly_accuracy`,
`waiting_count`) and `render_analysis.py` turns it into text (`!lastgame`, the analysis block of `!mystats`, `!analysisq`).
`!analysisq` (admin, DM) shows counts by status, the low-priority backlog, how long the oldest game has waited, when each
worker last asked, and how many games await a re-run.

## 7. The method

`analysis.py` follows the method Lichess publishes for its computer analysis, so numbers read on the same scale.
`METHOD_VERSION` (now 3: version 3 added the clocks) is raised whenever a change alters the figures or what is kept; older rows are then re-analysed.

- Scores are `("cp", n)` or `("mate", n)` from White's point of view. The start position counts as 15 centipawns.
- **Win %** = 50 + 50·(2 / (1 + e^(−0.00368208·cp)) − 1), with the evaluation capped at ±1000 centipawns (a mate is the cap).
- **Move accuracy** = 103.1668·e^(−0.04354·drop) − 3.1669 (+1), clamped to 0–100, where `drop` is the win % the mover lost.
  **Game accuracy** = the mean of a volatility-weighted mean and a harmonic mean of the moves.
- **Judgement** (inaccuracy / mistake / blunder) uses *winning chances* thresholds 0.1 / 0.2 / 0.3 on the **raw, uncapped**
  centipawns (a change in win % is twice a change in winning chances). A move that is the engine's own best is never
  judged. `Moment.lost` is the win-probability points a move cost the player who made it.
- **Phases** (opening / middlegame / endgame) come from `divider.py`, a port of Lichess's divider; `middle_ply` and
  `end_ply` are where they start (NULL if never reached).
- **Engine settings** (worker): Stockfish at 200,000 nodes per position, 8 threads, about 3 seconds a game. More time
  did not change accuracy much; the counts of mistakes differ from Lichess's own (theirs come from noisier evaluations),
  so present them as "the bot's own estimate", accuracy being the trustworthy headline.
- A clock-free time-trouble signal: "lost on time while equal or better" (used in the review's "Interesting" section).

## 8. The worker

`worker.py` (with `game_data.py`, `analysis.py`, `divider.py`) loops: `hello` → `claim` a batch (20) → for each game fetch
it (`Http`: Chess.com monthly archives, Lichess game export, spaced by `LICHESS_MIN_INTERVAL_SECONDS`), parse it
(`game_data.py`), run the engine (`Engine`, python-chess driving Stockfish), build the result (`make_result`), and
`submit`/`release`. When the queue is empty it sleeps `IDLE_SLEEP_SECONDS` (300). `--check` tests the connection and the
engine and stops; `--once` stops when the queue is empty. Settings are in `worker.env` beside it
(`worker.env.example`). The log is `LOG_FILE`.

**The Windows service.** A scheduled task `PlayMoreBlitzWorker` (XML must be saved as UTF-16 with a BOM), running as
SYSTEM at boot, restart on failure. SYSTEM can't use the user's SSH key, so it has its own copies in
`C:\ProgramData\pmb-worker\` (key with an ACL for SYSTEM and Administrators only, and its own `known_hosts`).

**Windows quirks that bit before.** `ssh.exe` hangs when driven through pipes or with big bodies, so `worker.py`'s
`Gateway` passes stdin/stdout through temp files and sends submits in chunks of at most 60,000 characters. A `2>nul` in
a command sent to the EliteDesk breaks it under `cmd`. Write `.ps1` scripts and copy them over rather than quoting through ssh.

**Deploying a change.**

- *Bot-only change* (commands, rendering, settings): commit and push on the PC; on the Minix `git pull`; restart the bot
  (`sudo systemctl restart playmoreblitz`, needs the owner's sudo). The worker is untouched.
- *Anything that changes the method or the worker* (`analysis.py`, `divider.py`, `game_data.py`, `worker.py`,
  `worker_gateway.py`, or the queue's rules): **in this order** — stop the worker task; pull on the Minix; copy the changed
  worker files to the EliteDesk; start the worker task. The worker refuses to start against a gateway with a different
  `method_version`, so a half-done update stops safely instead of writing bad rows. Raise `METHOD_VERSION` if the figures
  or what is stored change, and the whole history is re-analysed by itself (about 3 s per game).
- *Roll back:* `git revert` (or check out the previous commit) and pull again on the Minix; restore a database backup if
  a bad change wrote bad rows.

## 9. Reviews, exports and usage

**`!obit` / `/obit`** — a private review of one of your own games (Nate Solon's OBIT: Openings, Blunders, Interesting,
Takeaway). `obit.py` reads the game link or id (`candidates`), finds it among the caller's own games (`find_game`) or takes
their latest (`latest_game`, after looking at the sites first if the throttle allows), and:

1. if the game is analysed, sends the review at once (`render_obit.py`);
2. if not, `analysis_queue.prioritise` moves it to the front (priority −1), a row goes into `obit_requests`, and the
   channel/DM says it is being analysed;
3. `obit_loop` (every 60 s) sends the review when the game reaches `done`, or an apology if it can't be analysed
   (`failed`, `skipped`) or took over 24 hours. It first re-checks the person is still on the server; if not, the request
   is dropped silently. Whoever deletes the request row is the one that answers it (nobody answers twice); a failed DM
   puts it back to retry.
4. Up to `MAX_WAITING` (3) reviews per person; `!obit` works only in a DM (in the channel it gives a self-deleting hint);
   `/obit` answers only the person asking. Every DM the bot sends carries a **Delete** button (`DeleteButton`, persistent),
   because Discord doesn't let anyone delete a bot's message in a DM themselves.

### The review, element by element (`render_obit.py`)

Nothing in the review is worked out by an engine when it is sent. The worker analysed the game earlier and stored figures in one
`game_analysis` row; `render_obit.py` only reads that row and words it. The code is commented at every step; this is the map.

**What the row gives it.** `scores`: the engine's evaluation after every ply (a ply is one side's move; ply 1 is White's first
move, ply 2 Black's first), from White's point of view, in centipawns (100 = a pawn) or a forced mate. `moments`: the moves called
an inaccuracy, mistake or blunder, as (ply, verdict, points of winning chance lost); odd plies are White's. Plus each side's counts
and accuracy overall and by phase, the engine's score after 10 moves each (`eval_ply20`), and the game's facts. The review is
written from one player's side, so scores are flipped for Black.

**Two scales.** Pawns (what the engine says), and *winning chance* in % (`analysis.win_percent`), which is how "how much did that
cost" and "was I clearly winning" are decided. It is not a straight line:

| Engine score for you | 0.0 | +0.5 | +1.0 | +2.0 | +3.0 | +4.0 | -0.55 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Your winning chance | 50% | 55% | 59% | 68% | 75% | 81% | 45% |

| Part | What it says | Where each piece comes from |
| --- | --- | --- |
| Heading | Who, site, date, time control; "You won by timeout as White against …" and a link | The row's usernames, ratings, `ended_at`, `time_control`, `result`, `ending`. |
| **O**pening | The opening family and ECO; accuracy overall and by phase; the weakest phase; the score after 10 moves each | Name and ECO are the *site's* own (`opening_family` tidies the two sites' spellings). Accuracy is the worker's 0-100 figure for the player's moves (a phase the game never reached shows "-"). "Weakest phase" appears only if two or more phases are known and best and worst are 8+ points apart. "After 10 moves each" is `eval_ply20`, from the player's side. |
| **B**lunders | Both sides' counts; the player's worst moments | Counts are the row's totals. Each listed moment is one of the player's own flagged moves, biggest loss first (ties: earliest), at most 5: "• 11. inaccuracy, −6% (+0.8 → +0.1)" = move 11, an inaccuracy that gave away 6 points of winning chance, the engine's score going from +0.8 before the move (the score after the ply before it) to +0.1 after it. What the move *was* is not known: moves aren't stored. Lichess lines link to the position *before* the move. |
| **Time** (only when the game has clocks) | Seven checks on how the time was spent, each with an icon (✓ good, ⚠ worth a look, • information) | `clock_review.py`; see "The Time section" below. |
| **I**nteresting | Up to seven notes, or "Nothing unusual" | Six tests on the evaluation, below, and two on the clock. |
| **T**akeaway | A prompt | Nothing computed: the takeaway is the player's to write (a tick-list is planned). |

**How "Interesting" is decided.** Every test works on the player's winning chance after each ply (`curve`), from the engine's scores. There is
no clock data, so the clock test is a proxy. (`NOT_WORSE` = 45%, `CLEARLY` = 75%, both at the top of the file.)

1. **The clock** (only if the game ended on time): what the position was worth when the flag fell (the last score). You *lost* on time
   with a chance of 45% or more (equal or better): "the clock, not the position, decided this one". You *won* on time with under 45%:
   "your opponent's clock did the work"; or with 45% up to 55% (a **level** position, about -0.55 to +0.5 pawns): "the clock, not the board,
   decided this one", because winning on time from a level position is unusual. Winning on time when clearly better is not noted.
2. **A win thrown away**: you didn't win, but at your peak your chance was 75% or more (about +3.0): "you were clearly winning (+4.0 after 10...)
   and lost", naming the move at the peak.
3. **A lost position saved**: you didn't lose, but at your low your chance was 25% or less (about -3.0): "you were in real trouble … and won anyway".
4. **Chances the opponent gave you**: the opponent's flagged moves that are mistakes or blunders (not inaccuracies): how many and the biggest
   ("cost them 25% (+0.3 → +2.1)": what it did to the engine's score from your side), asking whether you used it.
5. **The turning point**: the single move that changed the evaluation most, whoever played it, if it cost 10 or more points of winning chance
   (`TURNING`, a mistake or worse). If it was the opponent's it is already named by test 4, so this note appears only when it was **your own**
   move: "The biggest swing of the game was your own 11.: it cost you 25% (+1.2 → -1.5)." Two equal swings: the earlier counts.
6. **A chance given back**: an opponent's mistake or blunder answered on the very next move by one of *your* flagged moves (any size):
   "Your opponent's blunder on 7... (−25%) was answered by your own mistake on 8. (−12%): the chance was given back straight away."
   If it happened more than once it names the costliest reply and says how many times.

Tests 2 and 3 can't both hold unless the game was drawn and test 1 needs a decisive result, so at most five notes appear. None holding is a
normal, steady game. Tests 4 to 6 use only the flagged moves and the evaluation curve, so they need no clock data.

**The two clock notes under Interesting** set the clock against the engine's verdict on the same move (they need the clocks, so they
appear only when the game has them): **your longest think**, if it took over 10% of the base time (Nate Solon's "a position where you got
stuck"): "it ended in a blunder (−22%). What were you stuck on?" or "the move held up, so the time was well spent"; and **fast slips**: your
mistakes and blunders played in under 5 seconds, how many and the quickest.

**The Time section** (`clock_review.py`, from the clocks the sites give after every move). A move's time is the mover's clock before it (the
base for a first move, otherwise their previous clock) minus the clock after it, plus the increment; White plays the odd plies. The rules and
thresholds are those of the time graph in the owner's chess-journal wiki (all named constants at the top of the file), plus the seventh check:

| # | Check | Rule |
| --- | --- | --- |
| 1 | Opening speed | Your first 10 moves' time as a share of the base: 15% or less ✓ "nicely quick"; over 25% ⚠ "too slow"; between, •. |
| 2 | Longest thinks | Your three slowest moves; ⚠ if one alone took over 10% of the base. Then, if you made 15+ moves, whether two of the three fell in moves 15-25 (the middlegame). |
| 3 | Blitzed moves | Of your moves after move 12 (needs at least 5): ⚠ if over 40% took under 5 seconds. |
| 4 | Time trouble | ⚠ at the first move where your clock fell below 10% of the base; otherwise ✓. |
| 5 | Pace against a strong player | Your clock at each move against a reference curve: the average fraction of the base time a strong blitz player had left at that move (94 rated 3+0 and 5+0 games, Chess.com, July 2026, from the wiki). Only for 3+0 and 5+0 games. ⚠ if you were ever more than 15% of the base behind; ✓ if ahead throughout or close. |
| 6 | Total | Time you used against your opponent's and your budget (base plus increment per move); what was left at the end; • if you used under 45% of the budget. |
| 7 | Pace against your opponent | The clock lead (your clock minus theirs) after each move both made, shown at moves 10, 20, 30 and the end. One sentence: a lead of over 3% of the base that slipped into a deficit (⚠), a deficit won back (✓), or who finished ahead or behind by over 3% (✓ or ⚠); otherwise "the clocks finished level". If the game was decided on time it says which: "You won on time: your opponent's clock ran out" (with "although at the last readings you were 29s behind" if the last readings had you behind by over 3%), or "You lost on time: your clock ran out": a clock is only read after each move, so the last reading can't say whose time ran out, but the result can. |

**How the clocks get here.** The worker asks Lichess for them (`clocks=true`) and reads Chess.com's `[%clk …]` comments; `game_data.py` turns
both into one number per ply (or None if any ply lacks one). The worker sends them base64-encoded beside the evaluations, `worker_gateway.py`
decodes them, `analysis_queue.py` checks there are exactly two bytes per ply and stores them. Because this changed what is kept the method
became version 3, so every game analysed before is re-analysed once (the worker re-fetches it) and gains its clocks.

**A real example** (names changed; a 21-ply game White won when Black's flag fell). The stored evaluations after each ply were
`+20 +16 +18 +35 +29 +33 +34 +42 +4 +1 -6 +26 0 +6 +6 +61 +64 +157 +89 +76 +9` centipawns, and the flagged moves `(16, inaccuracy, 5.0)`,
`(18, inaccuracy, 8.2)`, `(21, inaccuracy, 6.1)`. The player was White, so odd plies are theirs and the review read:

- *Opening*: the score after ply 20 was +76, so "after 10 moves each the engine had you at +0.8"; opening accuracy 96% against middlegame 77% is 19
  apart, so "your weakest phase here was the middlegame"; the game never reached an endgame, so that phase is "-".
- *Blunders*: ply 21 is the player's only flagged move (16 and 18 are Black's): move 11, an inaccuracy, −6% (the stored loss is 6.1 points, shown rounded), score before it
  +76 (after ply 20) and after it +9: "(+0.8 → +0.1)". Their counts "1 inaccuracy" against the opponent's "2 inaccuracies".
- *Interesting*, test by test: the game ended on time and the player won, and the last score (+9 cp) is a 51% chance: not under 45%, so not "won
  from a worse position", but inside the 45-55% band, so the clock test fires: "You won on time in a level position (the engine had it at +0.1
  for you): the clock, not the board, decided this one." (Before that band was added the review said "steady game" here.) The win chance peaked
  at 64% (ply 18) and never fell below 49%, so neither 75% nor 25% was reached, and the opponent's two flagged moves were inaccuracies, not
  mistakes or blunders. One note.

**Limits worth knowing.** It can say where, how big and what it did to the position, never what the move was. "Interesting" is only as good as
its four tests: an instructive game with no big swing reads as steady. The engine's counts of mistakes differ from Lichess's own (theirs come
from noisier evaluations), so the figures are described as the bot's own estimate. To change what counts as interesting, edit the four tests
and the three thresholds (`CLEARLY`, `NOT_WORSE`/`LEVEL`, `TURNING`) in `render_obit._interesting`, and the tests in `tests/test_obit.py` (search "what stands out").

**`!export` / `/export`** — `export_data.py`: one CSV per account (a username on a site), one line per game the bot holds
for the period (this month, last, week, a month, all), oldest first, analysed or not; or `summary` for one line per account.
UTF-8 with a BOM (Excel), CRLF, text cells beginning with `= + - @` are defused against spreadsheet formulas. One export
per person per 30 s. Nothing is stored: files are made on request.

**`!usage`** — `usage.py`: counts per day of each command, the distinct people seen, errors, reviews and exports sent.
Counts only, 35 days.

## 10. Who can do what, where

| | In the server channel | In a DM to the bot | Slash |
| --- | --- | --- | --- |
| Anyone | `!add` (own account, one per site), `!remove` (own), `!results`, `!mystats`, `!mystatsfull`, `!history`, `!lastgame`, `!100gob`, `!100gobnext`, `!helpblitzbot` | `!obit`, `!export` (registered members who are on the server) | `/obit`, `/export` |
| Admins (`ADMIN_USER_IDS`) | as above, plus `!add` for others (naming the member), `!remove` anyone; exempt from cooldowns | `!analysisq`, `!queuemonth`, `!closemonth`, `!setowner`, `!usage` | |
| Everyone else in a channel that isn't allowed | ignored | ignored | refused privately |

## 11. Watching over it

Alerts arrive as a DM headed "⚠ PlayMoreBlitz" to `ALERT_USER_IDS` (default: just Bob). Each is repeated at most every 6
hours while it lasts, and forgotten when it clears (so a return is reported at once); an error is repeated at most every
30 minutes. Logic is in `monitoring.py`; the loops are in `bot.py`.

| Alert says | It means | First thing to check | Usual fix |
| --- | --- | --- | --- |
| "!x from <id> hit an unexpected error (Type: message)" | A command raised something other than a normal refusal; the ID is whoever ran it. | `journalctl -u playmoreblitz` for the traceback (it says `!x failed for <id>`) and the lines just before it about that ID. | Fix the bug; the counts in `!usage` show how often. |
| "N games are waiting… no analysis worker has ever asked" / "The analysis worker last asked for work X ago" | The EliteDesk worker is not polling (15 min+, with games waiting). | `!analysisq`; the worker log; is the EliteDesk on and the task running (`schtasks /Query /TN PlayMoreBlitzWorker`)? Can it reach the Minix over ssh? | Start the task; fix the network/key; a reboot of the EliteDesk starts it by itself. |
| "The newest backup is from … days ago" / "backup folder … is missing" / "no backups" | The nightly backup isn't running. | Is the USB drive mounted (`df`)? `systemctl status playmoreblitz-backup.service`; `journalctl -u playmoreblitz-backup`. | Remount, rerun `venv/bin/python backup.py`. |
| "The last N refresh cycles all failed" | Every player's refresh failed N cycles running (the sites can't be reached). | `journalctl` for `refresh failed` lines; can the Minix reach Chess.com and Lichess? | Network/DNS; wait if a site is down. |
| "a refresh cycle crashed" / "sending the reviews… crashed" | A background loop hit a bug. | The traceback in the log. | Fix; the loop carries on at its next turn. |

**If the bot itself is down** it can't tell you. Set `HEARTBEAT_URL` (a free Healthchecks.io check: period 1 minute, grace
10 minutes; a restart takes seconds and even a Minix reboot is shorter than the grace) and the monitor emails you when the
pings stop. The service also restarts itself after a crash (`Restart=always`, 10 s).

## 12. Runbook

Always start with the log: `journalctl -u playmoreblitz -n 100 --no-pager`. Every command that reaches the bot logs
`command !x from <id> in channel <id>`; a refused one logs `ignored: …`. **Nothing logged at all means the message never
reached the bot.**

**Reading the log: what a request that worked looks like.** Every line names the Discord ID of whoever asked, and none carries
what they typed or what the bot sent (only IDs, outcomes, counts and the public game id):

```
command !obit from 1433… in channel 1551…                  the request arrived (a channel id that isn't the server's is a DM)
DM sent to 1433… (1 message)                               the DM was delivered
review of lichess abcd1234 sent to 1433…                   which review
obit request from 1433…: sent                              the request is answered   (other outcomes: waiting, no_dm, error)

command !export from 1433… in channel 1551…
export request from 1433… (games): 2 file(s) ready         (refused = it said why to the person, not in the log)
export sent to 1433…: 2 messages, 2 files
```

A failure says who too: `!obit failed for 1433… in channel …` with the traceback, the alert DM begins
`!obit from 1433… hit an unexpected error`, `couldn't send the review of … to 1433…: they don't accept DMs from the bot`
is a warning, and `the request of 1433… for lichess … was closed without a review (cant)` says a game couldn't be analysed.
Find one person's whole story with `journalctl -u playmoreblitz --since "-1day" | grep 1433`.

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| A command does nothing, and the log shows nothing | The bot can't see or read the channel: a permission change (View Channels off for `@everyone` while the bot's own role lacks it), or Message Content Intent off. | Give the bot's role **View Channels**, or allow it in that channel. Check the intent in the Developer Portal. |
| Log shows `ignored: !x in channel …` | Wrong channel, or a system command typed in a channel, or not an admin. | Use the allowed channel; admin commands go in a DM. |
| Every `!` command is ignored in the channel | The `@bot.check` decorator isn't on `_in_allowed_channel`. | `tests/test_obit.py` covers this; fix the decorator. |
| `/obit` or `/export` not offered | Slash commands weren't registered. | Log line `slash commands registered in <id>: obit, export` at start-up; if it says "Missing Access", re-invite with the `applications.commands` scope; reload Discord. |
| "I couldn't send you a DM" | The person's privacy setting blocks DMs from server members. | Turn on "Allow direct messages from server members". |
| `!obit` says it can't find the game | It isn't in the table (played before registering/analysis, or the refresh hasn't seen it yet). | Wait for the next refresh (30 min) or run `!obit` with no game (it looks at the sites first); `!queuemonth` backfills this month. |
| Games sit in `pending` | No worker asking. | `!analysisq`; see the worker alert row. |
| Games sit in `claimed` | The worker died mid-batch. | Nothing: after 30 minutes they return to `pending` (5 tries, then `failed`). |
| Many `failed` | The worker can't analyse them (site down, engine error). | `last_error` in the row; the worker log. To retry: set `status='pending', attempts=0` for those rows, or a member `!obit`s one. |
| `!results` shows the wrong owner/name | Registration issue. | `!remove` / `!add`; `!setowner` for ownership. |
| A month didn't close | A player's fetch failed at month end. | The failure is posted by name; fix the account or `!remove` it; run `!closemonth`. |
| "database is locked" | Another write held the lock longer than `DB_LOCK_TIMEOUT` (5 s). | Usually transient; check nothing else holds the file. |
| Numbers differ from Lichess's own analysis | The bot uses fewer nodes; Stockfish isn't deterministic with threads. | Expected: accuracy within a couple of points, counts indicative. |
| The service keeps restarting and the log ends in `Cannot connect to host discord.com … Temporary failure in name resolution` | The Minix can't resolve names. Seen when Tailscale's DNS was switched on with no upstream nameservers, which rewrote `/etc/resolv.conf` to point only at Tailscale (`tailscale dns status` shows "no resolvers configured"; `journalctl -u tailscaled` shows "no upstream resolvers set, returning SERVFAIL"). Pinging an address such as `1.1.1.1` still works. | Add a global nameserver in the Tailscale admin console's DNS page, or on the Minix `sudo tailscale set --accept-dns=false` (it then uses the router's DNS again). The bot recovers by itself: the service retries every 10 s. The outside monitor emails if it is down for more than its grace time. |
| Restarting the bot | | `sudo systemctl restart playmoreblitz`, then read the log for `connected as …` and `slash commands registered`. |

## 13. Developing and testing

- Python 3.13; `venv/bin/pip install -r requirements-dev.txt`; run everything with `python -m pytest -q` (no network, no
  Discord; about a minute). A few known-answer tests read real game data from `tests/fixtures_private/` (git-ignored) and
  skip themselves when it is absent.
- **Mutation checking** is how these tests were judged: change one thing in the code (flip a comparison, delete a line),
  run the tests, and a test that still passes has a gap. Survivors have repeatedly turned out to be missing tests, dead
  code, or a real bug (a decorator once ended up on the wrong function). Keep doing it for new code.
- Write to the code's existing style; put every tunable in `settings.py` (the tests check the README and `.env.example`
  list each one).
- **Editing gotchas.** Some files have Windows line endings: a script that edits them must read with `newline=""`.
  Backslash sequences in shell heredocs can be unescaped before they reach Python: write edit scripts to a file with the
  editor tool instead.
- **House rules (Bob's).** Committed files use invented example names only: no real chess usernames, no tokens, no email.
  Real data goes in git-ignored private fixtures. The Discord token lives only in `.env` files and is never printed.
  Commit and push only when asked. Personal features go by DM, never the channel. Don't open files or browsers on the
  user's machine unprompted.

## 14. Decisions and limits

- History begins at registration; no backfill except the current month. Data is kept indefinitely (a "forget me" or drop
  command is not built; opponents' usernames are kept as part of the public game record).
- Analysis covers games played since it was switched on (plus `!queuemonth` for the current month).
- **Time-management reference curves for other time controls** (3+1, 3+2, 5+3, 5+5, ...): the reference in check 5 was measured on 3+0 and 5+0 games only, so other controls get no pace line. Deriving more is a planned development (same method: average the fraction of the base time left at each move over many public games of one time control).
- Not built yet: takeaway tick-list and weekly `!obit` review, awards (weekly/monthly best game, most gained, and so on),
  a game-shape label, direct Google Sheets writing, a "forget me" command, deactivating accounts of people who leave the
  server, an alert that says the bot is back.
- The bot is running against a private test server; going live in the real server is a `.env` change
  (`ALLOWED_CHANNEL_IDS`, `POST_CHANNEL_ID`, `ADMIN_USER_IDS`, `ALERT_USER_IDS`) and re-inviting the bot there.

## 15. File map

| File | Role |
| --- | --- |
| `bot.py` | All Discord: commands, checks, loops, DMs, slash commands, alerts |
| `settings.py` | Every tunable, with `.env` overrides (`NAMES` lists them) |
| `store.py` | The database: schema, migrations, the player and month queries |
| `sources.py`, `gamecache.py`, `refresh.py`, `monthend.py`, `announce.py` | Site lookups, in-memory games, running totals, month close, scheduled posts |
| `stats.py`, `openings.py`, `render.py`, `monthargs.py` | Numbers from games, opening families, table text, reading "august"/"last" |
| `analysis_feed.py`, `game_records.py`, `analysis_queue.py` | Putting games in the queue, and the queue itself (claim/submit/release/prioritise/status) |
| `analysis.py`, `divider.py` | The method (accuracy, judgement, phases, moments, packing) |
| `worker.py`, `worker_gateway.py`, `game_data.py` | The EliteDesk worker, the Minix gateway it calls, reading a game from a site |
| `analysis_reports.py`, `render_analysis.py` | Reading the analysis for display |
| `obit.py`, `render_obit.py`, `clock_review.py` | Reviews: reading a game reference, own games, requests; the review text; the clock checks (how the time was spent) |
| `export_data.py` | CSV exports |
| `monitoring.py`, `usage.py` | Alerts logic and counts of use |
| `singleton.py` | The lock (`playmoreblitz.lock` beside the code) that stops a second copy of the bot starting from the same folder |
| `backup.py`, `playmoreblitz*.service/.timer` | Nightly backup and the systemd units |
| `tests/` | The tests (`test_*.py`; `analysis_helpers.py`, `helpers.py` build data) |
| `README.md` | Commands and settings reference; setup |
| `HOW_IT_WORKS.md` | This file |

## Local details (kept out of this public repository)

The real host names, user names, Tailscale address, file paths on each machine, Discord server/channel/admin IDs and the
step-by-step commands used to deploy are in the owner's private notes (`playmoreblitz-design.md` in the Downloads
folder, "Local cheat sheet") and in the assistant's memory for this project. The Discord token is only ever in the
`.env` files and must never be pasted anywhere.
