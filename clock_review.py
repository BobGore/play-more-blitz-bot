"""How a player spent their time in a game, from the clocks the sites give after every move.

This is the bot's version of the time graph in the owner's chess-journal wiki, with the same rules and thresholds, plus a check of pace
against the opponent. It only reads numbers: the seconds left on the mover's clock after each ply (stored by the worker as `clocks`,
see analysis.pack_clocks), the game's time control, and the flagged moves. Pure functions: no engine, no Discord, no database.

WHERE THE NUMBERS COME FROM
---------------------------
  clocks      `clocks[i]` is the seconds left on the clock of whoever played ply i + 1, just after they moved (so it includes any
              increment they were given). White plays the odd plies, Black the even ones. Chess.com writes these as `[%clk h:mm:ss.d]`
              in the game's PGN, Lichess as a list in centiseconds; game_data.py reads both.
  base, inc   the time control "300+5" is 300 seconds each to start with and 5 seconds added after every move.
  spent       the time a move took: the mover's clock before it (the base for their first move, otherwise their previous clock) minus
              the clock after it, plus the increment they were given, never below zero. That is how long they actually thought.

THE CHECKS (each gives an icon and a sentence; ✓ is good, ⚠ is worth a look, • is just information)
  1 Opening speed           the first 10 moves as a share of the base time: 15% or less is quick, over 25% is slow.
  2 Longest thinks          the three moves that took longest; flagged if one alone used over 10% of the base time; and whether the
                            deepest thinks fell in the middlegame (moves 15 to 25), where games are decided.
  3 Blitzed moves           of the moves after move 12, how many took under 5 seconds: over 40% is flagged.
  4 Time trouble            the first move at which the clock fell below 10% of the base time.
  5 Pace against a strong   your clock at each move against the average fraction of the base time a strong blitz player has left at that
    player                  move (a reference measured on 3+0 and 5+0 games, so only used for those time controls).
  6 Total                   the time you used against your opponent's and against your budget, and what was left at the end.
  7 Pace against opponent   who was ahead on the clock, at moves 10, 20 and 30 and at the end, and whether a lead slipped or was won back
                            (in a game decided on time it says who won on time instead: the clocks are read only after each move, so the
                            last readings can't say whose clock ran out, but the result can)


And two notes for the review's "Interesting" part that set the clock against the engine's verdict on the same move
(`interesting_notes`): your longest think (Nate Solon's "a position where you got stuck") and any mistakes or blunders you played fast.
"""

import re
from dataclasses import dataclass

# --- the rules, as in the wiki's time graph (all tunable) ------------------------------------------------------------------------
OPEN_MOVES = 10  # "the opening" is the first 10 moves
OPEN_GOOD = 0.15  # 15% of the base time or less on the opening: quick
OPEN_BAD = 0.25  # over 25%: slow
BIG_THINK = 0.10  # one move costing over 10% of the base time
FAST_SECONDS = 5  # a move after the opening under this long counts as blitzed
FAST_BAD = 0.40  # over 40% of the post-opening moves blitzed is flagged
POST_OPENING_FROM = OPEN_MOVES + 2  # "after the opening" means after move 12
MIN_POST_OPENING = 5  # fewer such moves than this and the check is skipped
TROUBLE = 0.10  # a clock below 10% of the base time is time trouble
MIDDLEGAME = (15, 25)  # where the deepest thinks belong
PACE_BEHIND = 0.15  # more than this share of the base time behind the reference pace is flagged
BUDGET_LOW = 0.45  # using under this share of the time budget means time was left on the table
LEAD_MARGIN = 0.03  # a clock lead or deficit must be over this share of the base time to count (3% is 9 s of 5 minutes)
LEAD_MOVES = (10, 20, 30)  # the moves at which the clock lead is shown, and the last move

