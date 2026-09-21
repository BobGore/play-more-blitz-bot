"""PlayMoreBlitz Discord bot.

Nudges members to play more blitz: monthly rated-blitz game counts per player.
Commands so far: !add, !remove, !results and a help command. The rest come in later steps.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks

import analysis
import analysis_feed
import analysis_queue
import analysis_reports
import announce
import gamecache
import monthargs
import monthend
import refresh
import render
import render_analysis
import settings
import singleton
import sources
import stats
import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("playmoreblitz")


class _NoVoiceWarnings(logging.Filter):
    """discord.py warns twice at startup that voice isn't supported. This bot never uses voice."""

    def filter(self, record):
        return "voice will NOT be supported" not in record.getMessage()


logging.getLogger("discord.client").addFilter(_NoVoiceWarnings())

# The settings live in settings.py (each can be overridden in .env); these names are kept
# here because the rest of this file and the tests read them from this module.
ALLOWED_CHANNEL_IDS = settings.ALLOWED_CHANNEL_IDS  # the only channels commands work in
ADMIN_USER_IDS = settings.ADMIN_USER_IDS  # can remove anyone's entry, add for others, run !closemonth
COOLDOWN_SECONDS = settings.COOLDOWN_SECONDS  # per user, on commands that call the chess sites
REFRESH_INTERVAL_MINUTES = settings.REFRESH_INTERVAL_MINUTES  # how often totals are refreshed
GOB_TARGET = settings.GOB_TARGET  # the 100GOB challenge: games in the month that earn the tick
POST_CHANNEL_ID = settings.POST_CHANNEL_ID  # where the bot's own scheduled posts go

USAGE = {
    "add": "!add <username> <site>   (site is chess.com or lichess)",
    "remove": "!remove <username> [site]",
    "100gob": "!100gob [username] [site]",
    "100gobnext": "!100gobnext [username] [site]",
    "results": "!results [month]",
    "mystats": "!mystats [username] [site] [month]",
    "mystatsfull": "!mystatsfull [username] [site] [month]",
    "history": "!history [username] [site]",
    "lastgame": "!lastgame [username] [site]",
}

intents = discord.Intents.default()
intents.message_content = True
# Command input gets echoed back in replies unfiltered - this stops any of it
# from ever triggering a real @everyone/@here/role/user ping.
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    allowed_mentions=discord.AllowedMentions.none(),
    # "! add ..." (whitespace after the prefix) would otherwise parse to an
    # empty command name and be silently dropped.
    strip_after_prefix=True,
    # !100GOB, !100gob and !100Gob all work.
    case_insensitive=True,
)


@tasks.loop(minutes=REFRESH_INTERVAL_MINUTES)
async def refresh_loop():
    """Every REFRESH_INTERVAL_MINUTES, and once as soon as it starts (so a restart is never stale).

    First it closes any finished month that is still open (retrying each time until it
    works), so a new month's rows exist before they are refreshed; then it refreshes
    everyone; then it posts anything about a month's ending that has become due.
    """
    try:
        for result in await monthend.close_due_months():
            log.info("month close for %s: %s", result.month, "closed" if result.ok else f"failed for {len(result.failures)} player(s)")
        outcomes = await refresh.refresh_all(sources.current_month())
        log.info("refresh cycle finished: %s", outcomes or "nobody registered")
        await post_month_end_if_due()
    except Exception:  # one bad cycle must not stop the loop
        log.exception("refresh cycle crashed")


async def post_signup_call_if_due():
    """Post the 100GOB sign-up call if it is due and hasn't been posted for that month. Returns whether it posted."""
    now = datetime.now(timezone.utc)
    month = announce.signup_call_due(now)
    if month is None:
        return False
    if not await asyncio.to_thread(store.claim_announcement, "signup_call", month, now.isoformat()):
        return False  # already posted, perhaps before a restart
    try:
        await (await _post_channel()).send(announce.signup_call_text(month, GOB_TARGET))
    except Exception:
        log.exception("couldn't post the 100GOB sign-up call for %s", month)
        await asyncio.to_thread(store.release_announcement, "signup_call", month)  # so the next try can post it
        return False
    log.info("posted the 100GOB sign-up call for %s", month)
    return True


async def _post_channel():
    return bot.get_channel(POST_CHANNEL_ID) or await bot.fetch_channel(POST_CHANNEL_ID)


