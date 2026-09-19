"""PlayMoreBlitz Discord bot.

Nudges members to play more blitz: monthly rated-blitz game counts per player.
Commands so far: !add, !remove and a help command. The rest come in later steps.
"""

import asyncio
import logging
import os
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

import sources
import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("playmoreblitz")

# Every command only works in one of these channels. A channel ID only ever
# belongs to one server, so this also keeps the bot inert everywhere else it
# might get invited to, and in DMs - no separate guild check needed.
ALLOWED_CHANNEL_IDS = {
    1550558058793533471,  # test
}

# Can !remove any entry, add for others and run !closemonth, and are exempt
# from the cooldown (so testing isn't rate-limited).
ADMIN_USER_IDS = {
    810486671174795274,  # Bob
    315229727629508609,  # Matt
}

# Commands that call the chess sites are limited to one use per user per this
# many seconds, so nobody can hammer the sites through the bot.
COOLDOWN_SECONDS = 600

USAGE = {
    "add": "!add <username> <site>   (site is chess.com or lichess)",
    "remove": "!remove <username> [site]",
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


@bot.event
async def on_ready():
    log.info("connected as %s", bot.user)
    if not sources.CONTACT:
        log.warning("CONTACT is not set: site requests carry no contact address (see .env.example)")


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


async def _reject(ctx, reason, *, refund_cooldown=False):
    """A red cross and a short reason. A refused command doesn't burn the cooldown."""
    await ctx.message.add_reaction("❌")
    await ctx.send(reason)
    # Admins have no cooldown bucket at all (see _cooldown_for), so there is
    # nothing to refund and reset_cooldown would fail on the missing bucket.
    if refund_cooldown and not _is_admin(ctx.author.id):
        ctx.command.reset_cooldown(ctx)


@bot.command(name="helpblitzbot")
async def help_blitz_bot(ctx):
    await ctx.send(
        "**PlayMoreBlitz bot** - still being built.\n"
        f"`{USAGE['add']}`\n"
        "`!remove <username> [site]` - takes a player off the list (whoever added them, or an admin)\n"
        "More commands are coming."
    )


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

    month = sources.current_month()
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            try:
                start = await sources.start_rating(session, site, username, month)
                username = await sources.account_name(session, site, username)  # the site's own spelling
            except sources.SourceError as exc:
                await _reject(ctx, str(exc), refund_cooldown=True)
                return

    outcome = await asyncio.to_thread(store.add_player, site, username, person.id, month, start)
    if outcome == store.EXISTS:  # someone added them while we were on the phone to the site
        await _reject(ctx, f"{username} is already on the list for {site}", refund_cooldown=True)
        return
    log.info("%s %s on %s for %s (start rating %s)", outcome, username, site, person.id, start)
    await ctx.message.add_reaction("✅")


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
        await _reject(ctx, f"'{username}' isn't on the list")
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
    await ctx.message.add_reaction("✅")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.TooManyArguments)):
        usage = USAGE.get(ctx.command.name if ctx.command else "")
        await _reject(ctx, f"Usage: `{usage}`" if usage else "I didn't understand that")
    elif isinstance(error, commands.CommandOnCooldown):
        await _reject(ctx, f"slow down - try again in {error.retry_after:.0f}s")
    elif isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
        pass
    else:
        log.exception("command failed", exc_info=error)
        await _reject(ctx, "something went wrong, check the logs")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set")
    bot.run(token)
