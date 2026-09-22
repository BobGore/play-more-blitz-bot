"""The OBIT review of one game, as text: Openings, Blunders, Interesting, Takeaway.

OBIT is Nate Solon's method for reviewing blitz games. This is the bot's version of it, built only from what the
analysis holds (the evaluation after every move and the flagged moves; never the moves themselves), so it says
where and how much, and leaves what to the player. Pure functions: a row of the analysis table in, messages out.

WHERE EVERY NUMBER COMES FROM
-----------------------------
Nothing here is computed by an engine at review time. The EliteDesk worker analysed the game earlier (analysis.py
turns its evaluations into figures) and stored them in one row of the `game_analysis` table. This file only reads that
row and words it. The parts of the row it uses:

  scores      the engine's evaluation after every ply, from White's point of view: ("cp", centipawns) or ("mate", n).
              `scores[i]` is the position after ply i + 1, so scores[0] is after White's first move. A ply is one
              move by one side; ply 1 is White's 1st move, ply 2 is Black's 1st move, ply 21 is White's 11th.
  moments     the moves called an inaccuracy, mistake or blunder: (ply, verdict, lost), where `lost` is how many points
              of winning chance the move gave away. Both sides are in the one list; odd plies are White's.
  counts      the row's `<side>_inaccuracies / _mistakes / _blunders` (each side's totals; equal to the moments).
  accuracy    the row's `<side>_accuracy` overall and `_acc_opening / _acc_middle / _acc_end` by phase (0-100).
  eval_ply20  the evaluation after ply 20 (10 moves each), centipawns, White's view.
  the game    result, how it ended, time control, the site's own opening name and ECO code, both usernames and ratings.
  clocks      the seconds left on the mover's clock after every ply, when the site gave them (Chess.com and Lichess live games do;
              a daily game or a game analysed before the clocks were kept has none). They give the Time section and two notes under
              Interesting; all the clock rules are in clock_review.py.

TWO SCALES, used together
-------------------------
  Pawns / centipawns: what the engine says. +1.0 (100 cp) means the player is a pawn's worth better.
  Winning chances (%): the same evaluation squeezed onto a 0-100 scale (analysis.win_percent), which is how "how
  much did that move cost" and "was I clearly winning" are decided. The conversion is not straight-line: a pawn matters
  more near equality than when already far ahead.

      evaluation   0.0   +0.5   +1.0   +2.0   +3.0   +4.0        and for the other side, subtract from 100
      win chance   50%    55%    59%    68%    75%    81%

The review is written from the point of view of the player asked about (`side`): scores are flipped for Black.
"""

from datetime import datetime, timezone

import analysis
import clock_review
import game_records
import render
import render_analysis
from openings import opening_family

# --- the few numbers that decide what counts as "interesting" -----------------------------------------------------
CLEARLY = 75  # winning chance, in %, at or above which a side is "clearly winning" (about +3.0 pawns); 100 minus it is "clearly worse" (25%)
NOT_WORSE = 45  # at or above this winning chance a side is "equal or better" (the score is no worse than about -0.55 pawns)
LEVEL = 55  # from NOT_WORSE up to this winning chance a position is "level": within about half a pawn either way
TURNING = 10  # points of winning chance one move must cost to be the game's turning point (a mistake or worse, not an inaccuracy)
MAX_MOMENTS = 5  # how many of the player's own flagged moves are listed under Blunders


def move_label(ply):
    """A ply as a chess move number: ply 1 is "1.", ply 2 is "1...", ply 45 is "23.", ply 46 is "23...". The dots mark Black's move."""
    number = (ply + 1) // 2  # two plies to a move number
    return f"{number}." if ply % 2 else f"{number}..."


def _win(score, side):
    """The player's winning chance in %, from an engine score (which is White's point of view).

    analysis.win_percent turns centipawns into White's chance; a forced mate counts as the cap (analysis.as_cp), so a
    mate is 'as good as it gets' rather than infinity. Black's chance is what White's leaves out of 100."""
    white = analysis.win_percent(analysis.as_cp(score))
    return white if side == "white" else 100 - white