async def send_final_table(channel, month, now):
    """Post a closed month's final table, then well done to everyone who reached the target."""
    rows = await asyncio.to_thread(store.results, month, True)  # only players who were in that month
    for message in render.render_results(rows, month, now, GOB_TARGET, final=True):
        await channel.send(message)
    finishers = sorted((r.username for r in rows if r.in_100gob and r.games >= GOB_TARGET), key=str.lower)
    congratulation = announce.well_done_text(finishers, GOB_TARGET)
    if congratulation:
        for piece in render.fit(congratulation):
            await channel.send(piece)


async def post_month_end_if_due():
    """Post the final table of any month that has been closed and is due to be posted, and a
    notice about any finished month that still can't be closed. Each is posted once (a
    notice once a day). Returns whether anything was posted."""
    now = datetime.now(timezone.utc)
    posted = False

    cutoff = (now - timedelta(days=3)).isoformat()  # only recent closes: never re-post old history
    for month in await asyncio.to_thread(store.closed_months_since, cutoff):
        if not announce.month_end_post_due(month, now):
            continue
        if not await asyncio.to_thread(store.claim_announcement, "month_end", month, now.isoformat()):
            continue  # already posted, perhaps before a restart
        try:
            await send_final_table(await _post_channel(), month, now)
            log.info("posted the final table for %s", month)
            posted = True
        except Exception:
            log.exception("couldn't post the final table for %s", month)
            await asyncio.to_thread(store.release_announcement, "month_end", month)

    for month in await asyncio.to_thread(store.unclosed_months, sources.current_month(now)):
        failures = monthend.last_failures.get(month)
        if not failures or not announce.month_end_post_due(month, now):
            continue
        if not await asyncio.to_thread(store.claim_announcement, "close_failed", f"{month}/{announce.uk_date(now)}", now.isoformat()):
            continue
        try:
            await (await _post_channel()).send(announce.close_failure_text(month, failures))
            log.info("posted the failure notice for %s", month)
            posted = True
        except Exception:
            log.exception("couldn't post the failure notice for %s", month)
            await asyncio.to_thread(store.release_announcement, "close_failed", f"{month}/{announce.uk_date(now)}")
    return posted


@tasks.loop(time=announce.POST_TIME)
async def daily_posts():
    """Every day at 9am UK time."""
    for post in (post_signup_call_if_due, post_month_end_if_due):
        try:
            await post()
        except Exception:
            log.exception("scheduled post %s crashed", post.__name__)


_background = set()  # keeps a reference to running tasks so they aren't garbage collected


def _start_refresh(site, username):
    """Refresh one player in the background, so a new player shows up without waiting for the next cycle."""
    task = asyncio.create_task(refresh.refresh_one(site, username))
    _background.add(task)
    task.add_done_callback(_background.discard)


def configuration_warnings():
    """Things about the setup that the person running the bot should hear about at startup."""
    problems = []
    if not sources.CONTACT:
        problems.append("CONTACT is not set: site requests carry no contact address (see .env.example)")
    if POST_CHANNEL_ID not in ALLOWED_CHANNEL_IDS:
        problems.append(f"POST_CHANNEL_ID {POST_CHANNEL_ID} is not in ALLOWED_CHANNEL_IDS, so nobody can run commands where the bot posts")
    if bot.get_channel(POST_CHANNEL_ID) is None:
        problems.append(
            f"can't see the post channel {POST_CHANNEL_ID}: is the bot in that server, with View Channel and Send Messages there? "
            "Scheduled posts will fail until it is"
        )
    return problems


@bot.event
async def on_ready():
    log.info("connected as %s", bot.user)
    log.info("database: %s", store.DB_PATH)
    for problem in configuration_warnings():
        log.warning(problem)
    if not refresh_loop.is_running():  # on_ready can fire again after a reconnect
        refresh_loop.start()
    if not daily_posts.is_running():
        daily_posts.start()
        try:
            await post_signup_call_if_due()  # catch up if the bot was down when a post was due
        except Exception:
            log.exception("catch-up posts crashed")


@bot.check
async def _in_allowed_channel(ctx):
    return ctx.channel.id in ALLOWED_CHANNEL_IDS


def _is_admin(user_id):
    return user_id in ADMIN_USER_IDS


