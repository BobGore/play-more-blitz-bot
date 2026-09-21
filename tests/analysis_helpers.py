"""Builders for analysed games, shared by the tests of the analysis reports, panels and commands."""

import analysis
import analysis_queue as q
import store

MONTH = "2026-09"
NOW = 1_790_000_000
OWNER = 1001


def register(*names, site="lichess"):
    for name in names:
        store.add_player(site, name, OWNER, MONTH, 1500)


def side(**over):
    figures = {"accuracy": 88.4, "acc_opening": 95.2, "acc_middle": 80.5, "acc_end": None, "inaccuracies": 5, "mistakes": 2,
               "blunders": 1, "acpl": 47}
    figures.update(over)
    return figures


def moments_for(white, black):
    """Flagged moves that agree with the two sides' counts: White's on the odd plies, Black's on the even ones."""
    moments = []
    for first_ply, figures in ((1, white), (2, black)):
        ply = first_ply
        for code, count, lost in (("i", figures["inaccuracies"], 6.5), ("m", figures["mistakes"], 12.0), ("b", figures["blunders"], 25.0)):
            for _ in range(count):
                moments.append([ply, code, lost])
                ply += 2
    return sorted(moments)


def spec(n, white="alice_example", black="rival_example", *, site="lichess", month=MONTH, result="white", ended_at=None, **extra):
    """A game as queue_games takes it: n orders games in time and makes the id."""
    game_id = f"{n:08d}" if site == "lichess" else f"live/{n}"
    return {"site": site, "game_id": game_id, "month": month, "ended_at": ended_at or 1_780_000_000 + n * 1000, "result": result,
            "white_username": white, "black_username": black, "time_control": "300+5", "ending": "resigned", **extra}


def analysed(*games, white=None, black=None, plies=40, **result_over):
    """Queue the games (each a spec()) and give every one an analysis result, with `white` and `black` figures if given."""
    q.queue_games(list(games), NOW)
    claimed = q.claim("desk", 1000, NOW + 1)
    results = []
    for job in claimed:
        w = white or side()
        b = black or side(accuracy=61.2, acc_opening=70.0, acc_middle=55.4, inaccuracies=7, mistakes=3, blunders=2, acpl=90)
        results.append({
            "site": job["site"], "game_id": job["game_id"], "method_version": analysis.METHOD_VERSION, "engine": "Stockfish 19", "nodes": 200_000,
            "plies": plies, "middle_ply": 14, "end_ply": None, "eval_ply20": 10, "evals": analysis.pack_evals([("cp", i) for i in range(plies)]),
            "white": w, "black": b, "moments": moments_for(w, b),
            **result_over})
    q.submit("desk", results, NOW + 2)
