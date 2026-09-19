"""Builders for invented games, shared by the tests."""

from datetime import datetime, timezone

import sources


def at(day, hour=12, minute=0, month=9, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def game(result="W", *, when=None, colour="white", rating_after=1500, rating_before=None, opponent="Rival",
         opponent_rating=1500, moves=30, ending=None, opening=None, url="https://example.test/game/1"):
    if ending is None:
        ending = {"W": "resigned", "D": "repetition", "L": "resigned"}[result]
    return sources.Game(
        ended_at=when or at(1),
        colour=colour,
        result=result,
        ending=ending,
        rating_after=rating_after,
        rating_before=rating_before,
        opponent=opponent,
        opponent_rating=opponent_rating,
        moves=moves,
        time_control="300+5",
        opening=opening,
        eco=None,
        url=url,
    )
