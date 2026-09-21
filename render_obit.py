"""The OBIT review of one game, as text: Openings, Blunders, Interesting, Takeaway.

OBIT is Nate Solon's method for reviewing blitz games. This is the bot's version of it, built only from what the
analysis holds (the evaluation after every move and the flagged moves; never the moves themselves), so it says
where and how much, and leaves what to the player. Pure functions: a row of the analysis table in, messages out.
"""

from datetime import datetime, timezone

import analysis
import game_records
import render
import render_analysis
from openings import opening_family

CLEARLY = 75  # winning chances, in %, at or above which a side is clearly winning (about +3 pawns); 100 minus it, clearly worse
NOT_WORSE = 45  # at or above this a side is equal or better (about -0.55 pawns)
MAX_MOMENTS = 5  # how many of the player's own flagged moves are listed

_VERDICT_ORDER = {"blunder": 0, "mistake": 1, "inaccuracy": 2}


def move_label(ply):
    """Ply 1 is "1.", ply 2 is "1...", ply 45 is "23.": the move number, with dots for Black's move."""
    number = (ply + 1) // 2
    return f"{number}." if ply % 2 else f"{number}..."


def _win(score, side):
    """The player's winning chances in %, from an engine score (White's point of view)."""
    white = analysis.win_percent(analysis.as_cp(score))
    return white if side == "white" else 100 - white


def _eval(score, side):
    """The score from the player's point of view, in pawns ("+0.4", "-2.3"), or "+M3" for a mate in three."""
    kind, value = score
    value = value if side == "white" else -value
    if kind == "mate":
        return "#" if value == 0 else f"{'+' if value > 0 else '-'}M{abs(value)}"
    return f"{value / 100:+.1f}"


def _plural(count, noun, plural=None):
    return f"{count} {noun if count == 1 else plural or noun + 's'}"


def _counts(row, side):
    parts = [_plural(row[f"{side}_blunders"], "blunder"), _plural(row[f"{side}_mistakes"], "mistake"),
             _plural(row[f"{side}_inaccuracies"], "inaccuracy", "inaccuracies")]
    return " · ".join(parts)


def _scores(row):
    try:
        return analysis.unpack_evals(row["evals"]) if row["evals"] else []
    except ValueError:
        return []


def _before(scores, ply):
    return scores[ply - 2] if ply >= 2 else ("cp", analysis.INITIAL_CP)


def _mine(moment, side):
    return (moment.ply % 2 == 1) == (side == "white")


def _opening_part(row, side):
    family = opening_family(row["opening_site"])
    name = f"{family} ({row['eco_site']})" if row["eco_site"] else family
    lines = [f"**O · Opening** — {name}"]
    phases = [(word, row[f"{side}_acc_{key}"]) for word, key in (("opening", "opening"), ("middlegame", "middle"), ("endgame", "end"))]
    known = [(word, value) for word, value in phases if value is not None]
    line = f"Accuracy {render_analysis._acc(row[f'{side}_accuracy'])}: " + ", ".join(f"{word} {render_analysis._acc(value)}" for word, value in phases)
    if len(known) >= 2 and max(v for _, v in known) - min(v for _, v in known) >= 8:
        line += f". Your weakest phase here was the {min(known, key=lambda p: p[1])[0]}"
    lines.append(line)
    if row["eval_ply20"] is not None:
        lines.append(f"After 10 moves each the engine had you at {_eval(('cp', row['eval_ply20']), side)}.")
    return "\n".join(lines) + "\n"


def _blunder_part(row, side, moments, scores, site):
    theirs = "black" if side == "white" else "white"
    lines = [f"**B · Blunders** — {_counts(row, side)}   (them: {_counts(row, theirs)})"]
    mine = sorted((m for m in moments if _mine(m, side)), key=lambda m: (-m.lost, m.ply))[:MAX_MOMENTS]
    if not any(row[f"{side}_{k}"] for k in ("blunders", "mistakes", "inaccuracies")):
        lines.append("No inaccuracies, mistakes or blunders by you. A clean game.")
    elif mine:
        lines.append("Your worst moments, biggest first (winning chances lost, and the engine's score before and after):")
        for m in mine:
            text = f"• {move_label(m.ply)} {m.verdict}, −{m.lost:.0f}%"
            if m.ply <= len(scores):
                text += f" ({_eval(_before(scores, m.ply), side)} → {_eval(scores[m.ply - 1], side)})"
            if site == "lichess":
                text += f"  <{game_records.game_url(site, row['game_id'], m.ply - 1)}>"
            lines.append(text)
        if site == "lichess":
            lines.append("Each link opens the position before the move.")
    return "\n".join(lines) + "\n"


