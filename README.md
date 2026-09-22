# PlayMoreBlitz

A Discord bot that nudges a chess server to play more blitz. Members register their Chess.com or Lichess
account, and the bot keeps a table of how many rated blitz games each has played this month, their results and
their rating change. It also runs a monthly **100GOB** challenge (100 Games Of Blitz), posts the final table at
the end of each month, and says well done to everyone who reached 100.

The bot keeps no score of its own: game counts and ratings always come from the two sites. It stores only the
list of registered players and each month's totals.

**How it all fits together, how the game analysis works, what the alerts mean and what to do when something breaks:
see [HOW_IT_WORKS.md](HOW_IT_WORKS.md).**

## Commands

| Command | Who | What it does |
| --- | --- | --- |
| `!add <username> <site> [@member]` | Anyone for their own account; admins can name another member | Registers an account. `<site>` is `chess.com` or `lichess`. The name is checked against the site and stored in the site's own spelling. One account per site each (admins are exempt). Counts the month's games so far from registration; earlier months are not filled in. |
| `!remove <username> [site]` | Whoever added it, or an admin | Takes a player off the list. Their history is kept. |
| `!results [month]` | Anyone | This month so far for everyone registered: games, record, current rating, rating gain, 100GOB progress. Give a past month (`!results august`, `!results 2026-08`, `!results last`) for its final table. |
| `!100gob [username] [site]` | Whoever added the account, or an admin | Joins this month's 100GOB challenge. |
| `!100gobnext [username] [site]` | Whoever added the account, or an admin | Signs up for next month's challenge. |
| `!mystats [username] [site] [month]` (also `!stats`) | Anyone | One player's results, opening tables and best and worst opening, and, when their games have been analysed, an analysis block (accuracy by phase, average centipawn loss, mistakes per game). No name means your own account. |
| `!mystatsfull [username] [site] [month]` (also `!statsfull`) | Anyone | Their records and splits by opponent rating, colour, weekday and time of day. |
| `!lastgame [username] [site]` | Anyone | The bot's analysis of a player's most recent analysed game, for both sides: result, rating change, inaccuracies, mistakes, blunders, average centipawn loss, accuracy overall and by phase, with a link to the game. Reads only what the bot already holds. Needs game analysis switched on. |
| `!obit [game link or id]` in a direct message to the bot (also `/obit [game]` in the channel) | Registered members who are on the server, for their own games | A private review of one of your own games by direct message, in Nate Solon's OBIT order: Openings (name, accuracy by phase, the engine's score after 10 moves), Blunders (your worst moments with links to the position before each, on Lichess), Interesting (a lost-on-time flag, a win thrown away or saved, chances your opponent gave you) and a prompt for your Takeaway. No link means your latest game. A Lichess or Chess.com link works, or a bare id. If the game isn't analysed yet it goes to the front of the queue and the review follows by DM. Only games played by an account you registered; the channel only sees a tick. Every DM carries a Delete button, since Discord doesn't let you delete a bot's message in a DM yourself. With no game named it looks on the sites first, so a game played a minute ago counts as the latest (at most once every two minutes per person). `/obit` is a slash command whose only reply is one that just the person asking can see, so nothing appears in the channel. `!obit` works only in a direct message to the bot (a reaction and, if needed, a short reply, all private); in the channel it answers with a hint that removes itself after 20 seconds. In a direct message the bot checks that the person is a member of the server it serves, and refuses if it can't tell. Needs game analysis switched on. |
| `!analysisq` (also `!analysisqueue`) | Admins only, in a direct message to the bot | How the analysis queue stands (waiting, done, skipped, failed), how long the oldest game has waited, and when the worker last asked for work. Silent for everyone else. |
| `!queuemonth` | Admins only, in a direct message to the bot | Puts this month's games so far, for every registered player, in the analysis queue (the refresher only sees games from when analysis was switched on). Can take a few minutes; safe to run again. |
| `!setowner <username> <@member> [site]` | Admins only, in a direct message to the bot | Hands a registered account to the member it belongs to. Accounts an admin registers without naming a member are the admin's own, which makes `!obit` and `!mystats` with no name treat them all as the admin's; this fixes that. Non-admin owners keep to one account per site, and the member must be on the server. |
| `!gamestate <game link or id>` | Admins only, in a direct message to the bot | The raw `game_analysis` row for one game, whoever it belongs to: status, priority, attempts, errors, when it was queued/claimed/analysed, the engine's figures for both sides once analysed, and whether it holds evals/clocks/moments - for checking on a specific game instead of reading the database by hand. A Lichess or Chess.com link, or a bare id. |
| `!export [summary] [period]` in a direct message to the bot (also `/export [period] [what]` in the channel) | Registered members who are on the server, for their own accounts | Your own games as CSV files for a spreadsheet, sent to you by DM: one file per account (a username on a site), one line per game the bot holds, oldest first, analysed or not (the analysis columns are blank until a game is analysed). Columns: when, site, account, link, colour, result, how it ended, time control, opening, ECO, rating before and change, opponent and rating, analysis status, your accuracy overall and by phase, your inaccuracies, mistakes, blunders and centipawn loss, the opponent's, the site's own accuracy, the engine's score after 10 moves, and your worst moments with their move numbers. The period is this month (the default), `last`, `week` (the last seven days), a month like `2026-08`, or `all`. `summary` gives one line per account for the period (games, record, rating start to end, average accuracy, mistakes per game, losses on time) to paste into your own sheet. Games from before analysis was switched on aren't held, and the reply says so when the month's count is higher. Google Sheets can import the file. |
| `!backfill <month>` in a direct message to the bot (also `/backfill <month>` in the channel) | Registered members who are on the server, for their own accounts | Fetches one of your own past months from its site and queues it for analysis, so `!obit` and `!export` can see it - for games from before you registered, or a month analysis missed. A month like `2025-11` or a name like `november`; not the current month, which the bot already keeps up with on its own. One at a time, at least a minute apart, since each one calls out to the chess site. Subject to the same monthly analysis limit as any other games. Personal only: it never touches `!results`, `!history` or 100GOB, which stay exactly as they are. |
| `!history [username] [site]` in a direct message to the bot | Registered members who are on the server | One player's months, newest first, one line each: games, record, rating start to end, net, average accuracy, 100GOB. No name means your own account; a name means any registered player, not just your own. Reads only what the bot holds, no calls to the chess sites. In the channel it answers with a hint that removes itself after 20 seconds. No slash version yet. |
| `!myhistory [site]` in a direct message to the bot | Registered members who are on the server, for their own accounts | The analysis side of your own months, one line each: games, how many are analysed, record, rating start to end, net, average accuracy. Reads `game_analysis`, not the `monthly_results` table `!history` reads, so a month `!backfill` pulled in shows up here even though `!history` can't see it - the two can disagree, since they're filled separately. In the channel it answers with a hint that removes itself after 20 seconds. No slash version yet. |
| `!usage [days]` | Admins only, in a direct message to the bot | How the bot has been used over the last 1 to 30 days (7 by default): per day the commands run, the people who used it and the errors, then the most used commands and the reviews and exports sent. Counts only. |
| `!clear` | Anyone on the server, in a direct message to the bot | Deletes the messages the bot has sent you in that conversation, old ones included (Discord doesn't let you delete a bot's messages in a DM yourself, but the bot can delete its own). Only the bot's messages go, the most recent 1000 in one go: run it again if some are left. Everything the bot says in a DM also carries a 🗑️ Delete button. |
| `!closemonth` | Admins only, in a direct message to the bot | Closes any finished month that is still open and posts its final table. Silent for everyone else. |
| `!helpblitzbot` | Anyone | The command list. |

