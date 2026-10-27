"""Make one member's monthly blunder map: a self-contained HTML page (template.html with the data inlined).

Reads, never writes: the bot's database (games, clocks, evaluations, flagged moves, phase boundaries) and one or more
pass 2 results files (good-move counts for every position, the flashcard record for every blunder).

    python blundermap.py --botdb BOT.db --counts OCT.db [--counts MORE.db] --site lichess --user NAME --month 2026-10 --out map.html

With --summary, one line of JSON on standard output says what the page holds (the bot reads it to word its message).
The bot runs this as a separate low-priority process (blundermap_build.py), so building a big month never holds it up.

Members see plain words only. Behind the scenes each comparison with the member's opponents uses a 95% range from
resampling the member's games (BOOTSTRAP times); the range decides the words (see `verdict`).
"""

import argparse
import calendar
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

import chess

import analysis

CLOCK_BANDS = [0, 10, 25, 50, 75]  # clock left, % of the starting time: band i is CLOCK_BANDS[i] to the next (or 100)
GOOD_COLS = ["1", "2", "3", "4–5", "6–10", "11+"]
HIGH_CLOCK, FEW_GOOD = 25, 2  # the quadrant lines: clock 25%+ is "plenty", 1-2 good moves is "few"
QUADS = ["Critical moment", "Careless", "Sharp + short of time", "Time-trouble slip"]
PH_NAMES = {"opening": "Opening", "middle": "Middlegame", "end": "Endgame"}
BOOTSTRAP = 1000
MIN_OPP_BLUNDERS, MIN_EXPECTED = 5, 3  # below either, there is not enough to compare at all
SAME_LO, SAME_HI = 0.75, 1.33          # a range inside this counts as "about the same"
LEAN_TAIL = 0.10                       # the "Probably ..." words use the 80% range (10% cut from each end)
SEEM_TAIL = 0.30                       # the "Seems ..." words use the 40% range (about 70% likely that way)
NEEDS_MORE = ", but it needs more games to be sure"
MIN_GAMES, FULL_GAMES = 10, 20  # tested 26 Oct by drawing 5-40 games from big months: under 10 the words mislead,
                                # 10-19 they claim a step too much, from 20 each word is right about as often as it says


def good_col(g):
    return 0 if g == 1 else 1 if g == 2 else 2 if g == 3 else 3 if g <= 5 else 4 if g <= 10 else 5


def clock_band(pct):
    return max(i for i, lo in enumerate(CLOCK_BANDS) if pct >= lo)


def quad(pct, good):
    return (0 if good <= FEW_GOOD else 1) if pct >= HIGH_CLOCK else (2 if good <= FEW_GOOD else 3)


def phase(ply, middle, end, n):
    """As analysis.summarise: the phase of the move made at `ply` (1-based)."""
    first_middle = middle if middle is not None else n + 1
    endgame_from = end if (end is not None and middle is not None) else n + 1
    return "opening" if ply < first_middle else "middle" if ply < endgame_from else "end"


