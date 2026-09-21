"""How game analysis is shown in Discord: the panel for one game, the analysis part of !mystats, and the admin's
picture of the queue. Pure functions: they take the rows analysis_reports and analysis_queue produce and return text.
"""

from datetime import datetime, timedelta, timezone

import game_records
import render
from openings import opening_family

# The engine's own figures are estimates, not the site's; say so wherever they are shown.
ANALYSIS_TITLE = "**Analysis** (the bot's own, by Stockfish)"


def time_control_label(time_control):
    """"300+5" as "5+5" and "90+1" as "1.5+1"; anything unexpected as it came."""
    try:
        initial, increment = time_control.split("+")
        return f"{int(initial) / 60:g}+{int(increment)}"
    except (AttributeError, ValueError):
        return time_control or ""


def _acc(value):
    return "-" if value is None else f"{int(value + 0.5)}%"


def _count(value):
    return "-" if value is None else str(value)


def _change(value):
    return "-" if value is None else render._signed(value)


def _outcome(row, colour):
    if row["result"] == "draw":
        return "drew"
    return "won" if row["result"] == colour else "lost"


def _table(header, rows):
    """A two-column panel: a label, then one column for each side."""
    label_width = max(len(label) for label, _, _ in rows)
    widths = [max(len(header[i]), *(len(row[i + 1]) for row in rows)) for i in range(2)]
    lines = [f"{'':<{label_width}}  {header[0]:<{widths[0]}}  {header[1]:<{widths[1]}}"]
    lines += [f"{label:<{label_width}}  {a:<{widths[0]}}  {b:<{widths[1]}}" for label, a, b in rows]
    return "\n".join(line.rstrip() for line in lines)


def render_lastgame(username, site, row, waiting=0):
    """The panel for one analysed game (a row of analysis_reports.player_games) from `username`'s side."""
    mine = row["side"]
    theirs = "black" if mine == "white" else "white"

    def both(key, fmt):
        return fmt(row[f"{mine}_{key}"]), fmt(row[f"{theirs}_{key}"])

    rows = [
        ("Result", _outcome(row, mine), _outcome(row, theirs)),
        ("Rating change", _change(row[f"{mine}_rating_change"]), _change(row[f"{theirs}_rating_change"])),
        ("Inaccuracies", *both("inaccuracies", _count)),
        ("Mistakes", *both("mistakes", _count)),
        ("Blunders", *both("blunders", _count)),
        ("Avg centipawn loss", *both("acpl", _count)),
        ("Accuracy", *both("accuracy", _acc)),
        ("  Opening", *both("acc_opening", _acc)),
        ("  Middlegame", *both("acc_middle", _acc)),
        ("  Endgame", *both("acc_end", _acc)),
    ]
    header = (f"{render._name(row[f'{mine}_username'])} ({mine[0].upper()})", f"{render._name(row[f'{theirs}_username'])} ({theirs[0].upper()})")

    when = datetime.fromtimestamp(row["ended_at"], timezone.utc)
    title = [f"`{username}`", render.SITE_NAMES.get(site, site), f"{when.day} {when:%b %Y}", time_control_label(row["time_control"])]
    if row["opening_site"]:
        family = opening_family(row["opening_site"])
        title.append(f"{family} ({row['eco_site']})" if row["eco_site"] else family)
    lines = [" · ".join(t for t in title if t), f"<{game_records.game_url(site, row['game_id'])}>", f"```\n{_table(header, rows)}\n```"]

    lines.append(f"Analysed by {row['engine']} at {row['nodes']:,} nodes a position: the bot's own estimate, not the site's.")
    figures = row[f"site_{mine}_accuracy"], row[f"site_{theirs}_accuracy"]
    if any(f is not None for f in figures):
        note = " (a different method, so the two won't match)" if site == "chess.com" else ""
        lines.append(f"{render.SITE_NAMES.get(site, site)}'s own accuracy: {_acc(figures[0])} / {_acc(figures[1])}{note}")
    if waiting:
        lines.append(f"{waiting} more of {username}'s games {'is' if waiting == 1 else 'are'} waiting to be analysed.")
    return "\n".join(lines)


def analysis_block(s):
    """The lines of the analysis part of !mystats, from an analysis_reports.MonthSummary."""
    other = []
    for count, words in ((s.waiting, "waiting"), (s.skipped, "skipped"), (s.over_limit, "over the monthly limit"), (s.failed, "failed")):
        if count:
            other.append(f"{count} {words}")
    lines = [f"Analysed {s.analysed} of {s.total} games" + (f" ({', '.join(other)})" if other else "")]
    if s.analysed:
        lines.append(f"Accuracy {_acc(s.accuracy)}   Opening {_acc(s.opening)}   Middlegame {_acc(s.middlegame)}   "
                     f"Endgame {_acc(s.endgame)}   Avg centipawn loss {'-' if s.acpl is None else round(s.acpl)}")
        lines.append(f"Per game: {s.inaccuracies:.1f} inaccuracies   {s.mistakes:.1f} mistakes   {s.blunders:.1f} blunders")
    return "\n".join(lines)


def mystats_part(summary):
    """The analysis part of !mystats as a message part, or None if the player has no games in the queue."""
    if summary is None or not summary.total:
        return None
    return f"{ANALYSIS_TITLE}\n```\n{analysis_block(summary)}\n```"


def duration(seconds):
    """A length of time in words, for "last asked for work ... ago" and "waiting ...": "under a minute", "14 min", "2 h 10 min"."""
    if seconds < 60:
        return "under a minute"
    return render.age(timedelta(seconds=seconds)).removesuffix(" ago")


STALE_WORKER_SECONDS = 15 * 60


def render_queue_status(status, enabled):
    """The admin's picture of the queue, from analysis_queue.status()."""
    counts = status["counts"]
    waiting, low = counts["pending"], status["low_priority_pending"]
    lines = [
        f"Waiting         {waiting:>6,}" + (f"   ({low:,} low priority)" if low else ""),
        f"Being analysed  {counts['claimed']:>6,}",
        f"Done            {counts['done']:>6,}",
        f"Skipped         {counts['skipped']:>6,}" + (f"   ({status['over_limit']:,} over the monthly limit)" if status["over_limit"] else ""),
        f"Failed          {counts['failed']:>6,}",
    ]
    if status["oldest_pending_seconds"] is not None:
        lines.append(f"Oldest waiting game: {duration(status['oldest_pending_seconds'])}")
    for name, seconds in status["workers"]:
        lines.append(f"Worker {name}: last asked for work {duration(seconds)} ago")
    text = "**Analysis queue**\n```\n" + "\n".join(lines) + "\n```"
    if not enabled:
        text += "\nAnalysis is switched off (`ANALYSIS_ENABLED`), so no new games are being queued."
    if waiting or counts["claimed"]:
        if not status["workers"]:
            text += "\n⚠ Games are waiting but no worker has ever asked for work."
        elif status["workers"][0][1] > STALE_WORKER_SECONDS:
            text += f"\n⚠ The worker last asked for work {duration(status['workers'][0][1])} ago."
    return text
