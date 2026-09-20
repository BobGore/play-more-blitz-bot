"""A player's games for a month, kept in memory and extended incrementally.

!mystats and !mystatsfull need each game's details (openings, records, splits),
which the bot doesn't store. The first request for a player fetches their month; later
ones fetch only the games after the last one held. Nothing is written to disk, the
number of players held is bounded, and a restart simply starts again.
"""

import asyncio
from collections import OrderedDict, defaultdict

import sources

MAX_PLAYERS = 64  # least recently used players are dropped beyond this

_cache = OrderedDict()  # (site, username lowercased, month) -> tuple of Game
_locks = defaultdict(asyncio.Lock)  # one fetch at a time per player, so a game is never added twice


def clear():
    _cache.clear()
    _locks.clear()


async def month_games(session, site, username, month):
    """All of a player's rated standard blitz games for `month`, oldest first."""
    key = (site, username.lower(), month)
    async with _locks[key]:
        held = _cache.get(key, ())
        new = await sources.month_games(session, site, username, month, after=held[-1].ended_at if held else None)
        games = held + tuple(new)
        _cache[key] = games
        _cache.move_to_end(key)
        while len(_cache) > MAX_PLAYERS:
            evicted, _ = _cache.popitem(last=False)
            _locks.pop(evicted, None)
    return list(games)
