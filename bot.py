"""PlayMoreBlitz Discord bot.

Nudges members to play more blitz: monthly rated-blitz game counts per player.
Commands so far: !add, !remove, !results and a help command. The rest come in later steps.
"""

import asyncio
import contextlib
import io
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import analysis
import analysis_feed
import analysis_queue
import analysis_reports
import announce
import export_data
import gamecache
import monitoring
import monthargs
import monthend
import obit
import refresh
import render
import render_analysis
import render_obit
import settings
import singleton
import sources
import stats
import store
import usage as usage_stats

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
ALERT_USER_IDS = settings.ALERT_USER_IDS  # sent a private message when something needs attention
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
    "obit": "!obit [game link or id]",
    "export": "!export [summary] [period]",
    "usage": "!usage [days]",
    "clear": "!clear",
    "setowner": "!setowner <username> <@member> [site]",
}

intents = discord.Intents.default()
intents.message_content = True
# Command input gets echoed back in replies unfiltered - this stops any of it
# from ever triggering a real @everyone/@here/role/user ping.
class DMContext(commands.Context):
    """The context the bot uses: whatever it says in a direct message gets a Delete button, because Discord doesn't let anyone
    delete a bot's message in a DM themselves. In a server nothing changes."""

    async def send(self, content=None, **kwargs):
        if self.guild is None and "view" not in kwargs:
            kwargs["view"] = DeleteButton()
        return await super().send(content, **kwargs)


class PlayMoreBlitzBot(commands.Bot):
    async def get_context(self, origin, *, cls=DMContext):
        return await super().get_context(origin, cls=cls)


