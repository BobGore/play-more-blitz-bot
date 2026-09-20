"""Closing a finished month: the authoritative figures, and the next month's rows.

A month is closed only when every active player's whole month has been fetched
successfully, and then all of it is written at once. If any fetch fails, nothing is
written and the failures are reported by name, so a final table is never built from
guessed or partial figures. A closed month's totals are recomputed from the sites in
full, not taken from the running totals the refresher keeps.
"""

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

import analysis_feed
import sources
import store

log = logging.getLogger("playmoreblitz.monthend")


@dataclass(frozen=True)
class Failure:
    site: str
    username: str
    reason: str


@dataclass(frozen=True)
class CloseResult:
    month: str
    ok: bool
    closed: int  # rows closed (0 unless ok)
    failures: tuple  # of Failure; empty if ok


# What the latest attempt to close each month said went wrong, kept for the bot to
# report. It lives in memory only: a restart makes a fresh attempt, which refills it.
last_failures = {}


def _finals(player, row, games):
    counts = Counter(g.result for g in games)
    return {
        "site": player.site,
        "username": player.username,
        "games": len(games),
        "wins": counts["W"],
        "draws": counts["D"],
        "losses": counts["L"],
        "end_rating": games[-1].rating_after if games else row["end_rating"],
        "last_game_at": games[-1].ended_at.isoformat() if games else row["last_game_at"],
    }


async def close_month(session, month, *, now=None):
    """Close `month`, all or nothing. Fetches every active player's whole month one at
    a time; only if none fails, writes the final rows and opens the next month."""
    now = now or datetime.now(timezone.utc)
    players = await asyncio.to_thread(store.open_players, month)
    finals, failures = [], []
    fetched = []  # (player, games), queued for analysis once the month has been closed

    for player in players:
        try:
            row = await asyncio.to_thread(store.month_row, player.site, player.username, month)
            games = await sources.month_games(session, player.site, player.username, month)
            finals.append(_finals(player, row, games))
            fetched.append((player, games))
        except sources.SourceError as exc:
            failures.append(Failure(player.site, player.username, str(exc)))
            log.warning("can't close %s: %s on %s: %s", month, player.username, player.site, exc)
        except Exception:
            log.exception("can't close %s: crash for %s on %s", month, player.username, player.site)
            failures.append(Failure(player.site, player.username, "unexpected error, see the log"))

    if failures:
        last_failures[month] = tuple(failures)
        return CloseResult(month, False, 0, tuple(failures))

    closed = await asyncio.to_thread(store.close_month, month, sources.next_month(month), finals, now.isoformat())
    last_failures.pop(month, None)
    log.info("closed %s for %d players", month, closed)
    for player, games in fetched:  # every game of the month is offered again, so any the refresher missed get queued
        await analysis_feed.feed(player.site, player.username, games, now)
    return CloseResult(month, True, closed, ())


async def close_due_months(*, now=None):
    """Close every finished month that is still open, oldest first, stopping at the first
    that can't be closed (a later month builds on the earlier one's closing ratings).
    Returns the CloseResults of the attempts made."""
    now = now or datetime.now(timezone.utc)
    current = sources.current_month(now)
    results, tried = [], set()
    async with aiohttp.ClientSession() as session:
        while True:
            # Asked again after each close: closing a month opens the next one, which may
            # itself be finished and still open.
            months = [m for m in await asyncio.to_thread(store.unclosed_months, current) if m not in tried]
            if not months:
                break
            tried.add(months[0])
            result = await close_month(session, months[0], now=now)
            results.append(result)
            if not result.ok:
                break
    return results