def _cooldown_for(ctx):
    """No cooldown for admins; everyone else gets COOLDOWN_SECONDS per user."""
    if _is_admin(ctx.author.id):
        return None
    return commands.Cooldown(1, COOLDOWN_SECONDS)


async def _react(ctx, emoji, fallback=None):
    """Add a reaction to the command message. If the bot isn't allowed to (a missing
    permission in some channel), say `fallback` in words instead, so the outcome is still
    clear and a command that succeeded doesn't turn into an error afterwards."""
    try:
        await ctx.message.add_reaction(emoji)
    except discord.HTTPException as exc:
        log.warning("couldn't add a reaction in channel %s (%s) - does the bot have Add Reactions?", ctx.channel.id, exc.status)
        if fallback:
            await ctx.send(fallback)


async def _tick(ctx):
    await _react(ctx, "✅", "Done.")


async def _reject(ctx, reason, *, refund_cooldown=False):
    """A red cross and a short reason. A refused command doesn't burn the cooldown."""
    await _react(ctx, "❌")  # the reason below carries the message even if the cross can't be added
    try:
        await ctx.send(reason)
    except discord.HTTPException as exc:
        log.warning("couldn't send a reply in channel %s (%s): %s", ctx.channel.id, exc.status, reason[:80])
    # Admins have no cooldown bucket at all (see _cooldown_for), so there is
    # nothing to refund and reset_cooldown would fail on the missing bucket.
    if refund_cooldown and not _is_admin(ctx.author.id):
        ctx.command.reset_cooldown(ctx)


@bot.command(name="helpblitzbot")
async def help_blitz_bot(ctx):
    await ctx.send(
        "**PlayMoreBlitz bot** - counts each member's rated blitz games this month.\n"
        f"`{USAGE['add']}` - register your own account, one per site (admins can add others). "
        "It counts this month's games so far; earlier months aren't counted\n"
        "`!remove <username> [site]` - takes a player off the list (whoever added them, or an admin)\n"
        "`!results [month]` - this month so far for everyone on the list (refreshed every "
        f"{REFRESH_INTERVAL_MINUTES} minutes), or a past month's final table: `!results august`, `!results last`\n"
        f"`!100gob [username]` - join this month's challenge: {GOB_TARGET} games of blitz\n"
        "`!100gobnext [username]` - sign up for next month's challenge\n"
        "`!mystats [username] [month]` - one player's results and openings this month, or another month (yours if no name)\n"
        "`!mystatsfull [username] [month]` - their records and splits by opponent, colour, day and time\n"
        "`!history [username]` - a player's months one line each: games, record, rating, accuracy, 100GOB\n"
        "`!lastgame [username]` - the bot's analysis of a player's latest analysed game: both sides, with a link\n"
    )


def _one_per_site_message(site, existing=None):
    have = f" ({existing})" if existing else ""
    return f"you already have a {site} account on the list{have} - it's one per site. Use `!remove` first, or ask an admin."


@bot.command()
@commands.dynamic_cooldown(_cooldown_for, commands.BucketType.user)
async def add(ctx, username: str, site: str, owner: Optional[discord.User] = None):
    """Register a Chess.com or Lichess account. Admins can name someone else as the owner."""
    site = site.lower()
    if site not in sources.SITES:
        await _reject(ctx, "the site must be `chess.com` or `lichess`", refund_cooldown=True)
        return

    person = owner or ctx.author
    if person.id != ctx.author.id and not _is_admin(ctx.author.id):
        await _reject(ctx, "only an admin can add someone else", refund_cooldown=True)
        return

    existing = await asyncio.to_thread(store.get_player, site, username)
    if existing and existing.active:
        await _reject(ctx, f"{username} is already on the list for {site}", refund_cooldown=True)
        return

    # One account per site each, unless an admin is adding. Checked here so a refusal costs
    # no calls to the chess sites, and again inside the database write (see LIMIT below).
    limited = not _is_admin(ctx.author.id)
    if limited:
        mine = [p for p in await asyncio.to_thread(store.accounts_of, person.id) if p.site == site]
        if mine:
            await _reject(ctx, _one_per_site_message(site, mine[0].username), refund_cooldown=True)
            return

    month = sources.current_month()
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            try:
                start = await sources.start_rating(session, site, username, month)
                username = await sources.account_name(session, site, username)  # the site's own spelling
            except sources.SourceError as exc:
                await _reject(ctx, str(exc), refund_cooldown=True)
                return

    outcome = await asyncio.to_thread(store.add_player, site, username, person.id, month, start, one_per_site=limited)
    if outcome == store.EXISTS:  # someone added them while we were on the phone to the site
        await _reject(ctx, f"{username} is already on the list for {site}", refund_cooldown=True)
        return
    if outcome == store.LIMIT:  # a second add by the same person got in while we were on the phone to the site
        await _reject(ctx, _one_per_site_message(site), refund_cooldown=True)
        return
    log.info("%s %s on %s for %s (start rating %s)", outcome, username, site, person.id, start)
    _start_refresh(site, username)
    await _tick(ctx)