def _eval(score, side):
    """A score written from the player's point of view, in pawns to a tenth: "+0.4" is better for them, "-2.3" worse.
    A mate is "+M3" (they mate in 3) or "-M3" (they are mated in 3); "#" is checkmate already on the board."""
    kind, value = score
    value = value if side == "white" else -value  # the stored score is White's view: flip it for Black
    if kind == "mate":
        return "#" if value == 0 else f"{'+' if value > 0 else '-'}M{abs(value)}"
    return f"{value / 100:+.1f}"  # centipawns to pawns


def _plural(count, noun, plural=None):
    return f"{count} {noun if count == 1 else plural or noun + 's'}"


def _counts(row, side):
    """"1 blunder · 2 mistakes · 5 inaccuracies" from the side's own totals in the row."""
    parts = [_plural(row[f"{side}_blunders"], "blunder"), _plural(row[f"{side}_mistakes"], "mistake"),
             _plural(row[f"{side}_inaccuracies"], "inaccuracy", "inaccuracies")]
    return " · ".join(parts)


def _scores(row):
    """The game's evaluations after every ply, unpacked from the row (they are stored two bytes a ply). Empty if the row has
    none or they are damaged, so the parts that need them are simply left out rather than the whole review failing."""
    try:
        return analysis.unpack_evals(row["evals"]) if row["evals"] else []
    except ValueError:
        return []


def _before(scores, ply):
    """The evaluation just before a ply was played, which is the evaluation after the ply before it. For the very first ply it is the
    starting position, which the method counts as +0.15 (analysis.INITIAL_CP, as Lichess does)."""
    return scores[ply - 2] if ply >= 2 else ("cp", analysis.INITIAL_CP)


def _swing(moment, side, scores):
    """" (+0.8 → -1.7)": what a flagged move did to the engine's score, from `side`'s point of view, or "" if the scores aren't there."""
    if moment.ply > len(scores):
        return ""
    return f" ({_eval(_before(scores, moment.ply), side)} → {_eval(scores[moment.ply - 1], side)})"


def _mine(moment, side):
    """True if the flagged move was made by `side`: White plays the odd plies, Black the even ones."""
    return (moment.ply % 2 == 1) == (side == "white")


def _clock_data(row):
    """(clocks, base, increment) for the game if it has usable clocks, else None: the site must have given a clock for every ply, and
    the time control must be "base+increment" in seconds (a daily game's isn't)."""
    blob = row.get("clocks")
    control = clock_review.parse_time_control(row["time_control"])
    if not blob or control is None:
        return None
    try:
        clocks = analysis.unpack_clocks(blob)
    except ValueError:
        return None
    return clocks, control[0], control[1]


def _time_part(row, side):
    """The Time section, the wiki's time-graph checks worded for this game, or "" if the game has no clocks. One line per check, each
    with an icon: ✓ good, ⚠ worth a look, • information. What each check means is in clock_review.py."""
    data = _clock_data(row)
    outcome = render_analysis._outcome(row, side)
    on_time = outcome if row["ending"] == "timeout" and outcome in ("won", "lost") else None  # a game decided by a flag falling
    found = clock_review.checks(data[0], side, data[1], data[2], on_time=on_time) if data else []
    if not found:
        return ""
    head = f"**Time** — {render_analysis.time_control_label(row['time_control'])}, from the clocks after every move"
    return head + "\n" + "\n".join(f"{c.icon} {c.text}" for c in found) + "\n"


def _clock_notes(row, side, moments):
    """The two Interesting notes that set the clock against the engine's verdict: your longest think, and mistakes played fast."""
    data = _clock_data(row)
    return clock_review.interesting_notes(data[0], side, data[1], data[2], moments) if data else []


