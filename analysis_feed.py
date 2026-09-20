"""Feeding the analysis queue from what the bot already fetches.

The refresher and the month-end recount call `feed` with each player's games. If analysis is switched off
(ANALYSIS_ENABLED) it does nothing. Otherwise it puts the games in the queue, and a failure is logged and
swallowed: the totals must never depend on the queue. A game that misses the queue this way is picked up by
the month-end recount, which offers every game of the month again.
"""

import asyncio
import logging

import analysis_queue
import game_records
import settings

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
