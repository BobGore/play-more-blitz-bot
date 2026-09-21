"""Turn stored results into Discord messages. Pure functions: no network, no Discord.

Tables are fixed-width text inside code blocks, which is the only way to line
columns up in Discord, split so no message goes over the length limit.
"""

from datetime import datetime, timedelta, timezone

import settings

MAX_MESSAGE = 1900  # Discord's limit is 2000; leave room for the code fences.
MAX_NAME = 20  # longer names are cut with an ellipsis so one long name can't widen every row
SITE_CODES = {"chess.com": "CC", "lichess": "LI"}


def age(delta):
    """A short, human "how long ago"."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min ago" if minutes else f"{hours} h ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def month_title(month):
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def _name(username):
    return username if len(username) <= MAX_NAME else username[: MAX_NAME - 1] + "…"


def _flag(row):
    """"!" if the latest refresh failed, "?" if nothing has been counted yet."""
    if row.refresh_error:
        return "!"
    if not row.has_row or row.refreshed_at is None:
        return "?"
    return " "


def _counted(row):
    return row.has_row and row.refreshed_at is not None


def _gain(row):
    if not _counted(row):
        return "-"
    gain = row.end_rating - row.start_rating
    return f"+{gain}" if gain > 0 else str(gain)


def _challenge(row, target):
    if not row.in_100gob:
        return ""
    return "100GOB ✅" if row.games >= target else f"100GOB {row.games}/{target}"


def _sort_key(row):
    return (-row.games, row.username.lower())


def _last_refreshed(rows):
    """The oldest successful refresh across the table: how stale the numbers might be."""
    stamps = [datetime.fromisoformat(r.refreshed_at) for r in rows if r.refreshed_at]
    return min(stamps) if stamps else None


def _next_month_title(month):
    start = datetime.strptime(month, "%Y-%m")
    following = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return following.strftime("%B %Y")


def render_results(rows, month, now, target, signups=(), final=False):
    """The `!results` table for a month as a list of messages, ready to send in order.

    Everyone registered is shown, most games first. A ! before a name means its
    latest refresh failed (the reason is listed underneath); a ? means nothing has
    been counted for it yet. `signups` are the players already signed up for the
    month after this one, listed under the table. With `final` it is the closed
    month's table: titled "final", with "Final results" in place of how fresh it is.
    """
    rows = sorted(rows, key=_sort_key)
    title = f"**{month_title(month)} {'final' if final else 'so far'}**"
    if not rows:
        return [f"{title}\nNobody is registered yet. Use `!add <username> <site>`."]

    name_w = max(len("Player"), *(len(_name(r.username)) for r in rows))
    games = [str(r.games) if _counted(r) else "-" for r in rows]
    record = [f"{r.wins}-{r.draws}-{r.losses}" if _counted(r) else "-" for r in rows]
    ratings = [str(r.end_rating) if _counted(r) else "-" for r in rows]
    gains = [_gain(r) for r in rows]
    games_w = max(len("Gm"), *map(len, games))
    record_w = max(len("W-D-L"), *map(len, record))
    rating_w = max(len("Rating"), *map(len, ratings))
    gain_w = max(len("Gain"), *map(len, gains))
    rank_w = len(str(len(rows)))

    header = (
        f"  {'#':>{rank_w}}  {'Player':<{name_w}}  Site  {'Gm':>{games_w}}  {'W-D-L':>{record_w}}  {'Rating':>{rating_w}}  {'Gain':>{gain_w}}  Challenge"
    )
    rule = "-" * len(header)
    lines = []
    for i, row in enumerate(rows):
        lines.append(
            f"{_flag(row)} {i + 1:>{rank_w}}  {_name(row.username):<{name_w}}  {SITE_CODES.get(row.site, row.site):<4}"
            f"  {games[i]:>{games_w}}  {record[i]:>{record_w}}  {ratings[i]:>{rating_w}}  {gains[i]:>{gain_w}}  {_challenge(row, target)}".rstrip()
        )

    footer = _footer(rows, now, final)
    if signups:
        footer += _signup_lines(_next_month_title(month), signups)
    return _split(title, header, rule, lines, footer)


def _signup_lines(month_title_, names, width=100):
    """"100GOB sign-ups for <month>: a, b, c" wrapped onto several short lines, so the
    footer can be split between messages on a line break without cutting a name."""
    lines, current = [], f"100GOB sign-ups for {month_title_}:"
    for name in names:
        piece = f" `{name}`,"
        if len(current) + len(piece) > width:
            lines.append(current)
            current = "  "
        current += piece
    lines.append(current.rstrip(","))
    return "\n" + "\n".join(lines)


def _footer(rows, now, final=False):
    oldest = _last_refreshed(rows)
    if final:
        parts = ["Final results"]
    else:
        parts = ["Updated " + age(now - oldest) if oldest else "Not updated yet"]
    parts.append("CC = Chess.com, LI = Lichess")
    text = " · ".join(parts)

    # One line per player, marked like their row in the table, so a long list can
    # be split on line breaks without ever cutting a name in half.
    for r in rows:
        if r.refresh_error:
            text += f"\n! `{r.username}`: {r.refresh_error[:80]}"
    for r in rows:
        if not r.refresh_error and not _counted(r):
            text += f"\n? `{r.username}`: not counted yet"
    return text


# --- player summaries: !mystats and !mystatsfull ----------------------------

SITE_NAMES = {"chess.com": "Chess.com", "lichess": "Lichess"}

BAND = settings.SIMILAR_RATING_BAND
OPPONENT_LABELS = {"Higher": f"Higher (>{BAND} above)", "Similar": f"Similar (within {BAND})", "Lower": f"Lower (>{BAND} below)"}
TIME_OF_DAY_LABELS = {"Night": "Night (21-06)", "Morning": "Morning (06-12)", "Afternoon": "Afternoon (12-17)", "Evening": "Evening (17-21)"}


def _pct(score):
    return "-" if score is None else f"{int(score * 100 + 0.5)}%"


def _signed(n):
    return f"+{n}" if n > 0 else str(n)


def _days(n):
    return f"{n} day" if n == 1 else f"{n} days"


def results_block(r):
    """The three-line results block of a player's month."""
    return "\n".join(
        [
            f"Games {r.games}    W {r.wins}   D {r.draws}   L {r.losses}    Score {_pct(r.score)}",
            f"Rating  start {r.start_rating}   end {r.end_rating}   net {_signed(r.end_rating - r.start_rating)}"
            f"   high {r.high}   low {r.low}",
            f"Days played {r.days_played}   Longest play streak {_days(r.longest_play_streak)}"
            f"   Best win streak {r.best_win_streak}   Most games in a day {r.most_games_in_day}",
        ]
    )