bot = PlayMoreBlitzBot(
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
        await _track_refresh(outcomes)
        await post_month_end_if_due()
        await asyncio.to_thread(usage_stats.prune)
    except Exception:  # one bad cycle must not stop the loop
        log.exception("refresh cycle crashed")
        await _alert_admins("refresh_crash", "a refresh cycle crashed. The log has the details.", monitoring.ERROR_REPEAT_SECONDS)


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


_alerts = monitoring.Alerts()
_health = {"refresh_failures": 0, "heartbeat_failing": False}


async def _count(name, user_id=None):
    """Note a use in the usage counts. Never raises: counting must not break what is being counted."""
    try:
        await asyncio.to_thread(usage_stats.count, name, user_id)
    except Exception:
        log.exception("couldn't count a use of %s", name)


async def _alert_admins(key, text, repeat=monitoring.REPEAT_SECONDS):
    """Send `text` privately to each of ALERT_USER_IDS, unless the same `key` was reported within `repeat` seconds. Never raises."""
    if not _alerts.due(key, repeat):
        return
    for user_id in ALERT_USER_IDS:
        try:
            await _dm(user_id, [f"⚠ PlayMoreBlitz: {text}"])
        except Exception:
            log.exception("couldn't send an alert to %s: %s", user_id, text[:100])


async def _track_refresh(outcomes):
    """Note whether a refresh cycle failed throughout, and report it once it has done so several times running."""
    if monitoring.refresh_failed(outcomes):
        _health["refresh_failures"] += 1
    else:
        _health["refresh_failures"] = 0
        _alerts.clear("refresh")
    problem = monitoring.refresh_problem(_health["refresh_failures"])
    if problem:
        await _alert_admins("refresh", problem)


@tasks.loop(minutes=5)
async def health_loop():
    """Every five minutes: is the analysis worker alive while games wait, and are the nightly backups happening? A problem
    is reported now, and again every few hours while it lasts; when it is over it is forgotten, so a return is reported at once."""
    try:
        checks = {"backup": monitoring.backup_problem(settings.BACKUP_DIR)}
        if settings.ANALYSIS_ENABLED:
            checks["worker"] = monitoring.worker_problem(await asyncio.to_thread(analysis_queue.status, int(time.time())))
        for key, problem in checks.items():
            if problem:
                await _alert_admins(key, problem)
            else:
                _alerts.clear(key)
    except Exception:
        log.exception("health check crashed")


@tasks.loop(seconds=60)
async def heartbeat_loop():
    """Ping HEARTBEAT_URL to say the bot is alive; a monitoring service raises the alarm when the pings stop."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(settings.HEARTBEAT_URL) as response:
                ok = response.status < 400
    except Exception:
        ok = False
    if not ok and not _health["heartbeat_failing"]:
        log.warning("the heartbeat ping failed (it is retried every minute)")  # not the address: it is a secret of a sort
    _health["heartbeat_failing"] = not ok


@tasks.loop(seconds=60)
async def obit_loop():
    """Send the reviews people asked for once their games have been analysed (or say that they can't be)."""
    try:
        for request in await asyncio.to_thread(obit.outstanding):
            try:
                await _process_obit(request)
            except Exception:  # one bad request must not hold up the rest
                log.exception("could not answer the !obit request for %s %s", request["site"], request["game_id"])
    except Exception:
        log.exception("!obit delivery crashed")
        await _alert_admins("obit_loop", "sending the reviews people asked for crashed. The log has the details.")


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
    bot.add_view(DeleteButton())  # so the Delete button on a DM sent before a restart still works
    await sync_slash_commands()
    if not obit_loop.is_running():
        obit_loop.start()
    if not health_loop.is_running():
        health_loop.start()
    if settings.HEARTBEAT_URL and not heartbeat_loop.is_running():
        heartbeat_loop.start()
    if not daily_posts.is_running():
        daily_posts.start()
        try:
            await post_signup_call_if_due()  # catch up if the bot was down when a post was due
        except Exception:
            log.exception("catch-up posts crashed")


DM_COMMANDS = ("obit", "export", "clear")  # the private commands members can send the bot in a direct message
ADMIN_DM_COMMANDS = ("analysisq", "queuemonth", "closemonth", "setowner", "usage")  # system-type commands: an admin's, and only in a direct message
ADMIN_HINT = "Admin commands work only in a direct message to me: send it there."


def _in_dm(ctx):
    """True for a direct message to the bot (a message in a server has a guild; a test context with none isn't a DM)."""
    return getattr(ctx, "guild", False) is None


@bot.check
async def _in_allowed_channel(ctx):
    name = getattr(getattr(ctx, "command", None), "name", None)
    if _in_dm(ctx):  # a direct message: the private commands (which check for themselves that the person is on the server), and an admin's system commands
        return name in DM_COMMANDS or (name in ADMIN_DM_COMMANDS and _is_admin(ctx.author.id))
    if name in ADMIN_DM_COMMANDS:  # never in a channel: they would fill it with system talk
        return False
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
        "`/obit [game link or id]` - a private review of one of your own games (Openings, Blunders, Interesting, Takeaway), "
        "sent by DM; no link means your latest game, and if it isn't analysed yet it jumps the queue. Nothing appears in the "
        "channel. Or send me `!obit` in a direct message. Registered members on the server only\n"
        "`/export [period] [what]` - your own games as a CSV file for a spreadsheet, one file per account, sent by DM; period is "
        "this month, last, week, a month like 2026-08 or all, and `what` can be games or summary. Or send me `!export` in a direct "
        "message\n"
        "`!clear` - send it to me in a direct message to delete everything I've sent you there, old messages included\n"
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
    if person.id != ctx.author.id and not await _on_server(getattr(ctx, "guild", None), person.id):
        await _reject(ctx, NOT_ON_SERVER, refund_cooldown=True)
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


@bot.command(name="setowner")
@commands.check(_admin_only)
async def setowner(ctx, username: str, member: discord.User, site: Optional[str] = None):
    """Admin: hand a registered account to the member it belongs to. Accounts an admin registered without naming the
    member belong to the admin, so `!obit` and `!mystats` with no name would treat them all as the admin's own."""
    if site is not None:
        site = site.lower()
        if site not in sources.SITES:
            await _reject(ctx, "the site must be `chess.com` or `lichess`")
            return
    if await _member_status(member.id) is False:  # by the servers the bot serves, since this may be a direct message
        await _reject(ctx, NOT_ON_SERVER)
        return
    matches = await asyncio.to_thread(store.find_active, username, site)
    if not matches:
        await _reject(ctx, f"'{sources.shorten(username)}' isn't on the list")
        return
    if len(matches) > 1:
        await _reject(ctx, f"{username} is on the list for {' and '.join(m.site for m in matches)} - say which, e.g. "
                           f"`!setowner {username} @member {matches[0].site}`")
        return
    player = matches[0]
    outcome = await asyncio.to_thread(store.set_owner, player.site, player.username, member.id, one_per_site=not _is_admin(member.id))
    if outcome == store.LIMIT:
        await _reject(ctx, f"that member already has a {player.site} account on the list - it's one per site (admins can hold several)")
    elif outcome == store.UNCHANGED:
        await _reject(ctx, f"{player.username} already belongs to that member")
    elif outcome == store.CHANGED:
        log.info("owner of %s on %s set to %s by %s", player.username, player.site, member.id, ctx.author.id)
        await _tick(ctx)
    else:  # removed while we were looking
        await _reject(ctx, f"{player.username} isn't on the list")


@bot.command(name="usage")
@commands.check(_admin_only)
async def usage_command(ctx, days: int = 7):
    """Admins only, in a direct message: how the bot has been used over the last few days (1 to 30). Counts only."""
    days = max(1, min(days, usage_stats.KEEP_DAYS - 5))
    await ctx.send(usage_stats.render_report(await asyncio.to_thread(usage_stats.report, days)))


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


NOT_ON_SERVER = "that person isn't on this server"


async def _on_server(guild, user_id):
    """False only if the server says `user_id` isn't a member of it. True if they are, and also if it can't be checked (no
    server to ask, or the lookup failed): a member must not be turned away because of a hiccup."""
    if guild is None:
        return True
    try:
        await guild.fetch_member(user_id)
    except discord.NotFound:
        return False
    except discord.HTTPException as exc:
        log.warning("couldn't check whether %s is on the server (%s)", user_id, exc.status)
    return True


class DeleteButton(discord.ui.View):
    """A "Delete" button on the bot's direct messages. Discord only lets someone delete their own messages in a DM, never
    the other side's, so the bot deletes its own message when asked. It has no timeout and a fixed id, and is registered
    at start-up, so the button on an old message still works after the bot has restarted."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.secondary, custom_id="pmb:delete_dm")
    async def delete(self, interaction, button):
        if interaction.guild is not None or interaction.message is None:  # only ever a DM: never a message in a channel
            await interaction.response.send_message("That button only works in a direct message.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException as exc:
            log.warning("couldn't delete a DM on request (%s)", exc.status)


def _home_guilds():
    """The servers the bot serves: those with an allowed channel it can see."""
    guilds = {}
    for channel_id in ALLOWED_CHANNEL_IDS:
        guild = getattr(bot.get_channel(channel_id), "guild", None)
        if guild is not None:
            guilds[guild.id] = guild
    return list(guilds.values())


async def _member_status(user_id):
    """True if `user_id` is a member of a server the bot serves, False if they are a member of none, None if that can't be
    told (the bot can't see a server, or the lookup failed)."""
    guilds = _home_guilds()
    if not guilds:
        return None
    unknown = False
    for guild in guilds:
        try:
            await guild.fetch_member(user_id)
            return True
        except discord.NotFound:
            continue
        except discord.HTTPException as exc:
            log.warning("couldn't check whether %s is in server %s (%s)", user_id, guild.id, exc.status)
            unknown = True
    return None if unknown else False


async def _dm(user_id, messages):
    """Send `messages` to the user privately, each with a Delete button. Raises discord.Forbidden if they don't accept
    messages from the bot."""
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    for message in messages:
        await user.send(message, view=DeleteButton())
    log.info("DM sent to %s (%d message%s)", user_id, len(messages), "" if len(messages) == 1 else "s")  # never what it said


async def _process_obit(request, *, immediate=False):
    """Send, hold or close one !obit request. Returns "sent", "waiting", "no_dm" (they don't accept DMs from the bot) or
    "closed" (given up on, answered already, or their asker has left the server). Whoever removes the request from the
    table is the one that answers it.

    `immediate` is for a request answered as it is made: the person has just written in the channel, so they are on the
    server and the command replies to them itself. Otherwise nothing is sent to someone who has since left the server, and a
    DM that can't be delivered is reported in the channel they asked in."""
    row = await asyncio.to_thread(obit.game_row, request["site"], request["game_id"])
    action = obit.action_for(request, row, int(time.time()))
    if action == obit.WAIT:
        return "waiting"
    if not await asyncio.to_thread(obit.close_request, request["user_id"], request["site"], request["game_id"]):
        return "closed"
    if not immediate and await _member_status(request["user_id"]) is False:
        log.info("dropped the !obit request of %s: they are no longer on the server", request["user_id"])
        return "closed"
    game = f"{request['site']} {request['game_id']}"
    try:
        if action == obit.SEND:
            side = obit.side_of(row, request["username"])
            messages = render_obit.render_obit(request["username"], request["site"], row, side)
        elif action == obit.CANT:
            messages = [f"I couldn't review that game: {obit.cant_reason(row)}."]
        else:
            messages = ["I couldn't get to that game's review in time (the analysis is behind). Ask again in a while."]
        await _dm(request["user_id"], messages)
    except discord.Forbidden:
        log.warning("couldn't send the review of %s to %s: they don't accept DMs from the bot", game, request["user_id"])
        if not immediate:
            await _say_no_dm(request["channel_id"], request["user_id"])
        return "no_dm"
    except discord.HTTPException:
        log.exception("couldn't send an !obit DM; will try again")
        await asyncio.to_thread(obit.add_request, request["user_id"], request["site"], request["game_id"], request["username"],
                                request["channel_id"], request["requested_at"])
        return "waiting"
    if action == obit.SEND:
        log.info("review of %s sent to %s", game, request["user_id"])
        await _count(usage_stats.OBIT_SENT)
        return "sent"
    log.info("the request of %s for %s was closed without a review (%s)", request["user_id"], game, action)
    return "closed"


NO_DM = ("I couldn't send you a direct message. Allow direct messages from server members "
         "(server name > Privacy Settings), then ask again.")


async def _say_no_dm(channel_id, user_id):
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is not None:
        try:
            await channel.send(f"<@{user_id}> {NO_DM}", allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)]))
        except discord.HTTPException as exc:
            log.warning("couldn't tell %s in channel %s that their DMs are closed (%s)", user_id, channel_id, exc.status)