# The reference pace: the average fraction of the base time a strong titled blitz player (one public Chess.com account, July 2026: 94 rated
# games, 83 at 3+0 and 11 at 5+0, no increment) has left when playing move n. The first value is for move 1. From the wiki's time graph.
REFERENCE_PACE = (
    0.9987, 0.9949, 0.9905, 0.9842, 0.9777, 0.9707, 0.9640, 0.9531, 0.9405, 0.9297,
    0.9152, 0.9026, 0.8863, 0.8667, 0.8450, 0.8253, 0.8024, 0.7714, 0.7506, 0.7299,
    0.7042, 0.6807, 0.6553, 0.6333, 0.6101, 0.5875, 0.5615, 0.5419, 0.5216, 0.5067,
    0.4788, 0.4602, 0.4431, 0.4263, 0.4112, 0.3973, 0.3830, 0.3692, 0.3559, 0.3439,
    0.3333, 0.3115, 0.3065, 0.2947, 0.2855, 0.2701, 0.2622, 0.2569, 0.2452, 0.2410,
    0.2358, 0.2321, 0.2198, 0.2136, 0.2159, 0.2127, 0.2064, 0.2012, 0.1931, 0.1761,
)
REFERENCE_TIME_CONTROLS = ((180, 0), (300, 0))  # the (base, increment) pairs the reference was measured on; only these are compared

_TIME_CONTROL = re.compile(r"(\d+)(?:\+(\d+))?")


def parse_time_control(text):
    """(base, increment) in seconds from "300+5" or "180", or None for anything else (a daily game's "1/86400", or nothing)."""
    match = _TIME_CONTROL.fullmatch(str(text or "").strip())
    if not match:
        return None
    base, inc = int(match[1]), int(match[2] or 0)
    return (base, inc) if base > 0 else None


def reference_fraction(n):
    """The reference pace at move n: the fraction of the base time left. Before move 1 it is all of it, and past move 60 it falls a
    little each move (the wiki's own tail), never below 3%."""
    if n < 1:
        return 1.0
    if n <= len(REFERENCE_PACE):
        return REFERENCE_PACE[n - 1]
    return max(0.03, REFERENCE_PACE[-1] - 0.0066 * (n - len(REFERENCE_PACE)))


def fmt_secs(seconds):
    """"45s" or "1m 12s", from a number of seconds."""
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    return f"{minutes}m {sec:02d}s" if minutes else f"{sec}s"


def _signed(seconds):
    """"+1m 05s" or "−8s"."""
    return ("+" if seconds >= 0 else "−") + fmt_secs(abs(seconds))


def label(ply):
    """A ply as a move number: 15 is "8.", 16 is "8..." (the dots mark Black's move)."""
    number = (ply + 1) // 2
    return f"{number}." if ply % 2 else f"{number}..."


@dataclass(frozen=True)
class Move:
    number: int  # the move number in the game (1 for a side's first move)
    ply: int  # the ply it was (1 is White's first move)
    clock: float  # seconds left on this side's clock just after it
    spent: float  # seconds it took


