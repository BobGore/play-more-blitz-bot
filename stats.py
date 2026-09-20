"""Numbers from a month of games.

Pure functions over sources.Game records: no network, no Discord, no database.
Turning these numbers into tables is render.py's job.

Times are UTC, matching how the months are cut.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta, timezone

import openings
import settings

MIN_OPENING_GAMES = settings.MIN_OPENING_GAMES  # fewer than this and an opening goes into "All others"
OTHERS = "All others"
BAND = settings.SIMILAR_RATING_BAND  # rating points either side that still count as a similar opponent

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Tally:
    """Wins, draws and losses under one label (an opening, a colour, a weekday...)."""

    label: str
    games: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0

    @property
    def score(self):
        """Wins plus half the draws, as a share of games. None with no games."""
        return (self.wins + self.draws / 2) / self.games if self.games else None


def _tally(label, games):
    counts = Counter(g.result for g in games)
    return Tally(label, len(games), counts["W"], counts["D"], counts["L"])


def _chronological(games):
    return sorted(games, key=lambda g: g.ended_at)


def _day(game):
    return game.ended_at.astimezone(timezone.utc).date()


def _pre_ratings(games, start_rating):
    """The rating before each game. Chess.com's first game of a month has none, so
    it is the start rating; every later one chains from the game before."""
    ratings, previous = [], start_rating
    for game in games:
        ratings.append(game.rating_before if game.rating_before is not None else previous)
        previous = game.rating_after
    return ratings


# --- the results block -----------------------------------------------------


@dataclass(frozen=True)
class Results:
    games: int
    wins: int
    draws: int
    losses: int
    score: float | None
    start_rating: int
    end_rating: int
    high: int  # highest and lowest rating held, the start rating included
    low: int
    days_played: int
    longest_play_streak: int  # consecutive days with at least one game
    best_win_streak: int  # consecutive wins; a draw or a loss ends it
    most_games_in_day: int
    busiest_day: date | None  # the earliest one if several tie


def summarise(games, start_rating):
    games = _chronological(games)
    tally = _tally("", games)
    ratings = [start_rating] + [g.rating_after for g in games]

    per_day = Counter(_day(g) for g in games)
    days = sorted(per_day)
    longest_play, run = 0, 0
    for i, day in enumerate(days):
        run = run + 1 if i and day - days[i - 1] == timedelta(days=1) else 1
        longest_play = max(longest_play, run)

    best_wins, wins_now = 0, 0
    for g in games:
        wins_now = wins_now + 1 if g.result == "W" else 0
        best_wins = max(best_wins, wins_now)

    busiest = min(days, key=lambda d: (-per_day[d], d)) if days else None

    return Results(
        games=tally.games,
        wins=tally.wins,
        draws=tally.draws,
        losses=tally.losses,
        score=tally.score,
        start_rating=start_rating,
        end_rating=ratings[-1],
        high=max(ratings),
        low=min(ratings),
        days_played=len(days),
        longest_play_streak=longest_play,
        best_win_streak=best_wins,
        most_games_in_day=per_day[busiest] if busiest else 0,
        busiest_day=busiest,
    )


# --- openings --------------------------------------------------------------


def opening_tables(games, min_games=MIN_OPENING_GAMES):
    """{"white": [...], "black": [...]}: a Tally per opening family, most played first.

    Families with fewer than min_games games are folded into one "All others" row,
    last, so a month of one-off openings stays readable.
    """
    tables = {}
    for colour in ("white", "black"):
        by_family = {}
        for g in games:
            if g.colour == colour:
                by_family.setdefault(openings.opening_family(g.opening), []).append(g)

        rows = [_tally(name, group) for name, group in by_family.items() if len(group) >= min_games]
        rows.sort(key=lambda t: (-t.games, -t.score, t.label))

        rest = [g for name, group in by_family.items() if len(group) < min_games for g in group]
        if rest:
            rows.append(_tally(OTHERS, rest))
        tables[colour] = rows
    return tables


MIN_BEST_WORST_GAMES = settings.MIN_BEST_WORST_GAMES  # an opening needs this many games before it can be called best or worst


@dataclass(frozen=True)
class OpeningVerdict:
    """The best and worst opening in one colour's table, among those with enough games."""

    best: Tally | None
    worst: Tally | None
    eligible: int  # how many openings had enough games to be judged
    min_games: int