_lookups = {}  # user id -> when we last looked on the sites for their games (monotonic seconds)
LOOKUP_GAP_SECONDS = 120  # how often one person can make the bot look on the sites for their new games
_monotonic = time.monotonic
TEXT_STAYS_SECONDS = 20  # how long the bot's error text to a quiet !obit stays in the channel


async def _obit_flow(user_id, channel_id, game, *, typing=None):
    """!obit and /obit up to what to tell the person (see _obit_steps); notes in the log how the request came out, by kind only:
    sent, waiting, no_dm or error, never what the person typed."""
    kind, text = await _obit_steps(user_id, channel_id, game, typing=typing)
    log.info("obit request from %s: %s", user_id, kind)
    return kind, text


async def _obit_steps(user_id, channel_id, game, *, typing=None):
    """Everything !obit and /obit do, up to what to tell the person. Returns (kind, text): "sent" (the review is in their
    DMs), "waiting" (queued: it will follow), "no_dm" (they don't accept DMs from the bot) or "error" (text says why).

    `typing` is a function giving an async context manager to show while the chess sites are looked at. With no game named
    the sites are looked at first, so a game played a minute ago is the latest one; with a game named, only if it isn't held.
    Either way one person makes the bot look at most once every LOOKUP_GAP_SECONDS."""
    if not settings.ANALYSIS_ENABLED:
        return "error", "game analysis isn't switched on yet"
    accounts = await asyncio.to_thread(store.accounts_of, user_id)
    if not accounts:
        return "error", "you haven't added an account yet - use `!add <username> <site>` in the server's channel first"
    refs = None
    if game is not None:
        refs = obit.candidates(game)
        if not refs:
            return "error", f"I can't read '{sources.shorten(game)}' as a game: give a Lichess or Chess.com game link, or the game's id"

    def look():
        return obit.find_game(accounts, refs) if refs else obit.latest_game(accounts)

    def may_look_at_the_sites():
        last = _lookups.get(user_id)
        return last is None or _monotonic() - last >= LOOKUP_GAP_SECONDS

    async def look_at_the_sites():
        _lookups[user_id] = _monotonic()
        async with (typing() if typing else contextlib.nullcontext()):
            for account in accounts:
                await refresh.refresh_one(account.site, account.username)

    if refs is None and may_look_at_the_sites():
        await look_at_the_sites()
    found = await asyncio.to_thread(look)
    held_back = False
    if found is None and refs is not None:
        if may_look_at_the_sites():
            await look_at_the_sites()
            found = await asyncio.to_thread(look)
        else:
            held_back = True
    if found is None:
        if held_back:
            return "error", "I looked on the sites for your games a moment ago and that one wasn't there: try again in a couple of minutes"
        return "error", ("I can't find that among your games. I only hold games played since you registered." if refs
                         else "I don't hold any games of yours yet.")
    row, account, side = found
    key = (row["site"], row["game_id"])

    if await asyncio.to_thread(obit.waiting_count, user_id, key) >= obit.MAX_WAITING:
        return "error", f"you already have {obit.MAX_WAITING} reviews waiting: I'll DM them as they finish"
    outcome = await asyncio.to_thread(analysis_queue.prioritise, *key)
    if outcome is not None:
        status, reason = outcome
        if status == analysis_queue.SKIPPED:
            return "error", f"I couldn't review that game: {obit.SKIP_WORDS.get(reason, 'it was skipped')}."
    now = int(time.time())
    await asyncio.to_thread(obit.add_request, user_id, key[0], key[1], account.username, channel_id, now)
    request = {"user_id": user_id, "site": key[0], "game_id": key[1], "username": account.username,
               "channel_id": channel_id, "requested_at": now}
    result = await _process_obit(request, immediate=True)
    if result == "sent":
        return "sent", "I've sent you the review by DM."
    if result == "waiting":
        return "waiting", "Analysing that game now: it's at the front of the queue. I'll DM you the review when it's done."
    if result == "no_dm":
        return "no_dm", NO_DM
    return "error", "I couldn't review that game"