def _interesting(row, side, moments, scores):
    """The things that stand out, as sentences; empty if nothing does. At most three can hold at once: a win thrown away
    and a lost position saved exclude each other unless the game was drawn, and the clock story needs a decisive result."""
    found = []
    outcome = render_analysis._outcome(row, side)
    if scores and row["ending"] == "timeout":
        final = _win(scores[-1], side)
        if outcome == "lost" and final >= NOT_WORSE:
            found.append(f"You lost on time in a position the engine had at {_eval(scores[-1], side)} for you: equal or better. "
                         "The clock, not the position, decided this one.")
        elif outcome == "won" and final < NOT_WORSE:
            found.append(f"You won on time from a position the engine had at {_eval(scores[-1], side)} for you: your opponent's "
                         "clock did the work.")
    if scores:
        curve = [_win(s, side) for s in scores]
        peak = max(range(len(curve)), key=lambda i: curve[i])
        trough = min(range(len(curve)), key=lambda i: curve[i])
        if outcome != "won" and curve[peak] >= CLEARLY:
            found.append(f"You were clearly winning ({_eval(scores[peak], side)} after {move_label(peak + 1)}) and {'drew' if outcome == 'drew' else 'lost'}."
                         " Working out where it slipped is the most useful thing in this game.")
        if outcome != "lost" and curve[trough] <= 100 - CLEARLY:
            found.append(f"You were in real trouble ({_eval(scores[trough], side)} after {move_label(trough + 1)}) and "
                         f"{'drew' if outcome == 'drew' else 'won'} anyway. What did you do to make it hard for them?")
    gifts = [m for m in moments if not _mine(m, side) and m.verdict in ("mistake", "blunder")]
    if gifts:
        biggest = max(gifts, key=lambda m: m.lost)
        found.append(f"Your opponent made {_plural(len(gifts), 'mistake or blunder', 'mistakes or blunders')}; the biggest, "
                     f"{move_label(biggest.ply)}, cost them {biggest.lost:.0f}%. Did you see it, and use it?")
    return found


def render_obit(username, site, row, side):
    """The messages for the OBIT of the analysed game `row` (a game_analysis row as a dict) from `side`'s point of view."""
    theirs = "black" if side == "white" else "white"
    outcome = render_analysis._outcome(row, side)
    when = datetime.fromtimestamp(row["ended_at"], timezone.utc)
    head = [f"**OBIT** · `{username}` · {render.SITE_NAMES.get(site, site)} · {when.day} {when:%b %Y}", render_analysis.time_control_label(row["time_control"])]
    opponent = render._name(row[f"{theirs}_username"])
    rating = row[f"{theirs}_rating"]
    versus = f"{opponent} ({rating})" if rating else opponent
    ending = f" by {row['ending']}" if row["ending"] and outcome != "drew" else (f" ({row['ending']})" if row["ending"] else "")
    title = " · ".join(t for t in head if t)
    title += f"\nYou {outcome}{ending} as {side.capitalize()} against {versus}\n<{game_records.game_url(site, row['game_id'])}>\n"

    moments = analysis.moments_from_json(row["moments"])
    scores = _scores(row)
    parts = [title, _opening_part(row, side), _blunder_part(row, side, moments, scores, site)]
    interesting = _interesting(row, side, moments, scores)
    parts.append("**I · Interesting**\n" + ("\n".join(f"• {line}" for line in interesting) if interesting else "Nothing unusual: a steady game.") + "\n")
    parts.append("**T · Takeaway** — over to you: what is the one thing you'll do differently next time?\n")
    parts.append(f"Analysed by {row['engine']} at {row['nodes']:,} nodes a position: the bot's own estimate, so treat it as a guide. "
                 "This review was sent to you alone.")
    return render._pack(parts)