@bot.command()
async def remove(ctx, username: str, site: Optional[str] = None):
    """Take a player off the list. Their history is kept."""
    if site is not None:
        site = site.lower()
        if site not in sources.SITES:
            await _reject(ctx, "the site must be `chess.com` or `lichess`")
            return

    matches = await asyncio.to_thread(store.find_active, username, site)
    if not matches:
        await _reject(ctx, f"'{sources.shorten(username)}' isn't on the list")
        return
    if len(matches) > 1:
        sites = " and ".join(m.site for m in matches)
        await _reject(ctx, f"{username} is on the list for {sites} - say which, e.g. `!remove {username} {matches[0].site}`")
        return

    player = matches[0]
    if not _is_admin(ctx.author.id) and player.added_by != ctx.author.id:
        await _reject(ctx, f"only whoever added {username}, or an admin, can remove them")
        return

    await asyncio.to_thread(store.remove_player, player.site, player.username)
    log.info("removed %s on %s (by %s)", player.username, player.site, ctx.author.id)
    await _tick(ctx)


async def _pick_player(ctx, username, site, command, *, refund_cooldown=False):
    """Work out which registered account a command means, or say why it can't.

    With no username it is the caller's own account (asking which if they have
    several). With one it is that registered player, on the given site if the same
    name is on both. Returns a store.Player, or None after replying with the reason.
    """
    if site is not None:
        site = site.lower()
        if site not in sources.SITES:
            await _reject(ctx, "the site must be `chess.com` or `lichess`", refund_cooldown=refund_cooldown)
            return None

    if username is None:
        mine = await asyncio.to_thread(store.accounts_of, ctx.author.id)
        if not mine:
            await _reject(ctx, "you haven't added an account yet - use `!add <username> <site>` first", refund_cooldown=refund_cooldown)
            return None
        if len(mine) > 1:
            names = ", ".join(f"{p.username} ({p.site})" for p in mine)
            await _reject(
                ctx,
                f"you have {len(mine)} accounts: {names} - say which, e.g. `!{command} {mine[0].username} {mine[0].site}`",
                refund_cooldown=refund_cooldown,
            )
            return None
        return mine[0]

    matches = await asyncio.to_thread(store.find_active, username, site)
    if not matches:
        await _reject(ctx, f"'{sources.shorten(username)}' isn't on the list", refund_cooldown=refund_cooldown)
        return None
    if len(matches) > 1:
        sites = " and ".join(m.site for m in matches)
        await _reject(
            ctx,
            f"{username} is on the list for {sites} - say which, e.g. `!{command} {username} {matches[0].site}`",
            refund_cooldown=refund_cooldown,
        )
        return None
    return matches[0]


async def _join_challenge(ctx, username, site, month, when, command):
    """Shared by !100gob (this month) and !100gobnext (next month).

    `when` is how a reply refers to the month ("this month", or its name) and
    `command` is the command's own name, for the examples in replies.
    """
    player = await _pick_player(ctx, username, site, command)
    if player is None:
        return
    if username is not None and not _is_admin(ctx.author.id) and player.added_by != ctx.author.id:
        await _reject(ctx, f"only whoever added {player.username}, or an admin, can put them in 100GOB")
        return

    outcome = await asyncio.to_thread(store.join_100gob, player.site, player.username, month)
    if outcome == store.ALREADY:
        await _reject(ctx, f"{player.username} is already in 100GOB {when}")
        return
    if outcome == store.NO_ROW:  # they were found active a moment ago, so the month must be closed
        await _reject(ctx, f"{render.month_title(month)} is already closed for {player.username}")
        return
    log.info("100GOB: %s on %s in for %s (by %s)", player.username, player.site, month, ctx.author.id)
    await _tick(ctx)