def _not_a_member_text(status):
    """What to say to someone in a DM whom _member_status didn't confirm (False: not a member; None: couldn't tell)."""
    return ("This is only for members of the server." if status is False
            else "I couldn't check that you're on the server just now: try again in a moment.")


OBIT_HINT = "Use `/obit`, or send me `!obit` in a direct message: reviews are private, so I don't do them in the channel."


@bot.command(name="obit")
async def obit_command(ctx, game: Optional[str] = None):
    """A private review of one of your own games. Send it to the bot in a direct message: it works only there, and only for
    a registered member who is on the server, so the whole exchange stays between you and the bot. In the channel it just
    points to `/obit` and to DMs, in a message that removes itself. No link or id means your latest game. If the game
    hasn't been analysed yet it goes to the front of the queue and the review follows when it's done."""
    if not _in_dm(ctx):
        try:
            await ctx.send(OBIT_HINT, delete_after=TEXT_STAYS_SECONDS)
        except discord.HTTPException as exc:
            log.warning("couldn't send a reply in channel %s (%s)", ctx.channel.id, exc.status)
        return
    status = await _member_status(ctx.author.id)
    if status is True:
        kind, text = await _obit_flow(ctx.author.id, ctx.channel.id, game, typing=ctx.typing)
    else:
        kind, text = "error", _not_a_member_text(status)
    if kind == "sent":
        await _react(ctx, "✅")  # the review is right here
        return
    await _react(ctx, "⏳" if kind == "waiting" else "❌")
    try:
        await ctx.send(text)
    except discord.HTTPException as exc:
        log.warning("couldn't answer a direct message (%s): %s", exc.status, text[:80])