def _opening_part(row, side):
    """O, Openings: what was played, how accurately each phase went, and where the engine had the player once the opening was over.

    - The name and ECO code are the site's own (opening_family tidies the two sites' different spellings into one family name).
    - Accuracy overall and per phase are the worker's figures for the player's own moves (analysis.py: a 0-100 score from how much
      winning chance each move gave away). A phase the game never reached (a short game has no endgame) is shown as "-".
    - "Weakest phase" is only said when at least two phases are known and the best and worst are 8 or more points apart:
      closer than that is noise, not a weakness.
    - "After 10 moves each" is the evaluation after ply 20, from the player's side: a plain read on whether the opening worked.
    """
    family = opening_family(row["opening_site"])
    name = f"{family} ({row['eco_site']})" if row["eco_site"] else family
    lines = [f"**O · Opening** — {name}"]
    phases = [(word, row[f"{side}_acc_{key}"]) for word, key in (("opening", "opening"), ("middlegame", "middle"), ("endgame", "end"))]
    known = [(word, value) for word, value in phases if value is not None]
    line = f"Accuracy {render_analysis._acc(row[f'{side}_accuracy'])}: " + ", ".join(f"{word} {render_analysis._acc(value)}" for word, value in phases)
    if len(known) >= 2 and max(v for _, v in known) - min(v for _, v in known) >= 8:
        line += f". Your weakest phase here was the {min(known, key=lambda p: p[1])[0]}"
    lines.append(line)
    if row["eval_ply20"] is not None:  # a game shorter than 10 moves each has no such position
        lines.append(f"After 10 moves each the engine had you at {_eval(('cp', row['eval_ply20']), side)}.")
    return "\n".join(lines) + "\n"


def _blunder_part(row, side, moments, scores, site):
    """B, Blunders: how many slips each side made, and the player's worst moments.

    - The counts are the row's totals for each side (inaccuracy, mistake, blunder), yours first, the opponent's in brackets.
    - Each listed moment is one of the player's own flagged moves, biggest first (ties: earliest first), at most MAX_MOMENTS:
        "• 8. blunder, −25% (+0.8 → -1.7)"  =  move 8, a blunder, it gave away 25 points of winning chance, and the engine's
        score went from +0.8 (before the move) to -1.7 (after it), both from the player's side.
      What the move actually was is not known (moves aren't stored): only where, how big, and what it did to the position.
    - On Lichess each line links to the position *before* the move (ply - 1), so the player sees the board they were deciding on.
      Chess.com's link format doesn't reliably open at a move, so it gets the game link at the top instead.
    - No flagged moves at all is a clean game and says so.
    """
    theirs = "black" if side == "white" else "white"
    lines = [f"**B · Blunders** — {_counts(row, side)}   (them: {_counts(row, theirs)})"]
    mine = sorted((m for m in moments if _mine(m, side)), key=lambda m: (-m.lost, m.ply))[:MAX_MOMENTS]
    if not any(row[f"{side}_{k}"] for k in ("blunders", "mistakes", "inaccuracies")):
        lines.append("No inaccuracies, mistakes or blunders by you. A clean game.")
    elif mine:
        lines.append("Your worst moments, biggest first (winning chances lost, and the engine's score before and after):")
        for m in mine:
            text = f"• {move_label(m.ply)} {m.verdict}, −{m.lost:.0f}%"
            if m.ply <= len(scores):  # the scores may be missing or short; then the line is just without them
                text += f" ({_eval(_before(scores, m.ply), side)} → {_eval(scores[m.ply - 1], side)})"
            if site == "lichess":
                text += f"  <{game_records.game_url(site, row['game_id'], m.ply - 1)}>"
            lines.append(text)
        if site == "lichess":
            lines.append("Each link opens the position before the move.")
    return "\n".join(lines) + "\n"