@bot.command(name="100gob")
async def gob(ctx, username: Optional[str] = None, site: Optional[str] = None):
    """Join this month's 100GOB challenge. If the month's row doesn't exist yet the
    sign-up is kept and applied when it is created."""
    await _join_challenge(ctx, username, site, sources.current_month(), "this month", "100gob")


@bot.command(name="100gobnext")
async def gob_next(ctx, username: Optional[str] = None, site: Optional[str] = None):
    """Sign up for next month's 100GOB challenge (next month means the month after the current UTC month)."""
    month = sources.next_month(sources.current_month())
    await _join_challenge(ctx, username, site, month, f"for {render.month_title(month)}", "100gobnext")


MONTH_HELP = "try `2026-08`, `august` or `last`"


async def _month_or_reject(ctx, text, current, *, refund_cooldown=False):
    """The "YYYY-MM" month a command's month word means (the current month if there is none), or None after
    replying with why it can't be used."""
    if text is None:
        return current
    month = monthargs.parse_month(text, current)
    if month is None:
        await _reject(ctx, f"I don't know the month '{sources.shorten(text)}': {MONTH_HELP}", refund_cooldown=refund_cooldown)
        return None
    if monthargs.is_future(month, current):
        await _reject(ctx, f"{render.month_title(month)} hasn't happened yet", refund_cooldown=refund_cooldown)
        return None
    return month


async def _no_data_text(month, whose=None):
    """What to say when no results are held for `month`: when the bot's records begin, and why not earlier."""
    if whose:
        history = await asyncio.to_thread(store.player_history, *whose)
        if history:
            first = history[-1]["month"]
            return (f"{whose[1]} has no results held for {render.month_title(month)}. Their history starts in "
                    f"{render.month_title(first)}, the month they registered: earlier months aren't filled in.")
    earliest = await asyncio.to_thread(store.earliest_month)
    if earliest is None:
        return "Nothing has been recorded yet."
    return (f"There are no results held for {render.month_title(month)}. The bot started counting in {render.month_title(earliest)}: "
            "a player's history begins in the month they register, and earlier months aren't filled in.")


async def _player_stats(ctx, username, site, command, full, month_text=None):
    """Shared by !mystats and !mystatsfull: one player's month, from their games.

    Unlike !results this calls the chess sites, though only for games not already
    held (see gamecache), which is why both commands carry the cooldown. The month is
    the current one unless a month is given, as a fourth word or in place of the site
    ("!mystats alice last") or of the name ("!mystats last", when no registered player
    has that word as a name).
    """
    current = sources.current_month()
    if month_text is None and site is not None and site.lower() not in sources.SITES and monthargs.parse_month(site, current):
        month_text, site = site, None
    if month_text is None and username is not None and site is None and monthargs.parse_month(username, current):
        if not await asyncio.to_thread(store.find_active, username):
            month_text, username = username, None

    player = await _pick_player(ctx, username, site, command, refund_cooldown=True)
    if player is None:
        return
    month = await _month_or_reject(ctx, month_text, current, refund_cooldown=True)
    if month is None:
        return

    row = await asyncio.to_thread(store.month_row, player.site, player.username, month)
    if row is None:
        if month == current:
            text = f"{player.username} has no results for {render.month_title(month)} yet - try again shortly"
        else:
            text = await _no_data_text(month, (player.site, player.username))
        await _reject(ctx, text, refund_cooldown=True)
        return

    async with ctx.typing():
        try:
            async with aiohttp.ClientSession() as session:
                games = await gamecache.month_games(session, player.site, player.username, month)
        except sources.SourceError as exc:
            await _reject(ctx, str(exc), refund_cooldown=True)
            return

    analysis_text = None
    if not full:
        try:  # the analysis part is a bonus: if it can't be read, the rest of the summary still goes out
            analysis_text = render_analysis.mystats_part(
                await asyncio.to_thread(analysis_reports.month_summary, player.site, player.username, month))
        except Exception:
            log.exception("could not read the analysis for %s on %s", player.username, player.site)

    start = row["start_rating"]
    summary = stats.summarise(games, start)
    if full:
        messages = render.render_mystatsfull(player.username, player.site, month, summary, stats.records(games), stats.splits(games, start),
                                             so_far=month == current)
    else:
        tables = stats.opening_tables(games)
        messages = render.render_mystats(player.username, player.site, month, summary, tables, stats.opening_verdicts(tables),
                                         analysis_text=analysis_text, so_far=month == current)
    for message in messages:
        await ctx.send(message)