@bot.tree.command(name="obit", description="A private review of one of your games, sent to you by DM")
@app_commands.describe(game="A Lichess or Chess.com game link or id. Leave it out for your latest game.")
@app_commands.guild_only()
async def obit_slash(interaction: discord.Interaction, game: Optional[str] = None):
    """The same as !obit, but nothing appears in the channel: the only reply is one only the person asking can see."""
    if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message("This command only works in the blitz channel.", ephemeral=True)
        return
    await _count("obit", interaction.user.id)
    await interaction.response.defer(ephemeral=True)  # looking at the chess sites can take longer than Discord waits for a reply
    _, text = await _obit_flow(interaction.user.id, interaction.channel_id, game)
    await interaction.followup.send(text, ephemeral=True)


EXPORT_HINT = "Use `/export`, or send me `!export` in a direct message: your data is private, so I don't do it in the channel."
EXPORT_GAP_SECONDS = 30  # between one person's exports
_exports = {}  # user id -> when they last had an export (monotonic seconds)


async def _export_flow(user_id, what, period_text):
    """!export and /export up to sending (see _export_steps); notes in the log when a request is refused, without the reason
    (which may quote what the person typed)."""
    parts, error = await _export_steps(user_id, what, period_text)
    log.info("export request from %s (%s): %s", user_id, what, "refused" if error else f"{sum(1 for p in parts if 'data' in p)} file(s) ready")
    return parts, error


