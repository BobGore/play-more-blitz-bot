"""PlayMoreBlitz Discord bot.

Nudges members to play more blitz: monthly rated-blitz game counts per player.
Step 0 skeleton - connection, channel restriction, error handling and a
placeholder help command. The real commands are added in later steps.
"""

import logging
import os

import discord
from discord.ext import commands

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


@bot.check
async def _in_allowed_channel(ctx):
    return ctx.channel.id in ALLOWED_CHANNEL_IDS


@bot.command(name="helpblitzbot")
async def help_blitz_bot(ctx):
    await ctx.send("**PlayMoreBlitz bot** - still under construction. Commands will be listed here.")


async def _reject(ctx, reason):
    await ctx.message.add_reaction("❌")
    await ctx.send(reason)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
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