def opening_verdict(rows, min_games=MIN_BEST_WORST_GAMES):
    """Best and worst by score. The "All others" lump and "Unknown" are not openings, so
    they are never judged. Ties go to the opening with more games (the stronger evidence),
    then to the name, so the answer never depends on input order."""
    eligible = [t for t in rows if t.label not in (OTHERS, openings.UNKNOWN) and t.games >= min_games]
    if not eligible:
        return OpeningVerdict(None, None, 0, min_games)
    best = min(eligible, key=lambda t: (-t.score, -t.games, t.label))
    worst = min(eligible, key=lambda t: (t.score, -t.games, t.label))
    return OpeningVerdict(best, worst, len(eligible), min_games)


def opening_verdicts(tables, min_games=MIN_BEST_WORST_GAMES):
    """{"white": OpeningVerdict, "black": OpeningVerdict} from opening_tables()."""
    return {colour: opening_verdict(rows, min_games) for colour, rows in tables.items()}


# --- records ---------------------------------------------------------------


@dataclass(frozen=True)
class Encounter:
    opponent: str
    rating: int
    result: str  # "W", "D" or "L"
    url: str


@dataclass(frozen=True)
class Records:
    best_win: Encounter | None  # the highest-rated opponent beaten
    worst_loss: Encounter | None  # the lowest-rated opponent lost to
    strongest_opponent: Encounter | None
    weakest_opponent: Encounter | None
    quickest_mate_won: int | None  # full moves
    quickest_mate_lost: int | None


def _encounter(game):
    return Encounter(game.opponent, game.opponent_rating, game.result, game.url)


def records(games):
    games = _chronological(games)
    rated = [g for g in games if g.opponent_rating is not None]
    wins = [g for g in rated if g.result == "W"]
    losses = [g for g in rated if g.result == "L"]

    def pick(pool, key):
        return _encounter(pool[max(range(len(pool)), key=lambda i: (key(pool[i]), -i))]) if pool else None

    mates_won = [g.moves for g in games if g.result == "W" and g.ending == "checkmated"]
    mates_lost = [g.moves for g in games if g.result == "L" and g.ending == "checkmated"]

    return Records(
        best_win=pick(wins, lambda g: g.opponent_rating),
        worst_loss=pick(losses, lambda g: -g.opponent_rating),
        strongest_opponent=pick(rated, lambda g: g.opponent_rating),
        weakest_opponent=pick(rated, lambda g: -g.opponent_rating),
        quickest_mate_won=min(mates_won, default=None),
        quickest_mate_lost=min(mates_lost, default=None),
    )


# --- splits ----------------------------------------------------------------


@dataclass(frozen=True)
class Splits:
    by_opponent_rating: list  # Higher, Similar, Lower
    by_colour: list  # White, Black
    by_weekday: list  # Mon .. Sun
    by_time_of_day: list  # Morning, Afternoon, Evening, Night


def _time_of_day(game):
    hour = game.ended_at.astimezone(timezone.utc).hour
    if hour < 6 or hour >= 21:
        return "Night"
    if hour < 12:
        return "Morning"
    if hour < 17:
        return "Afternoon"
    return "Evening"


def _split(games, key, order):
    """One Tally per label in `order` that has any games."""
    groups = {}
    for g in games:
        groups.setdefault(key(g), []).append(g)
    return [_tally(label, groups[label]) for label in order if label in groups]


def splits(games, start_rating):
    games = _chronological(games)
    pre = dict(zip(map(id, games), _pre_ratings(games, start_rating)))

    def band(game):
        gap = game.opponent_rating - pre[id(game)]
        return "Higher" if gap > BAND else "Lower" if gap < -BAND else "Similar"

    rated = [g for g in games if g.opponent_rating is not None]
    return Splits(
        by_opponent_rating=_split(rated, band, ("Higher", "Similar", "Lower")),
        by_colour=_split(games, lambda g: g.colour.capitalize(), ("White", "Black")),
        by_weekday=_split(games, lambda g: WEEKDAYS[_day(g).weekday()], WEEKDAYS),
        by_time_of_day=_split(games, _time_of_day, ("Morning", "Afternoon", "Evening", "Night")),
    )