**House rule: register your own account, one per site.** The bot can't check that an account belongs to whoever
registers it, so it works on trust and helps keep the rule: a member can have one active account on Chess.com and one
on Lichess (remove one with `!remove` to swap it). Admins aren't limited, can register an account for someone else
by naming them (`!add <username> <site> @member`, a mention or their Discord user ID, no other ID needed), and can
`!remove` anyone's entry, which is how any problem gets settled.

Success is shown with a ✅ reaction and no text; a refusal gets a ❌ and a short reason. Commands are not case
sensitive, and only work in the channels listed in `ALLOWED_CHANNEL_IDS`. Where a command takes `[username]`
and you have more than one account, the bot lists them and asks which.

## How it works

- **History begins at registration.** A player's first month is the month they register, counted from the 1st, and earlier
  months are never filled in. Every month after that is kept, unchanged once closed, so `!results`, `!history` and
  `!mystats` can look back at any month held. Asking for a month with no data says when the bot's records begin.
- **Only rated, standard-chess blitz games count.** Bughouse and other variants are excluded.
- **Months are UTC calendar months**, matching how Chess.com cuts its archives. In summer that means a month
  rolls over at 01:00 UK time.
- **Incremental fetching.** A background refresher visits each player every 30 minutes, one at a time, and asks
  the site only for games since the last one counted, so `!results` answers instantly from stored totals and
  never waits on a site. The table says how old its numbers are, and marks any player whose refresh failed.