def verdict(per_game, rng):
    """Plain words for one comparison. `per_game` = [[mb, mn, ob, on], ...]: the member's blunders and moves and the
    opponents' blunders and moves there, one entry per game of the month. Returns (code, words, calc); code is worse/
    better/same/seems-worse/seems-better/unclear, calc is the ratio and its ranges as text for the run's printout (never
    shown to members). Three steps, each tried only if the one before can't decide: the 95% range (plain words, with a
    size word from its cautious end), the 80% range ("Probably ...") and the 40% range ("Seems ..., but it needs more
    games to be sure"); after those, "No clear difference yet". Under MIN_GAMES games: "Not enough games to tell yet";
    under FULL_GAMES the words move down one step (resampling so few games makes the ranges too narrow). Too few
    blunders in this section: "Too few blunders here to compare yet"."""
    k = len(per_game)
    if k < MIN_GAMES:
        return "unclear", "Not enough games to tell yet", f"only {k} games"
    mb, mn, ob, on = (sum(x[i] for x in per_game) for i in range(4))
    if not mn or not on or ob < MIN_OPP_BLUNDERS or mn * ob / on < MIN_EXPECTED:
        return "unclear", "Too few blunders here to compare yet", f"too few blunders to compare (you {mb}, them {ob})"
    ratio = (mb / mn) / (ob / on)
    samples = []
    for _ in range(BOOTSTRAP):
        s = [0, 0, 0, 0]
        for _ in range(k):
            x = per_game[rng.randrange(k)]
            s[0] += x[0]; s[1] += x[1]; s[2] += x[2]; s[3] += x[3]
        if s[1] and s[2] and s[3]:
            samples.append((s[0] / s[1]) / (s[2] / s[3]))
    if len(samples) < 0.9 * BOOTSTRAP:
        return "unclear", "Too few blunders here to compare yet", f"ratio {ratio:.2f}, too few usable resamples"
    samples.sort()
    below = sum(s < 1 for s in samples) / len(samples)
    calc = f"ratio {ratio:.2f} ({below:.0%} likely less)"
    # step 0 = 95% range, 1 = 80% ("Probably"), 2 = 40% ("Seems"); the first range that decides gives the direction
    step, way, lo95 = None, None, samples[int(0.025 * len(samples))]
    hi95 = samples[int(0.975 * len(samples)) - 1]
    for i, (tail, name) in enumerate(((0.025, "95%"), (LEAN_TAIL, "80%"), (SEEM_TAIL, "40%"))):
        lo, hi = samples[int(tail * len(samples))], samples[int((1 - tail) * len(samples)) - 1]
        calc += f", {name} range {lo:.2f}-{hi:.2f}"
        same = lo >= SAME_LO and hi <= SAME_HI
        # a 95% range inside the "same" band is a practical tie even if it misses 1 (a big month makes 0.97 certain)
        way = "same" if i == 0 and same else "worse" if lo > 1 else "better" if hi < 1 else "same" if same else None
        if way:
            step = i
            break
    if step is None:
        return "unclear", "No clear difference yet", calc
    if k < FULL_GAMES and step < 2:
        step += 1
        calc += f" (moved down a step: only {k} games)"
    if step == 0:
        words = {"worse": "More than twice as often as your opponents" if lo95 >= 2.5 else
                          "About twice as often as your opponents" if lo95 >= 1.7 else "More often than your opponents",
                 "better": "Less than half as often as your opponents" if hi95 <= 0.4 else
                           "About half as often as your opponents" if hi95 <= 0.6 else "Less often than your opponents",
                 "same": "About the same as your opponents"}[way]
        return way, words, calc
    plain = {"worse": "more often than your opponents", "better": "less often than your opponents",
             "same": "about the same as your opponents"}[way]
    if step == 1:
        return way, "Probably " + plain, calc
    return ("same" if way == "same" else "seems-" + way), "Seems " + plain + NEEDS_MORE, calc


def one_in(b, n):
    """'1 in 23' style rate, or 'none' / '' when there is nothing to say."""
    if not n:
        return ""
    return "none" if not b else f"1 in {max(1, round(n / b))}"