def tally_table(rows, header, labels=None):
    """Rows of stats.Tally as fixed-width text: label, games, W, D, L and score."""
    labels = labels or {}
    names = [labels.get(t.label, t.label) for t in rows]
    width = max(len(header), *map(len, names))
    # Each number column is as wide as its own biggest value, so small months stay narrow.
    cols = [max(len(head), *(len(str(getattr(t, field))) for t in rows))
            for head, field in (("G", "games"), ("W", "wins"), ("D", "draws"), ("L", "losses"))]
    lines = [f"{header:<{width}}  " + "  ".join(f"{head:>{w}}" for head, w in zip("GWDL", cols)) + "  Score"]
    for name, t in zip(names, rows):
        numbers = "  ".join(f"{v:>{w}}" for v, w in zip((t.games, t.wins, t.draws, t.losses), cols))
        lines.append(f"{name:<{width}}  {numbers}  {_pct(t.score):>5}")
    return lines


def _table_parts(title, lines):
    """A titled table as one or more messages' worth of parts, the header repeated on each."""
    header, rows = lines[0], lines[1:]
    parts, chunk, size = [], [], 0
    limit = MAX_MESSAGE - len(title) - len(header) - 20
    for row in rows:
        if chunk and size + len(row) + 1 > limit:
            parts.append(f"**{title}**\n```\n{header}\n" + "\n".join(chunk) + "\n```")
            chunk, size = [], 0
        chunk.append(row)
        size += len(row) + 1
    parts.append(f"**{title}**\n```\n{header}\n" + "\n".join(chunk) + "\n```")
    return parts


def _pack(parts):
    """Join parts into as few messages as fit, never splitting a part."""
    messages, current = [], ""
    for part in parts:
        if current and len(current) + 1 + len(part) > MAX_MESSAGE:
            messages.append(current)
            current = ""
        current = f"{current}\n{part}" if current else part
    if current:
        messages.append(current)
    return messages


def _player_title(username, site, month, full):
    return f"`{username}` · {SITE_NAMES.get(site, site)} · {month_title(month)} so far" + (" · full" if full else "")