- **Month end.** Within half an hour of midnight UTC on the 1st the bot closes the month: it refetches every
  player's whole month and replaces the running totals with the recount. This is **all or nothing**: if any
  player's games can't be fetched, nothing is written and no table is posted. The bot posts a notice naming each
  player that failed and why instead, and retries every 30 minutes. At 9am UK time it posts the final table.
  Each player's next month opens from their closing rating.
- **Sign-up call.** Seven days before each month starts, at 9am UK time, the bot invites sign-ups for the
  next month's challenge.
- **Site limits are respected.** Chess.com and Lichess are called strictly one request at a time, with a
  timeout, and Lichess's export is spaced out. Anything unexpected (an unknown result code, a game that isn't
  standard rated blitz) is an error, never a guess.

## Setting it up

You need Python 3.13 or newer.

### 1. A Discord application

1. In the [Developer Portal](https://discord.com/developers/applications), create an application and add a bot.
2. On the **Bot** page turn on **Message Content Intent** and press **Save Changes**. Consider turning **Public
   Bot** off.
3. Invite it with the `bot` scope and permissions integer **68672** (View Channels, Send Messages, Read Message
   History, Add Reactions). Add nothing else.
4. **Slash commands** (`/obit`) need the `applications.commands` scope as well: on the OAuth2 URL Generator tick both `bot`
   and `applications.commands`, keep the same permissions, and open the new link. A bot already in the server just
   authorises again; it doesn't need removing first. Without it the log says "couldn't register slash commands" and the
   `!` commands carry on as normal.

### 2. Configure

Copy `.env.example` to `.env` and fill it in. It is ignored by git and must stay private. The two private values:

- `DISCORD_TOKEN`: the bot's token.
- `CONTACT`: an address or URL that Chess.com and Lichess can use to reach you if the bot misbehaves. It is sent
  in the User-Agent of site requests. Without it the bot still works but the sites can't contact you.
- `PLAYMOREBLITZ_DB` (optional): where the database file lives, if not beside the code. Use it to keep the data on a
  larger disk, in a folder of its own that already exists there. If that folder is missing (say the disk hasn't
  mounted yet) the bot refuses to start, and a service manager will keep retrying, so it can never quietly begin a
  new empty database in the wrong place. On a systemd machine add `RequiresMountsFor=/path/to/that/disk` to the
  service so it waits for the disk.

Every other setting has a default in `settings.py` and can be changed by adding a line with the same name to
`.env` and restarting the bot, for instance `COOLDOWN_SECONDS=300` or `ALLOWED_CHANNEL_IDS=123456,789012`. A
setting that is present but not valid (say `GOB_TARGET=lots`) stops the bot at startup and names the setting.

| Setting | Meaning | Default |
| --- | --- | --- |
| `ALLOWED_CHANNEL_IDS` | The only channels where commands work, comma separated. Also keeps the bot silent on other servers and in DMs. | the test channel |
| `POST_CHANNEL_ID` | Where the bot's own posts go (final tables, the sign-up call). Should be one of the allowed channels. | the test channel |
| `ALERT_USER_IDS` | Who is sent a private message when something needs attention (an unexpected error, a silent analysis worker, a missing backup, failing refreshes), comma separated. | Bob |
| `HEARTBEAT_URL` | An `https://` address the bot pings once a minute to say it is alive; a monitoring service tells you if the pings stop. Keep it in `.env`. See "Watching over the bot". | none |
| `ADMIN_USER_IDS` | Who can remove anyone's entry, add for others, and run `!closemonth`, comma separated. | Bob and Matt |
| `GOB_TARGET` | Games needed for the challenge. | 100 |
| `REFRESH_INTERVAL_MINUTES` | How often totals refresh. | 30 |
| `COOLDOWN_SECONDS` | Per-user cooldown on the commands that call the sites. Admins are exempt. | 600 |
| `POST_HOUR_UK` | The hour (0-23, UK time) at which scheduled posts go out. | 9 |
| `CALL_DAYS_BEFORE` | Days before a month starts that the sign-up call goes out. | 7 |
| `MIN_OPENING_GAMES` | Games an opening needs to get its own row rather than "All others". | 2 |
| `MIN_BEST_WORST_GAMES` | Games an opening needs before it can be called best or worst. | 3 |
| `SIMILAR_RATING_BAND` | Rating points either side of yours that count as a similar opponent. | 50 |
| `STATS_CACHE_PLAYERS` | Players whose games are kept in memory for `!mystats`. | 64 |
| `REQUEST_TIMEOUT_SECONDS` | Longest an ordinary Chess.com or Lichess request may take. | 10 |
| `MONTH_TIMEOUT_SECONDS` | Longest a whole month's fetch may take. | 300 |
| `MONTH_STALL_SECONDS` | A fetch that goes quiet for this long is abandoned. | 30 |
| `LICHESS_EXPORT_MIN_INTERVAL` | Seconds between two Lichess game exports. | 2 |
| `DB_LOCK_TIMEOUT` | Seconds to wait for another database write to finish. | 5 |
| `BACKUP_DIR` | Folder for the nightly database backups. It must already exist, and should be on a different disk from the database. | `backups` beside the code |
| `BACKUP_KEEP_DAYS` | Days of nightly backups to keep. | 100 |
| `ANALYSIS_ENABLED` | Whether members' games are put in the analysis queue (yes or no). Leave it off until the analysis worker is set up. | no |
| `ANALYSIS_FULL_PRIORITY_GAMES` | A player's games in a month up to this number are analysed as usual. | 500 |
| `ANALYSIS_MAX_GAMES` | Games from there up to this number go to the back of the analysis queue; beyond it they are not analysed. | 1000 |
| `ANALYSIS_CLAIM_MINUTES` | How long the analysis worker has to finish a game before it goes back in the queue. | 30 |
| `ANALYSIS_MAX_ATTEMPTS` | Tries before a game that keeps failing is given up on. | 5 |

At startup the bot logs a warning for anything it can see is wrong, such as a missing `CONTACT` or a post channel
it can't reach.

### 3. Run it

```bash
python3.13 -m venv venv
venv/bin/pip install -r requirements.txt
set -a && . ./.env && set +a
venv/bin/python bot.py
```

On Windows, use `py -3.13 -m venv venv` and `venv\Scripts\pip`, and set the variables from `.env` in your shell
first. A second copy started from the same folder refuses to run, so two copies can never answer every command
twice.

### 4. Keep it running (Linux)

Edit the paths and user in `playmoreblitz.service`, copy it to `/etc/systemd/system/`, then:

```bash
sudo systemctl enable --now playmoreblitz
journalctl -u playmoreblitz -f       # the log
```

### Backups

Everything the bot keeps is in the database file (`playmoreblitz.db` unless `PLAYMOREBLITZ_DB` says otherwise). It is
created on first use and upgraded in place when a new version adds to it.

`backup.py` makes a dated copy, `playmoreblitz-YYYY-MM-DD.db`, in `BACKUP_DIR` using SQLite's own backup, so it is safe
while the bot runs. Copies older than `BACKUP_KEEP_DAYS` (100) days are then removed, but only after a good new copy
exists. The folder must already exist and should be on a different disk from the database; if it is missing (the disk
isn't mounted) the backup refuses to run rather than fill the wrong disk. To run it every night at 03:30 on a systemd
machine, edit the paths and user in `playmoreblitz-backup.service`, copy it and `playmoreblitz-backup.timer` to
`/etc/systemd/system/`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now playmoreblitz-backup.timer
systemctl list-timers playmoreblitz-backup.timer   # when it last ran and runs next
journalctl -u playmoreblitz-backup                 # what it did, or why it failed
```

To restore, stop the bot, copy the chosen backup over the database file, and start the bot again.

### Watching over the bot

The bot tells you when something needs attention, by a private message to the people in `ALERT_USER_IDS` (just Bob to
begin with), headed "⚠ PlayMoreBlitz":

- **An unexpected error** in a command (the command, the kind of error and the Discord ID of whoever ran it, so you can
  find them and see what happened in the log). The same error is reported at most every 30 minutes.
- **The analysis worker has gone quiet:** games are waiting and it hasn't asked for work for 15 minutes (a reboot of
  the EliteDesk is shorter than that), or it never has.
- **The backups have stopped:** the newest is two or more days old, or the backup folder is missing or empty.
- **The refresh has failed throughout** for three cycles in a row (the chess sites can't be reached).
- **A background job crashed** (the refresh or the sending of reviews).

A problem that lasts is reported again every six hours; when it clears it is forgotten, so if it returns you hear at
once. `!usage` (in a DM to the bot) shows how the bot is being used: per day the commands run, the people who used it
and the errors, and the most used commands.

**If the bot itself is down** it can't tell you, so give it something outside to report to. Set `HEARTBEAT_URL` in
`.env` and the bot pings it once a minute; a monitoring service tells you when the pings stop. With
[Healthchecks.io](https://healthchecks.io) (free): make a check with a period of 1 minute and a grace time of 10
minutes (so a restart, which takes seconds, and even a reboot of the Minix never trigger it), add your email or
another way of being told, copy the check's ping address into `.env` as `HEARTBEAT_URL=https://hc-ping.com/...`, and
restart the bot. Keep that address private: anyone who has it can send pings.

### Game analysis (optional)

The bot can have a chess engine analyse the games its players play and keep the figures: accuracy overall and by
phase, inaccuracies, mistakes, blunders and average centipawn loss, for both sides. The engine runs on another
machine, the **worker**, which asks the bot's machine for games over SSH. The worker fetches each game from its
site, analyses it in memory and sends back only the figures; the moves are never stored or sent.

1. **Switch it on** in the bot's `.env`: `ANALYSIS_ENABLED=yes`, and restart the bot. From then on the refresher
   queues each registered player's games (see the settings table for the monthly limits).
2. **Give the worker its own SSH key.** On the worker machine: `ssh-keygen -t ed25519 -f ~/.ssh/pmb_worker -N ""`.
   On the bot's machine add one line to `~/.ssh/authorized_keys`, which locks that key to the gateway program so it
   can do nothing else (`desk` is the worker's name):

   ```
   command="/path/to/venv/bin/python /path/to/worker_gateway.py desk",restrict ssh-ed25519 AAAA... pmb-worker
   ```
3. **Set up the worker.** It needs Python 3.13, `pip install -r requirements-worker.txt`, a Stockfish binary, and these
   files in one folder: `worker.py`, `worker_gateway.py`, `analysis.py`, `divider.py`, `game_data.py`. Copy
   `worker.env.example` to `worker.env` there and fill it in (it is git-ignored).
4. **Try it.** `python worker.py --check` tests the connection and the engine and stops. `python worker.py --once`
   analyses everything in the queue and stops. `python worker.py` keeps running and looks for new games every
   `IDLE_SLEEP_SECONDS`.

**Keeping the worker running on Windows.** Run it as a scheduled task that starts with the machine, so it needs no
one to be logged in. The task runs as the SYSTEM account, which has its own home, so give it its own copy of the key and
of `known_hosts` in a folder only SYSTEM and Administrators can read (ssh refuses a key that others can read), and
point `worker.env` at them, with a log file:

```
GATEWAY_KEY=C:/ProgramData/pmb-worker/pmb_worker
GATEWAY_KNOWN_HOSTS=C:/ProgramData/pmb-worker/known_hosts
LOG_FILE=C:/ProgramData/pmb-worker/worker.log
```

```
icacls C:\ProgramData\pmb-worker\pmb_worker /inheritance:r /grant SYSTEM:F /grant Administrators:F
icacls C:\ProgramData\pmb-worker\pmb_worker /setowner SYSTEM
```

Then, in an administrator PowerShell (adjust the two paths):

```powershell
$action = New-ScheduledTaskAction -Execute "C:\path\to\python.exe" -Argument "C:\path\to\worker.py"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName PlayMoreBlitzWorker -Action $action -Trigger $trigger -Settings $settings -User SYSTEM -RunLevel Limited
Start-ScheduledTask -TaskName PlayMoreBlitzWorker
```

The worker's log says whether it connected (`connected to the bot's machine`) and lists each game it analyses. It
starts Stockfish at below-normal priority so the machine stays usable.

The worker only ever asks the bot for games, so it works whenever the two machines can reach each other; if the
worker is off, games simply wait in the queue. On Windows the worker passes its input and output to `ssh` through
temporary files, because Windows' `ssh.exe` stops responding when another program gives it a pipe.

### If commands seem to do nothing

The log says what the bot did with every command. Look for:

- `command !x from ... in channel ...`: the bot got it and ran it.
- `ignored: no command called !x`: the bot got it but has no such command.
- `ignored: !x in channel ... (not an allowed channel ...)`: wrong channel, or an admin-only command.
- Nothing at all: the message never reached the bot. Check that Message Content Intent is on and saved, and that
  the bot can see the channel. If the server hides channels from everyone (View Channels switched off for
  @everyone, for instance to limit a tester to one channel), the bot's own role needs **View Channels** switched on,
  or a channel permission that allows it in the bot's channel: an invite that didn't ask for View Channels leaves
  the bot relying on @everyone for it.

## What it stores

The list of registered players (site, username, the Discord user ID of whoever added them), and for each month
the player's totals: games, wins, draws, losses, start and end rating, and whether they joined the challenge. Game
details for `!mystats` are held in memory only and are gone when the bot restarts. Everything comes from public
data on Chess.com and Lichess. `!remove` takes a player off the list; asking an admin to delete their rows from
the database removes the history too.

If game analysis is switched on (`ANALYSIS_ENABLED`) the bot also keeps a row for every game a registered player
plays: the site and its id for the game, when it ended, the time control, the result and how it ended, both players'
usernames (the opponent's is in the public game record too), the registered player's rating change, the ratings the site
reports, and the site's own opening name and ECO code. Once a game has been analysed the row also holds the engine's
figures for both sides (accuracy overall and by phase, inaccuracies, mistakes, blunders, average centipawn loss) and a
packed list of the evaluation after each move, and the ply numbers of the moves called inaccuracies, mistakes and
blunders with how much each cost (numbers only, so the game can be opened at that position from its link). The moves
themselves are never stored.

A `!obit` request is kept only until it is answered: the Discord user ID of whoever asked, the game, their account
and the channel they asked in, so the review can be sent when the analysis finishes. The review itself is sent by
direct message and not stored, and a request the bot cannot answer within a day is dropped.

The bot also keeps counts of its own use, for `!usage`: per day, how many times each command ran, and which Discord user
IDs were seen that day (so that people can be counted). No message text and no game data, and anything older than 35 days is
dropped. Alerts are messages, not records: nothing about them is kept.

## Development

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest
```

The tests need no network and no Discord. A few known-answer tests read real game data from
`tests/fixtures_private/` (git-ignored, because real games name real people) and skip themselves when it is absent. The
analysis tests need `chess` (python-chess) for some checks and skip those without it; `pip install -r requirements-dev.txt`
brings it in.

| File | Purpose |
| --- | --- |
| `bot.py` | Commands and the scheduled tasks |
| `backup.py` | The nightly database backup |
| `analysis.py` | The game-analysis maths: accuracy, inaccuracies, mistakes, blunders, average loss (Lichess's published method) |
| `divider.py` | Where a game's opening, middlegame and endgame start (a translation of the scalachess divider, MIT) |
| `analysis_queue.py` | The queue of games waiting to be analysed and the results that come back (claiming, limits, giving games back) |
| `analysis_feed.py`, `game_records.py` | Putting the games the bot already fetches into the analysis queue (off unless `ANALYSIS_ENABLED`) |
| `worker_gateway.py` | The one program the analysis worker's SSH key may run: `hello`, `claim N`, `submit`, `release`, JSON in and out |
| `worker.py`, `game_data.py` | The analysis worker (runs on the machine with the engine) and its reading of the two sites' games |
| `settings.py` | Every setting, its default, and how `.env` overrides it |
| `sources.py` | Chess.com and Lichess lookups |
| `stats.py`, `openings.py` | The numbers and opening grouping, as pure functions |
| `render.py` | Tables and messages |
| `store.py` | SQLite storage |
| `refresh.py` | Incremental refreshing of running totals |
| `monthend.py` | Closing a finished month |
| `announce.py` | When the scheduled posts are due, and what they say |
| `gamecache.py` | A player's games held in memory for `!mystats` |
| `singleton.py` | Refuses a second copy of the bot |

## Licence

GPL-3.0, see `LICENSE`. The planned game analysis is a reimplementation of the accuracy, inaccuracy/mistake/blunder
and game-phase methods that [Lichess](https://lichess.org) publishes, and the analysis engine is Stockfish, itself
GPL-3.0. Thanks to the Lichess and Stockfish teams.