def _interesting(row, side, moments, scores):
    """I, Interesting: the things about this game worth stopping at, each a sentence, or none.

    The moves aren't stored, so "interesting" here means what the evaluation curve and the flagged moves show without
    knowing the moves. There are four tests. Every one works on the player's winning chance at each ply (`curve`): the
    engine's score after every ply turned into a 0-100 chance, from the player's side, so 50 is equal, above 50 is better.

      1 THE CLOCK.  Only when the game ended on time. There is no clock data, so this is a proxy: what the position was worth at
        the moment the flag fell.
          - You LOST on time while the position was equal or better for you (chance 45% or more, about -0.55 pawns or
            better): the clock decided it, not the position.
          - You WON on time while the position was worse for you (under 45%): your opponent's clock did the work.
          - You WON on time in a LEVEL position (45% up to 55%, about -0.55 to +0.5 pawns): nothing on the board decided it, the
            clock did. Unusual, so it is noted. (Won on time when clearly better needs no note: the board had already decided it.)
      2 A WIN THROWN AWAY.  You didn't win (lost or drew), yet at your peak your chance was 75% or more (about +3.0 pawns):
        you were clearly winning. Names the move where the peak was, and says working out where it slipped is the lesson.
      3 A LOST POSITION SAVED.  You didn't lose (won or drew), yet at your low your chance was 25% or less (about -3.0 pawns):
        you were in real trouble and survived. Names the move of the low point.
      4 CHANCES THE OPPONENT GAVE YOU.  The opponent's flagged moves that were mistakes or blunders (not mere inaccuracies):
        how many, and the biggest (the move, how much it cost them and what it did to the engine's score from your side).
        Whether you used it can't be known from one move, so it asks; test 6 looks at the next move.
      5 THE TURNING POINT.  The single move that changed the evaluation most, whoever played it, provided it cost 10 or more
        points of winning chance (a mistake or worse). If it was the opponent's, test 4 already names it (as their biggest
        chance to you), so this note only appears when it was YOUR move: "the biggest swing of the game was your own 11.".
      6 A CHANCE GIVEN BACK.  An opponent's mistake or blunder answered on the very next move by one of YOUR flagged moves: the
        chance was there and you gave it back at once. Names the pair (the biggest reply if several) and how many times it happened.

    So a game can show up to five notes (tests 2 and 3 exclude each other unless it was drawn, and test 1 needs a decisive
    result). If none holds the review says "Nothing unusual: a steady game." That is a fair answer for an ordinary game.
    """
    found = []
    outcome = render_analysis._outcome(row, side)  # "won", "lost" or "drew", for this player
    # test 1: the clock. `scores[-1]` is the position after the last move, the one on the board when the game ended.
    if scores and row["ending"] == "timeout":
        final = _win(scores[-1], side)
        if outcome == "lost" and final >= NOT_WORSE:
            found.append(f"You lost on time in a position the engine had at {_eval(scores[-1], side)} for you: equal or better. "
                         "The clock, not the position, decided this one.")
        elif outcome == "won" and final < NOT_WORSE:
            found.append(f"You won on time from a position the engine had at {_eval(scores[-1], side)} for you: your opponent's "
                         "clock did the work.")
        elif outcome == "won" and final < LEVEL:  # level, and still won on time: nothing on the board decided it
            found.append(f"You won on time in a level position (the engine had it at {_eval(scores[-1], side)} for you): the clock, "
                         "not the board, decided this one.")
    if scores:
        curve = [_win(s, side) for s in scores]  # the player's winning chance after every ply
        peak = max(range(len(curve)), key=lambda i: curve[i])  # the ply where it was highest (the first, if tied)
        trough = min(range(len(curve)), key=lambda i: curve[i])  # and lowest
        # test 2: a win thrown away
        if outcome != "won" and curve[peak] >= CLEARLY:
            found.append(f"You were clearly winning ({_eval(scores[peak], side)} after {move_label(peak + 1)}) and {'drew' if outcome == 'drew' else 'lost'}."
                         " Working out where it slipped is the most useful thing in this game.")
        # test 3: a lost position saved
        if outcome != "lost" and curve[trough] <= 100 - CLEARLY:
            found.append(f"You were in real trouble ({_eval(scores[trough], side)} after {move_label(trough + 1)}) and "
                         f"{'drew' if outcome == 'drew' else 'won'} anyway. What did you do to make it hard for them?")
    # test 4: the opponent's mistakes and blunders (the flagged moves that are not yours, leaving out the small inaccuracies)
    gifts = [m for m in moments if not _mine(m, side) and m.verdict in ("mistake", "blunder")]
    if gifts:
        biggest = max(gifts, key=lambda m: m.lost)
        found.append(f"Your opponent made {_plural(len(gifts), 'mistake or blunder', 'mistakes or blunders')}; the biggest, "
                     f"{move_label(biggest.ply)}, cost them {biggest.lost:.0f}%{_swing(biggest, side, scores)}. Did you see it, and use it?")
    # test 5: the turning point, when it was your own move (an opponent's is the biggest gift above)
    swings = [m for m in moments if m.lost >= TURNING]
    if swings:
        turning = max(swings, key=lambda m: (m.lost, -m.ply))  # the biggest; the earliest if two are equal
        if _mine(turning, side):
            found.append(f"The biggest swing of the game was your own {move_label(turning.ply)}: it cost you {turning.lost:.0f}%"
                         f"{_swing(turning, side, scores)}.")
    # test 6: a chance given back. The move right after an opponent's mistake or blunder is always yours (plies alternate).
    flagged = {m.ply: m for m in moments}
    pairs = [(gift, flagged[gift.ply + 1]) for gift in gifts if gift.ply + 1 in flagged]
    if pairs:
        gift, reply = max(pairs, key=lambda pair: (pair[1].lost, -pair[0].ply))  # the costliest reply; the earliest if equal
        text = (f"Your opponent's {gift.verdict} on {move_label(gift.ply)} (−{gift.lost:.0f}%) was answered by your own {reply.verdict} "
                f"on {move_label(reply.ply)} (−{reply.lost:.0f}%): the chance was given back straight away.")
        found.append(text + (f" It happened {len(pairs)} times." if len(pairs) > 1 else ""))
    return found