def verdict_line(v):
    """One line naming the best and worst opening, or saying why it can't yet."""
    if v.eligible == 0:
        return f"No opening has {v.min_games}+ games yet, so no best or worst."
    if v.eligible == 1:
        return f"Only {v.best.label} has {v.min_games}+ games ({_pct(v.best.score)})."
    if v.best.score == v.worst.score:
        return f"Every opening with {v.min_games}+ games scores {_pct(v.best.score)}."
    return (
        f"Best: {v.best.label} {_pct(v.best.score)} ({v.best.games} games) · "
        f"Worst: {v.worst.label} {_pct(v.worst.score)} ({v.worst.games} games)"
    )


def render_mystats(username, site, month, results, openings, verdicts=None, analysis_text=None):
    """The default summary: the results block, then opening tables as White and Black.

    `verdicts` (stats.opening_verdicts) adds a best and worst opening line under each table, and `analysis_text`
    (render_analysis.mystats_part) is put in after the results block.
    """
    parts = [f"{_player_title(username, site, month, False)}\n```\n{results_block(results)}\n```"]
    if analysis_text:
        parts.append(analysis_text)
    if results.games == 0:
        parts.append("No rated blitz games yet this month.")
        return _pack(parts)
    for colour, title in (("white", "As White"), ("black", "As Black")):
        rows = openings[colour]
        if rows:
            parts += _table_parts(title, tally_table(rows, "Opening"))
            if verdicts:
                parts[-1] += "\n" + verdict_line(verdicts[colour])  # stays with the table's last part
        else:
            parts.append(f"**{title}**\nNo games.")
    return _pack(parts)


def records_block(rec):
    def line(label, e, tag=""):
        return f"{label:<19}" + ("-" if e is None else f"{e.rating}  {e.opponent}{tag}")

    def mate(label, moves):
        return f"{label:<19}" + ("-" if moves is None else f"{moves} moves")

    return "\n".join(
        [
            line("Best win", rec.best_win),
            line("Worst loss", rec.worst_loss),
            line("Strongest opponent", rec.strongest_opponent, f" ({rec.strongest_opponent.result})" if rec.strongest_opponent else ""),
            line("Weakest opponent", rec.weakest_opponent, f" ({rec.weakest_opponent.result})" if rec.weakest_opponent else ""),
            mate("Quickest mate won", rec.quickest_mate_won),
            mate("Quickest mate lost", rec.quickest_mate_lost),
        ]
    )


def render_mystatsfull(username, site, month, results, records, splits):
    """The full summary: records, then the splits by opponent rating, colour, weekday and time of day."""
    parts = [f"{_player_title(username, site, month, True)}\n```\n{records_block(records)}\n```"]
    if results.games == 0:
        parts.append("No rated blitz games yet this month.")
        return _pack(parts)
    for title, rows, header, labels in (
        ("By opponent rating", splits.by_opponent_rating, "Opponent", OPPONENT_LABELS),
        ("By colour", splits.by_colour, "Colour", None),
        ("By weekday", splits.by_weekday, "Day", None),
        ("By time of day (UTC)", splits.by_time_of_day, "Time", TIME_OF_DAY_LABELS),
    ):
        if rows:
            parts += _table_parts(title, tally_table(rows, header, labels))
    return _pack(parts)


def _split(title, header, rule, lines, footer):
    """Pack the rows into code blocks that fit a message, repeating the header on each."""
    fence_cost = len("```\n") * 2 + len(header) + len(rule) + 2
    messages, block, size, first = [], [], 0, True

    def flush():
        nonlocal block, size, first
        heading = f"{title}\n" if first else ""
        messages.append(heading + "```\n" + header + "\n" + rule + "\n" + "\n".join(block) + "\n```")
        block, size, first = [], 0, False

    for line in lines:
        room = MAX_MESSAGE - fence_cost - (len(title) + 1 if first else 0)
        if block and size + len(line) + 1 > room:
            flush()
        block.append(line)
        size += len(line) + 1
    flush()

    if len(messages[-1]) + len(footer) + 1 <= MAX_MESSAGE:
        messages[-1] += "\n" + footer
    else:
        messages.extend(_fit(footer))
    return messages


def _fit(text):
    """Cut text into pieces that each fit a message, on line breaks where possible."""
    pieces, current = [], ""
    for line in text.split("\n"):
        while len(line) > MAX_MESSAGE:  # one enormous line (a very long list of names)
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:MAX_MESSAGE])
            line = line[MAX_MESSAGE:]
        if current and len(current) + 1 + len(line) > MAX_MESSAGE:
            pieces.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        pieces.append(current)
    return pieces


fit = _fit  # for callers outside this module that need to cut a long text into messages