def moves_of(clocks, side, base, inc):
    """Each of `side`'s moves with its clock and how long it took, in order. `clocks` is one entry per ply."""
    first = 1 if side == "white" else 2
    moves, before = [], float(base)
    for ply in range(first, len(clocks) + 1, 2):
        clock = clocks[ply - 1]
        moves.append(Move((ply + 1) // 2, ply, clock, max(0.0, before - clock + inc)))
        before = clock
    return moves


@dataclass(frozen=True)
class Check:
    icon: str  # "✓", "⚠" or "•"
    text: str


def checks(clocks, side, base, inc, on_time=None):
    """The seven checks as a list of Check, or an empty list if there is nothing to say (no moves by the player).

    `on_time` is "won" or "lost" if the game was decided by a flag falling (the player won or lost on time), else None. The clocks are
    only read after each move, so the last reading says nothing about whose clock then ran out; the result does, and check 7 says so."""
    mine = moves_of(clocks, side, base, inc)
    theirs = moves_of(clocks, "black" if side == "white" else "white", base, inc)
    if not mine:
        return []
    out = []

    # 1 opening speed
    opening = mine[:OPEN_MOVES]
    spent = sum(m.spent for m in opening)
    share = spent / base
    icon = "✓" if share <= OPEN_GOOD else "⚠" if share > OPEN_BAD else "•"
    verdict = " — nicely quick" if share <= OPEN_GOOD else " — too slow; know your openings or play them faster" if share > OPEN_BAD else ""
    out.append(Check(icon, f"Opening (your first {len(opening)} moves): {fmt_secs(spent)} = {share * 100:.0f}% of base time{verdict}"))

    # 2 longest thinks, and where they fell
    top = sorted((m for m in mine if m.spent > 0), key=lambda m: -m.spent)[:3]
    if top:
        text = "Longest thinks: " + ", ".join(f"{label(m.ply)} ({fmt_secs(m.spent)})" for m in top)
        big = top[0].spent > base * BIG_THINK
        if big:
            text += f" — {label(top[0].ply)} alone ate {top[0].spent / base * 100:.0f}% of your base time"
        out.append(Check("⚠" if big else "•", text))
        if len(mine) >= MIDDLEGAME[0]:  # a game that never got to move 15 has no middlegame window to judge
            middle = sum(1 for m in top if MIDDLEGAME[0] <= m.number <= MIDDLEGAME[1])
            out.append(Check("✓" if middle >= 2 else "•",
                             "Your deepest thinks fell in the middlegame (moves 15–25), where games are decided: good placement" if middle >= 2
                             else "Best practice: the longest thinks belong in the middlegame (roughly moves 15–25), not the opening or a lost ending"))

    # 3 blitzed moves after the opening
    after = [m for m in mine if m.number > POST_OPENING_FROM]
    if len(after) >= MIN_POST_OPENING:
        fast = [m for m in after if m.spent < FAST_SECONDS]
        many = len(fast) / len(after) > FAST_BAD
        out.append(Check("⚠" if many else "✓", f"{len(fast)} of your {len(after)} post-opening moves took under {FAST_SECONDS}s"
                         + (" — moving too fast in positions that deserve thought is the classic cause of avoidable blunders" if many else "")))

    # 4 time trouble
    trouble = next((m for m in mine if m.clock < base * TROUBLE), None)
    out.append(Check("⚠", f"Time trouble: your clock dropped below {TROUBLE * 100:.0f}% of base at move {trouble.number}") if trouble
               else Check("✓", f"You never hit serious time trouble (under {TROUBLE * 100:.0f}% of base)"))

    # 5 pace against the reference (only for the time controls it was measured on)
    if (base, inc) in REFERENCE_TIME_CONTROLS:
        gaps = [(reference_fraction(k) * base - m.clock, m) for k, m in enumerate(mine, start=1)]
        gap, where = max(gaps, key=lambda pair: pair[0])  # positive: further behind (slower than) the reference
        if gap > base * PACE_BEHIND:
            out.append(Check("⚠", f"Furthest behind a strong player's pace at move {where.number}: {fmt_secs(gap)} ({gap / base * 100:.0f}% of base) "
                                  "more used than their blitz profile at that point"))
        elif gap < 0:
            out.append(Check("✓", "You stayed ahead of a strong player's blitz pace for the whole game: in a slower time control, "
                                  "consider whether you are using enough of your time"))
        else:
            out.append(Check("✓", f"You tracked a strong player's pace closely (worst gap {fmt_secs(gap)} at move {where.number})"))

    # 6 total time used
    used = sum(m.spent for m in mine)
    budget = base + inc * len(mine)
    out.append(Check("•" if used < budget * BUDGET_LOW else "✓",
                     f"Total: you used {fmt_secs(used)} (opponent {fmt_secs(sum(m.spent for m in theirs))}); {fmt_secs(mine[-1].clock)} left at the end"
                     + (" — you used under half your budget; slower, deeper thought was available for free" if used < budget * BUDGET_LOW else "")))

    # 7 pace against the opponent
    out.extend(_against_opponent(mine, theirs, base, on_time))
    return out


def _against_opponent(mine, theirs, base, on_time=None):
    """The clock lead (your clock minus theirs) after each move both have made, shown at moves 10, 20, 30 and the end, and in a sentence:
    a lead that slipped, a deficit won back, or how the game finished."""
    lead = [(a.number, a.clock - b.clock) for a, b in zip(mine, theirs)]  # (move number, seconds) after each move both have made
    if not lead:
        return []
    by_move = dict(lead)
    last_move, final = lead[-1]
    parts = [f"{_signed(by_move[n])} at move {n}" for n in LEAD_MOVES if n in by_move and n < last_move]
    parts.append(f"{_signed(final)} at the end (move {last_move})")
    out = [Check("•", "Clock lead (you minus your opponent): " + ", ".join(parts))]
    margin = base * LEAD_MARGIN
    if on_time == "won":  # the game itself says whose clock ran out, whatever the last readings were
        behind = f", although at the last readings you were {fmt_secs(-final)} behind" if final <= -margin else ""
        return out + [Check("✓", "You won on time: your opponent's clock ran out" + behind)]
    if on_time == "lost":
        ahead = f", although at the last readings you were {fmt_secs(final)} ahead" if final >= margin else ""
        return out + [Check("⚠", "You lost on time: your clock ran out" + ahead)]
    peak_n, peak = max(lead, key=lambda pair: pair[1])
    low_n, low = min(lead, key=lambda pair: pair[1])
    if peak >= margin and final <= -margin:
        out.append(Check("⚠", f"You led on the clock by {fmt_secs(peak)} at move {peak_n} but finished {fmt_secs(-final)} behind: the lead slipped"))
    elif low <= -margin and final >= margin:
        out.append(Check("✓", f"You were {fmt_secs(-low)} behind on the clock at move {low_n} and finished {fmt_secs(final)} ahead"))
    elif final >= margin:
        out.append(Check("✓", f"You finished ahead on the clock by {fmt_secs(final)}"))
    elif final <= -margin:
        out.append(Check("⚠", f"You finished behind on the clock by {fmt_secs(-final)}"))
    else:
        out.append(Check("•", "The clocks finished level"))
    return out


def interesting_notes(clocks, side, base, inc, moments):
    """Notes for the review's Interesting part that set the clock against the engine's verdict on the same move.

    `moments` are the game's flagged moves as analysis.Moment (ply, verdict, lost); they are the engine's word on which moves were slips.
      - YOUR LONGEST THINK, if it took over 10% of the base time: Nate Solon's "a position where you got stuck". Says what the
        engine made of the move: a slip (which kind and how much) or a move that held up.
      - FAST SLIPS: your mistakes and blunders (not inaccuracies) played in under 5 seconds: how many, and the fastest."""
    mine = moves_of(clocks, side, base, inc)
    if not mine:
        return []
    flagged = {m.ply: m for m in moments}
    notes = []
    longest = max(mine, key=lambda m: (m.spent, -m.ply))
    if longest.spent > base * BIG_THINK:
        head = f"Your longest think was {label(longest.ply)} ({fmt_secs(longest.spent)}, {longest.spent / base * 100:.0f}% of your base time)"
        slip = flagged.get(longest.ply)
        notes.append(head + (f": it ended in a {slip.verdict} (−{slip.lost:.0f}%). What were you stuck on?" if slip
                             else ": the move held up, so the time was well spent. What made the position hard?"))
    fast = [m for m in mine if m.spent < FAST_SECONDS and flagged.get(m.ply) and flagged[m.ply].verdict in ("mistake", "blunder")]
    if fast:
        slips = sum(1 for m in mine if flagged.get(m.ply) and flagged[m.ply].verdict in ("mistake", "blunder"))
        quickest = min(fast, key=lambda m: (m.spent, m.ply))
        notes.append(f"You played {len(fast)} of your {slips} mistakes and blunders in under {FAST_SECONDS} seconds (the quickest, {label(quickest.ply)}, "
                     f"{fmt_secs(quickest.spent)}): slowing down at those moments is the cheapest fix.")
    return notes