def load(botdb, counts_paths, site, user, month):
    db = sqlite3.connect(f"file:{botdb}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    games = db.execute("""SELECT * FROM game_analysis WHERE site = ? AND month = ? AND status IN ('done', 'skipped')
                          AND (white_username = ? COLLATE NOCASE OR black_username = ? COLLATE NOCASE)
                          ORDER BY ended_at""", (site, month, user, user)).fetchall()
    counts, blunders = {}, {}
    for path in counts_paths:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for gid, legal, good in c.execute("SELECT game_id, legal, good FROM game_counts WHERE site = ? AND month = ?", (site, month)):
            counts[gid] = (legal, good)
        for row in c.execute("""SELECT game_id, ply, fen, played_move, best_move, good_list, close_list, legal_moves, good_moves
                                FROM blunder_moves WHERE site = ?""", (site,)):
            blunders[(row[0], row[1])] = row
    return games, counts, blunders


def build(games, counts, blunders, site, user, month, rng):
    done = [g for g in games if g["status"] == "done"]
    skipped = len(games) - len(done)
    used, no_clock, mismatch = [], 0, 0
    for g in done:
        if g["game_id"] not in counts:
            continue
        if g["clocks"] is None or not (g["time_control"] or "").split("+")[0].isdigit():
            no_clock += 1
            continue
        used.append(g)

    moves = []    # one per ply of the games used: (game index, member?, clock %, good, blunder?, phase)
    mine = []
    for gi, g in enumerate(used):
        legal, good = counts[g["game_id"]]
        clocks, evals = analysis.unpack_clocks(g["clocks"]), analysis.unpack_evals(g["evals"]) if g["evals"] else []
        n = g["plies"]
        if len(good) != n or len(clocks) != n:
            mismatch += 1
            continue
        base = int(g["time_control"].split("+")[0])
        me_white = g["white_username"].lower() == user.lower()
        flagged = {p: lost for p, code, lost in json.loads(g["moments"] or "[]") if code == "b"}
        ended = time.gmtime(g["ended_at"])
        for p in range(1, n + 1):
            white = p % 2 == 1
            secs = base if p <= 2 else clocks[p - 3]
            pct = min(100.0, 100 * secs / base)
            ph = phase(p, g["middle_ply"], g["end_ply"], n)
            member = white == me_white
            moves.append((gi, member, pct, good[p - 1], p in flagged, ph))
            if member and p in flagged and (g["game_id"], p) in blunders:
                mine.append(card(g, p, blunders[(g["game_id"], p)], secs, pct, ph, evals, ended, me_white))

    def tally(pick):
        """Per game [mb, mn, ob, on] for the moves `pick` accepts."""
        per = {}
        for gi, member, pct, gd, bl, ph in moves:
            if pick(pct, gd, ph):
                x = per.setdefault(gi, [0, 0, 0, 0])
                i = 0 if member else 2
                x[i] += bl
                x[i + 1] += 1
        # games with no such moves still count in a resample: they add nothing, but they are part of the month
        return [per.get(i, [0, 0, 0, 0]) for i in range(len(used))]

    def compare(pick):
        per = tally(pick)
        mb, mn, ob, on = (sum(x[i] for x in per) for i in range(4))
        code, words, calc = verdict(per, rng) if per else ("unclear", "Not enough games to tell yet", "no games")
        return {"mb": mb, "mn": mn, "ob": ob, "on": on, "code": code, "words": words, "calc": calc,
                "you": one_in(mb, mn), "opp": one_in(ob, on)}

    cells = []
    for gc in range(len(GOOD_COLS)):
        for cb in range(len(CLOCK_BANDS)):
            c = compare(lambda pct, gd, ph, gc=gc, cb=cb: good_col(gd) == gc and clock_band(pct) == cb)
            if c["mn"] or c["on"]:
                cells.append({"g": gc, "c": cb, **c})
    quads = [{"name": QUADS[q], **compare(lambda pct, gd, ph, q=q: quad(pct, gd) == q)} for q in range(4)]
    phases = {ph: compare(lambda pct, gd, p, ph=ph: p == ph) for ph in ("opening", "middle", "end")}
    overall = compare(lambda pct, gd, ph: True)
    low = {"you": sum(1 for m in moves if m[1] and m[2] < 10), "opp": sum(1 for m in moves if not m[1] and m[2] < 10)}

    y, mo = map(int, month.split("-"))
    return {
        "site": site, "user": user, "month": month, "monthName": f"{calendar.month_name[mo]} {y}",
        "monthOpen": month >= time.strftime("%Y-%m", time.gmtime()),  # the month isn't over: say "so far"
        "games": len(games), "analysed": len(done), "skipped": skipped, "used": len(used),
        "waiting": len([g for g in done if g["game_id"] not in counts]), "noClock": no_clock, "mismatch": mismatch,
        "cells": cells, "quads": quads, "phases": phases, "overall": overall,
        "lowClock": {"you": low["you"], "youMoves": overall["mn"], "opp": low["opp"], "oppMoves": overall["on"]},
        "mine": mine,
    }


def card(g, p, rec, secs, pct, ph, evals, ended, me_white):
    """One member blunder: what the dot, the hover board and the flashcard need."""
    _, _, fen, played, best, good_list, close_list, legal_n, good_n = rec
    board = chess.Board(fen)
    lg = {m.uci(): board.san(m) for m in board.legal_moves}
    white = p % 2 == 1

    def mover_win(i):  # mover's win % after ply i (0 = the start)
        score = evals[i - 1] if i else ("cp", analysis.INITIAL_CP)
        w = analysis.win_percent(analysis.as_cp(score))
        return w if white else 100 - w

    wb, wa = (round(mover_win(p - 1), 1), round(mover_win(p), 1)) if len(evals) >= p else (None, None)
    base, inc = g["time_control"].split("+")
    if g["site"] == "lichess":
        link = f"https://lichess.org/{g['game_id']}{'' if me_white else '/black'}#{p - 1}"
    else:
        link = f"https://www.chess.com/game/{g['game_id']}?move={p - 1}"  # game_id is "live/123..."; ?move=N = after N half-moves
    return {"g": g["game_id"], "p": p, "good": good_n, "legal": legal_n, "pct": round(pct, 1), "secs": secs,
            "tc": f"{int(base) // 60}+{inc}" if int(base) % 60 == 0 else g["time_control"],
            "date": f"{ended.tm_mday} {calendar.month_abbr[ended.tm_mon]}", "ph": ph,
            "fen": fen.split(" ")[0], "fx": fen.split(" ", 1)[1], "uci": played, "san": lg.get(played, played), "best": best, "bsan": lg.get(best, best),
            "gl": good_list.split(), "cl": close_list.split(), "lg": lg, "wb": wb, "wa": wa, "link": link}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--botdb", required=True)
    ap.add_argument("--counts", action="append", required=True)
    ap.add_argument("--site", required=True, choices=["lichess", "chess.com"])
    ap.add_argument("--user", required=True)
    ap.add_argument("--month", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--template", default=str(Path(__file__).with_name("blundermap_template.html")))
    ap.add_argument("--summary", action="store_true", help="print one line of JSON about the page on standard output")
    args = ap.parse_args()
    games, counts, blunders = load(args.botdb, args.counts, args.site, args.user, args.month)
    if not games:
        if args.summary:
            print(json.dumps({"games": 0}))
        sys.exit(f"no games for {args.user} on {args.site} in {args.month}")
    data = build(games, counts, blunders, args.site, args.user, args.month, random.Random(f"{args.site}:{args.user}:{args.month}"))
    # the calculations behind the words go to the printout only, not into the member's page
    shown = [("Whole month", data["overall"])] + [(PH_NAMES[p], data["phases"][p]) for p in ("opening", "middle", "end")] \
        + [(q["name"], q) for q in data["quads"]]
    calcs = [f"  {name:22} you {x['mb']:>3} in {x['mn']:<5} them {x['ob']:>3} in {x['on']:<5} {x['calc']:34} -> {x['words']}"
             for name, x in shown]
    for x in [data["overall"], *data["phases"].values(), *data["quads"], *data["cells"]]:
        x.pop("calc")
    page = Path(args.template).read_text(encoding="utf-8")
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    if "/*DATA*/null" not in page:
        sys.exit("template has no /*DATA*/null placeholder")
    Path(args.out).write_text(page.replace("/*DATA*/null", blob), encoding="utf-8")
    print(f"{args.user} {args.month}: {data['used']} of {data['analysed']} analysed games mapped ({data['waiting']} waiting, "
          f"{data['noClock']} without clocks, {data['mismatch']} length mismatches), {len(data['mine'])} blunder cards, "
          f"{len(blob) // 1024} KB of data", file=sys.stderr)
    print("\n".join(calcs), file=sys.stderr)
    if args.summary:
        print(json.dumps({k: data[k] for k in ("games", "analysed", "skipped", "used", "waiting", "noClock", "mismatch", "monthOpen")}
                         | {"cards": len(data["mine"])}))


if __name__ == "__main__":
    main()