async def _export_steps(user_id, what, period_text):
    """Everything !export and /export do, up to sending. Returns (parts, error): `parts` is a list with one dict per account
    (the unit is a username on a site) of "text" and, unless that account had no games, "filename" and "data" (the CSV);
    `error` is text saying why not."""
    accounts = await asyncio.to_thread(store.accounts_of, user_id)
    if not accounts:
        return None, "you haven't added an account yet - use `!add <username> <site>` in the server's channel first"
    current = sources.current_month()
    period = export_data.parse_period(period_text, current, time.time())
    if period is None:
        return None, f"I don't know the period '{sources.shorten(period_text)}': try `week`, `last`, a month like `2026-08`, or `all`"
    if period.month and monthargs.is_future(period.month, current):
        return None, f"{export_data.describe(period)} hasn't happened yet"
    last = _exports.get(user_id)
    if last is not None and _monotonic() - last < EXPORT_GAP_SECONDS:
        return None, "one export at a time please: try again in a few seconds"
    parts = []
    for account in accounts:
        rows = await asyncio.to_thread(export_data.games_for, account.site, account.username, period)
        label = f"`{account.username}` · {render.SITE_NAMES.get(account.site, account.site)} · {export_data.describe(period)}"
        if not rows:
            parts.append({"text": f"{label}: no games held."})
            continue
        analysed = sum(1 for r in rows if export_data.has_analysis(r))
        if what == "summary":
            data = export_data.summary_csv([export_data.summary_cells(account.site, account.username, period, rows)])
            text = f"{label}: {len(rows)} games, summarised on one line."
        else:
            data = export_data.games_csv(account.site, account.username, rows)
            text = f"{label}: {len(rows)} games ({analysed} analysed)."
            counted = await asyncio.to_thread(store.month_row, account.site, account.username, period.month) if period.month else None
            if counted and counted["games"] > len(rows):
                text += (f" The bot counted {counted['games']} games that month; the file holds {len(rows)}, because games played "
                         "before analysis was switched on aren't in it.")
        if len(data) > export_data.MAX_BYTES:
            parts.append({"text": f"{label}: too many games for one file: choose a shorter period."})
            continue
        parts.append({"text": text, "filename": export_data.file_name(account.site, account.username, period, what), "data": data})
    if any("data" in p for p in parts):
        _exports[user_id] = _monotonic()
    return parts, None