@bot.command(name="mystats", aliases=["stats"])
@commands.dynamic_cooldown(_cooldown_for, commands.BucketType.user)
async def mystats(ctx, username: Optional[str] = None, site: Optional[str] = None, month: Optional[str] = None):
    """Results and openings for one player this month, or another month: your own account, or any registered player."""
    await _player_stats(ctx, username, site, "mystats", full=False, month_text=month)


@bot.command(name="mystatsfull", aliases=["statsfull"])
@commands.dynamic_cooldown(_cooldown_for, commands.BucketType.user)
async def mystatsfull(ctx, username: Optional[str] = None, site: Optional[str] = None, month: Optional[str] = None):
    """Records and the splits by opponent rating, colour, weekday and time of day."""
    await _player_stats(ctx, username, site, "mystatsfull", full=True, month_text=month)


def _admin_only(ctx):
    return _is_admin(ctx.author.id)


@bot.command(name="closemonth")
@commands.check(_admin_only)
async def closemonth(ctx):
    """Admins only: close every finished month that is still open and post its final table.

    The same job that runs by itself, run now, for when it couldn't (a site was down, a
    player's account had gone). It never closes a month twice or posts a table twice.
    """
    now = datetime.now(timezone.utc)
    if not await asyncio.to_thread(store.unclosed_months, sources.current_month(now)):
        await ctx.send("Nothing to close: every finished month is already closed.")
        return

    async with ctx.typing():
        results_ = await monthend.close_due_months(now=now)

    channel = await _post_channel()
    everything_ok = True
    for result in results_:
        try:
            if not result.ok:
                everything_ok = False
                await channel.send(announce.close_failure_text(result.month, result.failures))
            elif await asyncio.to_thread(store.claim_announcement, "month_end", result.month, now.isoformat()):
                try:
                    await send_final_table(channel, result.month, now)
                except Exception:
                    await asyncio.to_thread(store.release_announcement, "month_end", result.month)
                    raise
        except Exception:
            log.exception("!closemonth couldn't post for %s", result.month)
            everything_ok = False
            await _reject(ctx, f"{render.month_title(result.month)} is closed but I couldn't post it - check the logs")
            return
    log.info("!closemonth by %s: %s", ctx.author.id, [(r.month, r.ok) for r in results_])
    if everything_ok:
        await _tick(ctx)
    else:
        await _react(ctx, "❌")  # the failure notice has already been posted


@bot.command(name="lastgame")
async def lastgame(ctx, username: Optional[str] = None, site: Optional[str] = None):
    """The bot's analysis of a player's most recent analysed game: both sides, with a link. Reads only what the bot
    already holds, so it makes no calls to the chess sites and needs no cooldown."""
    player = await _pick_player(ctx, username, site, "lastgame")
    if player is None:
        return
    games = await asyncio.to_thread(analysis_reports.player_games, player.site, player.username, 1)
    waiting = await asyncio.to_thread(analysis_reports.waiting_count, player.site, player.username)
    if not games:
        if waiting:
            await ctx.send(f"None of {player.username}'s games have been analysed yet ({waiting} waiting).")
        elif not settings.ANALYSIS_ENABLED:
            await ctx.send("Game analysis isn't switched on yet.")
        else:
            await ctx.send(f"No analysed games for {player.username} yet: analysis covers games from when it was switched on.")
        return
    await ctx.send(render_analysis.render_lastgame(player.username, player.site, games[0], waiting))


@bot.command(name="analysisq", aliases=["analysisqueue"])
@commands.check(_admin_only)
async def analysisq(ctx):
    """Admins only: how the analysis queue stands, and whether the worker is asking for work."""
    status = await asyncio.to_thread(analysis_queue.status, int(datetime.now(timezone.utc).timestamp()), analysis.METHOD_VERSION)
    await ctx.send(render_analysis.render_queue_status(status, settings.ANALYSIS_ENABLED))


_queuemonth_lock = asyncio.Lock()