def render_obit(username, site, row, side):
    """The messages for the OBIT of the analysed game `row` (a game_analysis row as a dict) from `side`'s point of view.

    Put together in this order: the heading (who, where, when, time control, and the result in words, with a link to the game),
    O, B, the Time section (only when the game has clocks), I (the engine-based notes, then the clock-based ones), then T (a prompt only: the takeaway is the player's to write; a tick-list is planned) and a note that the figures are
    the bot's own estimate. Long reviews are split into several messages, never in the middle of a part (render._pack)."""
    theirs = "black" if side == "white" else "white"
    outcome = render_analysis._outcome(row, side)
    when = datetime.fromtimestamp(row["ended_at"], timezone.utc)  # the game's end, UTC
    head = [f"**OBIT** · `{username}` · {render.SITE_NAMES.get(site, site)} · {when.day} {when:%b %Y}", render_analysis.time_control_label(row["time_control"])]
    opponent = render._name(row[f"{theirs}_username"])  # a very long name is shortened
    rating = row[f"{theirs}_rating"]  # the opponent's rating going into the game, if the site said
    versus = f"{opponent} ({rating})" if rating else opponent
    # "won by timeout" / "lost by resignation"; a draw reads "drew (repetition)"
    ending = f" by {row['ending']}" if row["ending"] and outcome != "drew" else (f" ({row['ending']})" if row["ending"] else "")
    title = " · ".join(t for t in head if t)
    title += f"\nYou {outcome}{ending} as {side.capitalize()} against {versus}\n<{game_records.game_url(site, row['game_id'])}>\n"

    moments = analysis.moments_from_json(row["moments"])  # both sides' flagged moves; empty for a game analysed before they were kept
    scores = _scores(row)
    parts = [title, _opening_part(row, side), _blunder_part(row, side, moments, scores, site)]
    time_part = _time_part(row, side)  # only when the site gave the clocks
    if time_part:
        parts.append(time_part)
    interesting = _interesting(row, side, moments, scores) + _clock_notes(row, side, moments)
    parts.append("**I · Interesting**\n" + ("\n".join(f"• {line}" for line in interesting) if interesting else "Nothing unusual: a steady game.") + "\n")
    parts.append("**T · Takeaway** — over to you: what is the one thing you'll do differently next time?\n")
    parts.append(f"Analysed by {row['engine']} at {row['nodes']:,} nodes a position: the bot's own estimate, so treat it as a guide. "
                 "This review was sent to you alone.")
    return render._pack(parts)
