"""Feeding the analysis queue from what the bot already fetches.

The refresher and the month-end recount call `feed` with each player's games. If analysis is switched off
(ANALYSIS_ENABLED) it does nothing. Otherwise it puts the games in the queue, and a failure is logged and
swallowed: the totals must never depend on the queue. A game that misses the queue this way is picked up by
the month-end recount, which offers every game of the month again.
"""

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import timezone

import aiohttp

import analysis_queue
import game_records
import settings
import sources
import store

log = logging.getLogger("playmoreblitz.analysis")


async def feed(site, username, games, now):
    """Queue `games` (sources.Game records for `username` on `site`); `now` is an aware datetime. Never raises."""
    if not settings.ANALYSIS_ENABLED or not games:
        return None
    try:
        games = await asyncio.to_thread(with_first_ratings, site, username, games)
        records, unusable = game_records.records(site, username, games)
        if unusable:
            log.warning("%s's %d game(s) on %s have no recognisable game id and can't be analysed", username, unusable, site)
        outcome = await asyncio.to_thread(analysis_queue.queue_games, records, int(now.timestamp()))
        log.info("analysis queue for %s on %s: %s", username, site, dict(outcome))
        return outcome
    except Exception:
        log.exception("could not queue %s's games on %s for analysis", username, site)
        return None


def with_first_ratings(site, username, games):
    """`games` with a rating before filled in where Chess.com's chain leaves it out.

    Chess.com gives only the rating after each game, so sources.month_games takes the rating before from the game
    before it, over the whole month; the month's first game has none. Without it the queue stores no rating and no
    change for the member's side of that game. Fill it from what the bot already holds, with no call to the site:
    the month's official start rating (the one !results uses), or for a month with no results row (a backfilled
    month before registration) the member's last stored game before it. Lichess games, and games that already have
    a rating before, are returned as they are.
    """
    if site != "chess.com":
        return games
    filled = []
    for game in games:
        if game.rating_before is None:
            before = _rating_at(site, username, game.ended_at)
            if before is not None:
                game = replace(game, rating_before=before)
        filled.append(game)
    return filled


def _rating_at(site, username, ended_at):
    """The member's rating going into a month's first game that ended at `ended_at`, or None if not known."""
    month = ended_at.astimezone(timezone.utc).strftime("%Y-%m")
    row = store.month_row(site, username, month)
    if row is not None and row["start_rating"] is not None:
        return row["start_rating"]
    with store.transaction() as conn:
        last = conn.execute(
            "SELECT white_username, white_rating, white_rating_change, black_rating, black_rating_change FROM game_analysis "
            "WHERE site = ? AND (white_username = ? OR black_username = ?) AND ended_at < ? ORDER BY ended_at DESC LIMIT 1",
            (site, username, username, int(ended_at.timestamp()))).fetchone()
    if last is None:
        return None
    side = "white" if last["white_username"].lower() == username.lower() else "black"
    rating, change = last[f"{side}_rating"], last[f"{side}_rating_change"]
    return rating + change if rating is not None and change is not None else None


@dataclass
class Backfill:
    players: int = 0
    outcome: Counter = field(default_factory=Counter)  # what happened to the games (analysis_queue's QUEUED and so on)
    failures: list = field(default_factory=list)  # (username, site, reason) for players whose games could not be fetched or queued


async def backfill(players, month, now):
    """Queue `players`' games for `month`, fetching each player's month in full, one after another. `!queuemonth` passes
    every active player, `!backfillfor` just one.

    For a month whose games the refresher never offered (analysis was off, or the players were added earlier). Games
    already queued are left as they are, so it can be run again safely. Does nothing if analysis is switched off.
    """
    result = Backfill()
    if not settings.ANALYSIS_ENABLED:
        return result
    result.players = len(players)
    async with aiohttp.ClientSession() as session:
        for player in players:
            try:
                games = await sources.month_games(session, player.site, player.username, month)
            except sources.SourceError as exc:
                result.failures.append((player.username, player.site, str(exc)))
                continue
            except Exception:
                log.exception("backfill: could not fetch %s's games on %s", player.username, player.site)
                result.failures.append((player.username, player.site, "unexpected error, see the log"))
                continue
            outcome = await feed(player.site, player.username, games, now)
            if outcome is None and games:
                result.failures.append((player.username, player.site, "could not be queued, see the log"))
            elif outcome:
                result.outcome.update(outcome)
    return result
