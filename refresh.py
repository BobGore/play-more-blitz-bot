"""Keep each player's running monthly totals up to date, incrementally.

A refresh fetches only the games that ended after the last one already counted
(the watermark, monthly_results.last_game_at), adds them to the totals, and moves
the watermark on, all in one database step. A failed refresh changes nothing but
the recorded error, so a table is never left half updated or guessed at.
"""

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone

import aiohttp

import analysis_feed
import sources
import store

log = logging.getLogger("playmoreblitz.refresh")

UPDATED = "updated"  # new games were counted
UNCHANGED = "unchanged"  # fetched fine; nothing new
SKIPPED = "skipped"  # no open row for the month, or another refresh got there first
FAILED = "failed"  # the fetch failed; the reason is recorded on the row

# One refresh at a time per player, so two requests can't fetch and count the same games.
_locks = defaultdict(asyncio.Lock)


def _parse(stamp):
    return datetime.fromisoformat(stamp) if stamp else None


async def refresh_player(session, player, month, *, now=None):
    """Bring one player's totals for `month` up to date. Returns one of the outcomes above."""
    now = now or datetime.now(timezone.utc)
    async with _locks[(player.site, player.username.lower())]:
        row = await asyncio.to_thread(store.month_row, player.site, player.username, month)
        if row is None or row["closed_at"]:
            return SKIPPED

        watermark = row["last_game_at"]
        try:
            games = await sources.month_games(session, player.site, player.username, month, after=_parse(watermark))
        except sources.SourceError as exc:
            await asyncio.to_thread(store.record_refresh_error, player.site, player.username, month, str(exc))
            log.warning("refresh failed for %s on %s: %s", player.username, player.site, exc)
            return FAILED
        except Exception:
            log.exception("refresh crashed for %s on %s", player.username, player.site)
            await asyncio.to_thread(
                store.record_refresh_error, player.site, player.username, month, "unexpected error, see the log"
            )
            return FAILED

        counts = Counter(g.result for g in games)
        applied = await asyncio.to_thread(
            lambda: store.apply_refresh(
                player.site,
                player.username,
                month,
                expected_watermark=watermark,
                games=len(games),
                wins=counts["W"],
                draws=counts["D"],
                losses=counts["L"],
                end_rating=games[-1].rating_after if games else row["end_rating"],
                last_game_at=games[-1].ended_at.isoformat() if games else watermark,
                now=now.isoformat(),
            )
        )
        if not applied:
            return SKIPPED
        await analysis_feed.feed(player.site, player.username, games, now)  # after the totals: it can never spoil them
        return UPDATED if games else UNCHANGED


async def refresh_all(month, *, now=None):
    """Refresh every active player, one after another. Returns {outcome: how many}."""
    players = await asyncio.to_thread(store.active_players)
    outcomes = Counter()
    async with aiohttp.ClientSession() as session:
        for player in players:
            outcomes[await refresh_player(session, player, month, now=now)] += 1
    return dict(outcomes)


async def refresh_one(site, username, month=None):
    """Refresh a single player (used right after !add, so they show up promptly)."""
    player = await asyncio.to_thread(store.get_player, site, username)
    if player is None or not player.active:
        return SKIPPED
    async with aiohttp.ClientSession() as session:
        return await refresh_player(session, player, month or sources.current_month())
