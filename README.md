# PlayMoreBlitz

A Discord bot that nudges a chess server to play more blitz. Members register their Chess.com or Lichess
account, and the bot keeps a table of how many rated blitz games each has played this month, their results and
their rating change. It also runs a monthly **100GOB** challenge (100 Games Of Blitz), posts the final table at
the end of each month, and says well done to everyone who reached 100.

The bot keeps no score of its own: game counts and ratings always come from the two sites. It stores only the
list of registered players and each month's totals.

## Commands

| Command | Who | What it does |
| --- | --- | --- |
| `!add <username> <site> [@member]` | Anyone for their own account; admins can name another member | Registers an account. `<site>` is `chess.com` or `lichess`. The name is checked against the site and stored in the site's own spelling. One account per site each (admins are exempt). |
| `!remove <username> [site]` | Whoever added it, or an admin | Takes a player off the list. Their history is kept. |
| `!results` | Anyone | This month so far for everyone registered: games, record, rating gain, 100GOB progress. |
| `!100gob [username] [site]` | Whoever added the account, or an admin | Joins this month's 100GOB challenge. |
| `!100gobnext [username] [site]` | Whoever added the account, or an admin | Signs up for next month's challenge. |
| `!mystats [username] [site]` (also `!stats`) | Anyone | One player's results, opening tables and best and worst opening. No name means your own account. |
| `!mystatsfull [username] [site]` (also `!statsfull`) | Anyone | Their records and splits by opponent rating, colour, weekday and time of day. |
| `!closemonth` | Admins only | Closes any finished month that is still open and posts its final table. Silent for everyone else. |
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

### 2. Configure

Copy `.env.example` to `.env` and fill it in. It is ignored by git and must stay private:

- `DISCORD_TOKEN`: the bot's token.
- `CONTACT`: an address or URL that Chess.com and Lichess can use to reach you if the bot misbehaves. It is sent
  in the User-Agent of site requests. Without it the bot still works but the sites can't contact you.
- `PLAYMOREBLITZ_DB` (optional): where the database file lives, if not beside the code. Use it to keep the data on a
  larger disk, in a folder of its own that already exists there. If that folder is missing (say the disk hasn't
  mounted yet) the bot refuses to start, and a service manager will keep retrying, so it can never quietly begin a
  new empty database in the wrong place. On a systemd machine add `RequiresMountsFor=/path/to/that/disk` to the
  service so it waits for the disk.

The other settings are constants at the top of `bot.py`:

| Constant | Meaning |
| --- | --- |
| `ALLOWED_CHANNEL_IDS` | The only channels where commands work. Also keeps the bot silent on other servers and in DMs. |
| `POST_CHANNEL_ID` | Where the bot's own posts go (final tables, the sign-up call). Should be one of the allowed channels. |
| `ADMIN_USER_IDS` | Who can remove anyone's entry, add for others, and run `!closemonth`. |
| `GOB_TARGET` | Games needed for the challenge (100). |
| `REFRESH_INTERVAL_MINUTES` | How often totals refresh (30). |
| `COOLDOWN_SECONDS` | Per-user cooldown on the commands that call the sites (600). Admins are exempt. |

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

Everything the bot keeps is in `playmoreblitz.db`. To back it up, stop the bot and copy the file, or use
`sqlite3 playmoreblitz.db ".backup backup.db"` while it runs. The file is created on first use and upgraded in
place when a new version adds to it.

### If commands seem to do nothing

The log says what the bot did with every command. Look for:

- `command !x from ... in channel ...`: the bot got it and ran it.
- `ignored: no command called !x`: the bot got it but has no such command.
- `ignored: !x in channel ... (not an allowed channel ...)`: wrong channel, or an admin-only command.
- Nothing at all: the message never reached the bot. Check that Message Content Intent is on and saved, and that
  the bot can see the channel.

## What it stores

The list of registered players (site, username, the Discord user ID of whoever added them), and for each month
the player's totals: games, wins, draws, losses, start and end rating, and whether they joined the challenge. Game
details for `!mystats` are held in memory only and are gone when the bot restarts. Everything comes from public
data on Chess.com and Lichess. `!remove` takes a player off the list; asking an admin to delete their rows from
the database removes the history too.

## Development

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest
```

The tests need no network and no Discord. A few known-answer tests read real game data from
`tests/fixtures_private/` (git-ignored, because real games name real people) and skip themselves when it is absent.

| File | Purpose |
| --- | --- |
| `bot.py` | Commands, the scheduled tasks, settings |
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