@bot.command(name="queuemonth")
@commands.check(_admin_only)
async def queuemonth(ctx):
    """Admins only: put this month's games so far, for every registered player, in the analysis queue.

    The refresher only queues games it sees from now on, so this catches up the month's earlier games. It fetches each
    player's month from their site, one player at a time, so it can take a few minutes. Safe to run again."""
    if not settings.ANALYSIS_ENABLED:
        await _reject(ctx, "analysis is switched off (`ANALYSIS_ENABLED`)")
        return
    if _queuemonth_lock.locked():
        await _reject(ctx, "it's already running - wait for it to finish")
        return
    async with _queuemonth_lock:
        async with ctx.typing():
            result = await analysis_feed.backfill(sources.current_month(), datetime.now(timezone.utc))
    outcome = result.outcome
    queued = outcome[analysis_queue.QUEUED] + outcome[analysis_queue.QUEUED_LOW]
    text = (f"Queued {queued} game(s) from {result.players} player(s) for analysis "
            f"({outcome[analysis_queue.ALREADY_QUEUED]} were already queued, {outcome[analysis_queue.OVER_LIMIT]} over the monthly limit).")
    if result.failures:
        text += "\nCouldn't do: " + "; ".join(f"{name} ({site}): {sources.shorten(why, 80)}" for name, site, why in result.failures)
    log.info("!queuemonth by %s: %s", ctx.author.id, dict(outcome))
    await ctx.send(text)
    if not result.failures:
        await _tick(ctx)


@bot.command()
async def results(ctx, month: Optional[str] = None):
    """This month so far, or a past month's final table, from the stored totals. Makes no calls to the chess sites."""
    current = sources.current_month()
    chosen = await _month_or_reject(ctx, month, current)
    if chosen is None:
        return
    now = datetime.now(timezone.utc)
    if chosen == current:
        rows = await asyncio.to_thread(store.results, chosen)
        signed_up = await asyncio.to_thread(store.signups, sources.next_month(chosen))
        messages = render.render_results(rows, chosen, now, GOB_TARGET, signed_up)
    else:
        rows = await asyncio.to_thread(store.results, chosen, True)  # only the players who were in that month
        if not rows:
            await ctx.send(await _no_data_text(chosen))
            return
        messages = render.render_results(rows, chosen, now, GOB_TARGET, final=True)
    for message in messages:
        await ctx.send(message)


@bot.command(name="history")
async def history(ctx, username: Optional[str] = None, site: Optional[str] = None):
    """A player's months one line each, newest first. Reads only what the bot holds, so no calls to the chess sites."""
    player = await _pick_player(ctx, username, site, "history")
    if player is None:
        return
    rows = await asyncio.to_thread(store.player_history, player.site, player.username)
    if not rows:
        await ctx.send(await _no_data_text(sources.current_month(), (player.site, player.username)))
        return
    try:
        accuracy = await asyncio.to_thread(analysis_reports.monthly_accuracy, player.site, player.username)
    except Exception:  # the accuracy column is a bonus
        log.exception("could not read the accuracy history for %s on %s", player.username, player.site)
        accuracy = {}
    for message in render.render_history(player.username, player.site, rows, GOB_TARGET, accuracy, current=sources.current_month()):
        await ctx.send(message)


@bot.event
async def on_command(ctx):
    log.info("command !%s from %s in channel %s", ctx.command.qualified_name, ctx.author.id, ctx.channel.id)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.TooManyArguments)):
        usage = USAGE.get(ctx.command.name if ctx.command else "")
        await _reject(ctx, f"Usage: `{usage}`" if usage else "I didn't understand that")
    elif isinstance(error, commands.CommandOnCooldown):
        await _reject(ctx, f"slow down - try again in {error.retry_after:.0f}s")
    elif isinstance(error, commands.CommandNotFound):
        # Silent in Discord (the bot shouldn't answer every "!" message), but say so in the log.
        log.info("ignored: no command called !%s (channel %s)", ctx.invoked_with, ctx.channel.id)
    elif isinstance(error, commands.CheckFailure):
        log.info("ignored: !%s in channel %s (not an allowed channel, or the command is not permitted)", ctx.invoked_with, ctx.channel.id)
    else:
        log.exception("command failed", exc_info=error)
        await _reject(ctx, "something went wrong, check the logs")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set")
    store.check_location()  # refuse to start (and let the service retry) if the database's disk isn't there
    # Held for as long as the bot runs: a second copy from this folder refuses to start.
    _instance_lock = singleton.acquire(Path(__file__).with_name("playmoreblitz.lock"))
    # log_handler=None: our own logging setup above is used, so each line appears once.
    bot.run(token, log_handler=None)
