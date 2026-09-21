"""How the bot is used, as counts: per day, how many times each command ran and how many different people used it.

Counts only: no message text and no game data, and nothing about who did what, beyond a list of which Discord IDs were seen on
a day (to count people). Days are UTC. Anything older than KEEP_DAYS is dropped. `!usage` (an admin's, in a DM) shows it.
"""

import time
from collections import Counter
from datetime import datetime, timezone

import store

KEEP_DAYS = 35
ERROR, OBIT_SENT, EXPORT_SENT = "error", "obit_sent", "export_sent"
NOT_COMMANDS = (ERROR, OBIT_SENT, EXPORT_SENT)  # counters that aren't a command being run


def _day(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def count(name, user_id=None, now=None):
    """Note one use of `name` (a command's name, or one of ERROR, OBIT_SENT, EXPORT_SENT) and, if given, that `user_id` was seen."""
    day = _day(time.time() if now is None else now)
    with store.transaction() as conn:
        conn.execute("INSERT INTO usage_daily (day, name, n) VALUES (?, ?, 1) ON CONFLICT (day, name) DO UPDATE SET n = n + 1", (day, name))
        if user_id is not None:
            conn.execute("INSERT OR IGNORE INTO usage_seen (day, user_id) VALUES (?, ?)", (day, user_id))


def prune(now=None):
    """Drop what is older than KEEP_DAYS."""
    cutoff = _day((time.time() if now is None else now) - KEEP_DAYS * 86400)
    with store.transaction() as conn:
        conn.execute("DELETE FROM usage_daily WHERE day < ?", (cutoff,))
        conn.execute("DELETE FROM usage_seen WHERE day < ?", (cutoff,))


def report(days, now=None):
    """The last `days` days (today included) as {"days": [{day, commands, people, errors}, newest first], "totals": {name: n}}."""
    now = time.time() if now is None else now
    wanted = [_day(now - i * 86400) for i in range(days)]
    with store.transaction() as conn:
        rows = conn.execute("SELECT day, name, n FROM usage_daily WHERE day >= ?", (wanted[-1],)).fetchall()
        seen = {r["day"]: r["n"] for r in conn.execute("SELECT day, COUNT(*) AS n FROM usage_seen WHERE day >= ? GROUP BY day", (wanted[-1],))}
    commands, errors, totals = Counter(), Counter(), Counter()
    for row in rows:
        totals[row["name"]] += row["n"]
        if row["name"] == ERROR:
            errors[row["day"]] += row["n"]
        elif row["name"] not in NOT_COMMANDS:
            commands[row["day"]] += row["n"]
    return {"days": [{"day": d, "commands": commands[d], "people": seen.get(d, 0), "errors": errors[d]} for d in wanted], "totals": dict(totals)}


def render_report(data):
    """The report as a message: a line per day, then the most used commands and the sends."""
    days = data["days"]
    lines = [f"{'Day':<10}  {'Commands':>8}  {'People':>6}  {'Errors':>6}"]
    lines += [f"{d['day']:<10}  {d['commands']:>8}  {d['people']:>6}  {d['errors']:>6}" for d in days]
    totals = data["totals"]
    used = sorted(((n, name) for name, n in totals.items() if name not in NOT_COMMANDS), key=lambda t: (-t[0], t[1]))[:8]
    popular = " · ".join(f"{name} {n}" for n, name in used) or "none yet"
    return (f"**Usage, last {len(days)} day{'s' if len(days) != 1 else ''}** (UTC days; counts only)\n```\n" + "\n".join(lines) + "\n```\n"
            f"Most used: {popular}\n"
            f"Reviews sent: {totals.get(OBIT_SENT, 0)} · Exports sent: {totals.get(EXPORT_SENT, 0)} · Errors: {totals.get(ERROR, 0)}")
