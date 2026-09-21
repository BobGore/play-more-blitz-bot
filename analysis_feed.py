"""Feeding the analysis queue from what the bot already fetches.

The refresher and the month-end recount call `feed` with each player's games. If analysis is switched off
(ANALYSIS_ENABLED) it does nothing. Otherwise it puts the games in the queue, and a failure is logged and
swallowed: the totals must never depend on the queue. A game that misses the queue this way is picked up by
the month-end recount, which offers every game of the month again.
"""

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field

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
        records, unusable = game_records.records(site, username, games)
        if unusable:
            log.warning("%s's %d game(s) on %s have no recognisable game id and can't be analysed", username, unusable, site)
        outcome = await asyncio.to_thread(analysis_queue.queue_games, records, int(now.timestamp()))
        log.info("analysis queue for %s on %s: %s", username, site, dict(outcome))
        return outcome
    except Exception:
        log.exception("could not queue %s's games on %s for analysis", username, site)
        return None


@dataclass
class Backfill:
    players: int = 0
    outcome: Counter = field(default_factory=Counter)  # what happened to the games (analysis_queue's QUEUED and so on)
    failures: list = field(default_factory=list)  # (username, site, reason) for players whose games could not be fetched or queued


async def backfill(month, now):
    """Queue every active player's games for `month`, fetching each player's month in full, one after another.

    For a month whose games the refresher never offered (analysis was off, or the players were added earlier). Games
    already queued are left as they are, so it can be run again safely. Does nothing if analysis is switched off.
    """
    result = Backfill()
    if not settings.ANALYSIS_ENABLED:
        return result
    players = await asyncio.to_thread(store.active_players)
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