async def _dm_parts(user_id, parts):
    """Send each part to the person privately, with its file if it has one and a Delete button. Raises discord.Forbidden if
    they don't accept messages from the bot."""
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    for part in parts:
        file = discord.File(io.BytesIO(part["data"]), filename=part["filename"]) if "data" in part else None
        await user.send(part["text"], file=file, view=DeleteButton())
    log.info("export sent to %s: %d message%s, %d file%s", user_id, len(parts), "" if len(parts) == 1 else "s",
             sum(1 for p in parts if "data" in p), "" if sum(1 for p in parts if "data" in p) == 1 else "s")
    await _count(usage_stats.EXPORT_SENT)


def _export_args(first, second):
    """("summary" or "games", period text or None) from the two optional words after !export, in either order."""
    words = [w for w in (first, second) if w]
    what = "summary" if any(w.lower() == "summary" for w in words) else "games"
    rest = [w for w in words if w.lower() not in ("summary", "games")]
    return what, (rest[0] if rest else None)


@bot.command(name="export")
async def export_command(ctx, first: Optional[str] = None, second: Optional[str] = None):
    """Your own games as CSV files for a spreadsheet, one file per account, sent in this conversation. Direct messages only,
    and only for a registered member who is on the server. `!export [period]`, or `!export summary [period]` for one line per
    account. The period is this month (the default), `last`, `week` (the last seven days), a month like `2026-08`, or `all`."""
    if not _in_dm(ctx):
        try:
            await ctx.send(EXPORT_HINT, delete_after=TEXT_STAYS_SECONDS)
        except discord.HTTPException as exc:
            log.warning("couldn't send a reply in channel %s (%s)", ctx.channel.id, exc.status)
        return
    status = await _member_status(ctx.author.id)
    parts, error = (None, _not_a_member_text(status)) if status is not True else await _export_flow(ctx.author.id, *_export_args(first, second))
    if error is None:
        try:
            await _dm_parts(ctx.author.id, parts)
        except discord.HTTPException as exc:
            log.warning("couldn't send an export (%s)", exc.status)
            error = "I couldn't send that just now: try again in a moment"
    if error is None:
        await _react(ctx, "✅")
        return
    await _react(ctx, "❌")
    try:
        await ctx.send(error)
    except discord.HTTPException as exc:
        log.warning("couldn't answer a direct message (%s): %s", exc.status, error[:80])


@bot.tree.command(name="export", description="Your games as a CSV file for a spreadsheet, sent to you by DM")
@app_commands.describe(period="This month (the default), last, week (the last 7 days), a month like 2026-08, or all",
                       what="games: a line per game. summary: one line for the period")
@app_commands.guild_only()
async def export_slash(interaction: discord.Interaction, period: Optional[str] = None, what: Literal["games", "summary"] = "games"):
    """The same as !export, but nothing appears in the channel: the only reply is one only the person asking can see."""
    if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message("This command only works in the blitz channel.", ephemeral=True)
        return
    await _count("export", interaction.user.id)
    await interaction.response.defer(ephemeral=True)
    parts, error = await _export_flow(interaction.user.id, what, period)
    if error is None:
        try:
            await _dm_parts(interaction.user.id, parts)
        except discord.Forbidden:
            error = NO_DM
        except discord.HTTPException as exc:
            log.warning("couldn't send an export (%s)", exc.status)
            error = "I couldn't send that just now: try again in a moment"
    if error is not None:
        await interaction.followup.send(error, ephemeral=True)
        return
    files = sum(1 for p in parts if "data" in p)
    await interaction.followup.send(f"I've sent you {files} file{'s' if files != 1 else ''} by DM." if files else "There were no games to send.",
                                    ephemeral=True)


CLEAR_HINT = "Send me `!clear` in a direct message: it clears what I've sent you there, so it isn't done in the channel."
CLEAR_LIMIT = 1000  # how many recent messages in the conversation `!clear` looks through in one go


