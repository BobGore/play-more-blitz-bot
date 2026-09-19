"""Turn stored results into Discord messages. Pure functions: no network, no Discord.

Tables are fixed-width text inside code blocks, which is the only way to line
columns up in Discord, split so no message goes over the length limit.
"""

from datetime import datetime, timedelta, timezone

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


def render_results(rows, month, now, target, signups=()):
    """The `!results` table for a month as a list of messages, ready to send in order.

    Everyone registered is shown, most games first. A ! before a name means its
    latest refresh failed (the reason is listed underneath); a ? means nothing has
    been counted for it yet. `signups` are the players already signed up for the
    month after this one, listed under the table.
    """
    rows = sorted(rows, key=_sort_key)
    title = f"**{month_title(month)} so far**"
    if not rows:
        return [f"{title}\nNobody is registered yet. Use `!add <username> <site>`."]

    name_w = max(len("Player"), *(len(_name(r.username)) for r in rows))
    games = [str(r.games) if _counted(r) else "-" for r in rows]
    record = [f"{r.wins}-{r.draws}-{r.losses}" if _counted(r) else "-" for r in rows]
    gains = [_gain(r) for r in rows]
    games_w = max(len("Gm"), *map(len, games))
    record_w = max(len("W-D-L"), *map(len, record))
    gain_w = max(len("Gain"), *map(len, gains))
    rank_w = len(str(len(rows)))

    header = (
        f"  {'#':>{rank_w}}  {'Player':<{name_w}}  Site  {'Gm':>{games_w}}  {'W-D-L':>{record_w}}  {'Gain':>{gain_w}}  Challenge"
    )
    rule = "-" * len(header)
    lines = []
    for i, row in enumerate(rows):
        lines.append(
            f"{_flag(row)} {i + 1:>{rank_w}}  {_name(row.username):<{name_w}}  {SITE_CODES.get(row.site, row.site):<4}"
            f"  {games[i]:>{games_w}}  {record[i]:>{record_w}}  {gains[i]:>{gain_w}}  {_challenge(row, target)}".rstrip()
        )

    footer = _footer(rows, now)
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


def _footer(rows, now):
    oldest = _last_refreshed(rows)
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