@bot.command(name="clear")
async def clear_command(ctx):
    """In a direct message with the bot: delete the messages the bot has sent you there, old ones included. Discord doesn't let you
    delete a bot's messages in a DM yourself, but a bot can delete its own. Only what the bot sent goes (your own messages are yours to
    delete), from the most recent CLEAR_LIMIT in the conversation: run it again if some are left."""
    if not _in_dm(ctx):
        try:
            await ctx.send(CLEAR_HINT, delete_after=TEXT_STAYS_SECONDS)
        except discord.HTTPException as exc:
            log.warning("couldn't send a reply in channel %s (%s)", ctx.channel.id, exc.status)
        return
    status = await _member_status(ctx.author.id)
    if status is not True:
        await _react(ctx, "❌")
        await ctx.send(_not_a_member_text(status))
        return
    deleted = failed = 0
    async with ctx.typing():
        async for message in ctx.channel.history(limit=CLEAR_LIMIT):
            if message.author.id != bot.user.id:
                continue
            try:
                await message.delete()
                deleted += 1
            except discord.NotFound:
                pass  # already gone
            except discord.HTTPException:
                failed += 1
    log.info("!clear for %s: deleted %d of the bot's messages (%d failed)", ctx.author.id, deleted, failed)
    await _react(ctx, "❌" if failed else "✅")
    try:
        await ctx.send(f"Deleted {deleted} of my messages here." + (f" I couldn't delete {failed}." if failed else ""), delete_after=30)
    except discord.HTTPException as exc:
        log.warning("couldn't answer a direct message (%s)", exc.status)


@bot.tree.error
async def on_app_command_error(interaction, error):
    """A slash command that fails says so privately, never in the channel."""
    name = getattr(getattr(interaction, "command", None), "name", None) or "a slash command"
    who = getattr(getattr(interaction, "user", None), "id", None)
    log.exception("/%s failed for %s", name, who, exc_info=error)
    await _count(usage_stats.ERROR)
    await _alert_admins(f"error:/{name}:{type(getattr(error, 'original', error)).__name__}", monitoring.error_alert(f"/{name}", error, who),
                        monitoring.ERROR_REPEAT_SECONDS)
    text = "something went wrong, check the logs"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException as exc:
        log.warning("couldn't tell someone that a slash command failed (%s)", exc.status)


_slash = {"synced": False}


async def sync_slash_commands():
    """Register the slash commands in each server that has an allowed channel (a server's commands appear at once; global
    ones can take an hour). Needs the bot to have been invited with the applications.commands scope: without it Discord
    refuses, and the log says how to fix it. Once per run: a reconnect doesn't repeat it."""
    if _slash["synced"]:
        return
    _slash["synced"] = True
    for guild in _home_guilds():
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info("slash commands registered in %s: %s", guild.id, ", ".join(c.name for c in synced) or "none")
        except discord.Forbidden:
            log.warning("couldn't register slash commands in %s: invite the bot again with the applications.commands scope "
                        "(see the README, 'Slash commands')", guild.id)
        except discord.HTTPException:
            log.exception("couldn't register slash commands in %s", guild.id)


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


@bot.before_invoke
async def _count_command(ctx):
    """Count each command that is about to run. Done here and not in on_command, which discord.py fires without waiting: a
    command could then read the counts (as !usage does) before its own use had been counted."""
    await _count(ctx.command.qualified_name, ctx.author.id)


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
        if (getattr(ctx.command, "name", None) in ADMIN_DM_COMMANDS and not _in_dm(ctx) and _is_admin(ctx.author.id)
                and ctx.channel.id in ALLOWED_CHANNEL_IDS):  # an admin who typed a system command in the channel is told where it goes
            try:
                await ctx.send(ADMIN_HINT, delete_after=TEXT_STAYS_SECONDS)
            except discord.HTTPException as exc:
                log.warning("couldn't send a reply in channel %s (%s)", ctx.channel.id, exc.status)
    else:
        name = getattr(ctx.command, "name", None) or "a command"
        log.exception("!%s failed for %s in channel %s", name, ctx.author.id, ctx.channel.id, exc_info=error)
        await _count(usage_stats.ERROR)
        await _alert_admins(f"error:{name}:{type(getattr(error, 'original', error)).__name__}",
                            monitoring.error_alert(f"!{name}", error, ctx.author.id), monitoring.ERROR_REPEAT_SECONDS)
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
