"""The analysis worker: fetching, engine, batches, and the connection to the bot's machine, all with fakes."""

import base64
import io
import json
import logging
import os
import subprocess
import urllib.error

import pytest

chess = pytest.importorskip("chess")
import chess.engine  # noqa: E402

import analysis  # noqa: E402
import analysis_queue as q  # noqa: E402
import divider  # noqa: E402
import game_data as gd  # noqa: E402
import store  # noqa: E402
import worker as w  # noqa: E402
import worker_gateway as gw  # noqa: E402

MONTH = "2026-09"
NOW = 1_790_000_000

RUY = "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O h3 Nb8 d4 Nbd7".split()   # 20 plies
SCHOLAR = "e4 e5 Bc4 Nc6 Qh5 Nf6 Qxf7#".split()                                                # White mates on ply 7
FOOLS = "f3 e5 g4 Qh4#".split()                                                                 # Black mates on ply 4
STALEMATE = "e3 a5 Qh5 Ra6 Qxa5 h5 Qxc7 Rah6 h4 f6 Qxd7+ Kf7 Qxb7 Qd3 Qxb8 Qh7 Qxc8 Kg6 Qe6".split()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")


CONFIG = w.Config(gateway_target="user@host", gateway_key="key", stockfish_path="sf", nodes=1234, batch_size=10, min_plies=6)


def material(board, token):
    """A stand-in engine: the material balance in centipawns from White's point of view, and the first legal move."""
    values = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330, chess.ROOK: 500, chess.QUEEN: 900}
    total = sum(v * (len(board.pieces(p, chess.WHITE)) - len(board.pieces(p, chess.BLACK))) for p, v in values.items())
    return ("cp", total), next(iter(board.legal_moves)).uci()


class FakeEngine:
    name = "FakeFish 1"

    def __init__(self, evaluate=material):
        self.evaluate_fn, self.calls = evaluate, 0

    def evaluate(self, board, token):
        self.calls += 1
        return self.evaluate_fn(board, token)


# --- terminal positions and the walk through a game -------------------------------------------------------------------------

def test_a_checkmate_is_a_mate_for_whoever_delivered_it_and_a_stalemate_is_a_draw():
    def last(moves):
        b = chess.Board()
        for m in moves:
            b.push_san(m)
        return b
    assert last(SCHOLAR).is_checkmate() and w.terminal_score(last(SCHOLAR)) == ("mate", 1)
    assert last(FOOLS).is_checkmate() and w.terminal_score(last(FOOLS)) == ("mate", -1)
    assert last(STALEMATE).is_stalemate() and w.terminal_score(last(STALEMATE)) == ("cp", 0)


def test_every_position_is_evaluated_once_and_the_lists_line_up():
    engine = FakeEngine()
    scores, bests, played, positions = w.analyse_moves(engine.evaluate, RUY)
    assert len(scores) == len(bests) == len(played) == 20 and len(positions) == 21
    assert engine.calls == 21          # the start and each of the 20 positions after a move
    assert played[:3] == ["e2e4", "e7e5", "g1f3"] and all(b for b in bests)
    assert all(kind == "cp" for kind, _ in scores)


def test_a_game_that_ends_is_not_sent_to_the_engine_for_its_last_position():
    engine = FakeEngine()
    scores, bests, played, positions = w.analyse_moves(engine.evaluate, SCHOLAR)
    assert scores[-1] == ("mate", 1) and engine.calls == 7      # start plus six positions; the mate is scored by the rules
    assert len(positions) == 8


def test_the_engines_best_move_before_each_ply_is_kept_for_the_skip_rule():
    def picky(board, token):
        return ("cp", 0), "e2e4" if board.fullmove_number == 1 and board.turn == chess.WHITE else "a2a3"
    scores, bests, played, positions = w.analyse_moves(picky, RUY[:6])
    assert bests[0] == "e2e4" and bests[1:] == ["a2a3"] * 5


def test_an_illegal_move_is_named():
    with pytest.raises(ValueError) as why:
        w.analyse_moves(FakeEngine().evaluate, ["e4", "e5", "Qxh7"])
    assert "'Qxh7'" in str(why.value) and "ply 3" in str(why.value)


# --- the result of a game ------------------------------------------------------------------------------------------------------

JOB = {"site": "lichess", "game_id": "abcd1234", "month": MONTH, "ended_at": 1, "white_username": "alice_example", "black_username": "zed_example", "attempts": 1}


def test_a_result_has_everything_the_gateway_needs_and_passes_the_queues_own_checks():
    data = gd.GameData(tuple(RUY), 91.0, None)
    run = w.analyse_moves(material, RUY)
    result = w.make_result(JOB, data, run, "FakeFish 1", 1234)
    assert q._problem({**result, "evals": base64.b64decode(result["evals"])}) is None
    assert (result["site"], result["game_id"], result["engine"], result["nodes"], result["plies"]) == ("lichess", "abcd1234", "FakeFish 1", 1234, 20)
    assert result["method_version"] == analysis.METHOD_VERSION and (result["site_white_accuracy"], result["site_black_accuracy"]) == (91.0, None)
    assert analysis.unpack_evals(base64.b64decode(result["evals"])) == run[0]
    assert set(result["white"]) == {"accuracy", "acc_opening", "acc_middle", "acc_end", "inaccuracies", "mistakes", "blunders", "acpl"}
    json.dumps(result)


def test_the_phases_come_from_the_divider_and_a_lone_endgame_is_dropped():
    data = gd.GameData(tuple(RUY), None, None)
    run = w.analyse_moves(material, RUY)
    result = w.make_result(JOB, data, run, "E", 1)
    expected = divider.divide(run[3])
    assert (result["middle_ply"], result["end_ply"]) == (expected if expected[0] is not None else (None, None))


def test_a_mate_game_carries_its_mate_into_the_curve():
    data = gd.GameData(tuple(SCHOLAR), None, None)
    result = w.make_result(JOB, data, w.analyse_moves(material, SCHOLAR), "E", 1)
    assert analysis.unpack_evals(base64.b64decode(result["evals"]))[-1] == ("mate", 1) and result["plies"] == 7


# --- a batch, start to finish, against the real queue -------------------------------------------------------------------------

class Direct:
    """The gateway's own handler with the real queue behind it, standing in for the SSH connection."""

    def __init__(self, worker="desk"):
        self.worker, self.log = worker, []

    def _ask(self, request, body=None):
        self.log.append(request)
        return gw.handle(self.worker, request, io.StringIO(json.dumps(body) if body is not None else ""), NOW, q)

    def hello(self):
        return self._ask("hello")

    def claim(self, n):
        return self._ask(f"claim {n}")

    def submit(self, results):
        return self._ask("submit", {"results": results})

    def release(self, releases):
        return self._ask("release", {"releases": releases})


class FakeHttp:
    def __init__(self):
        self.lichess, self.archives, self.calls, self.fail = {}, {}, [], None

    def lichess_games(self, ids):
        self.calls.append(("lichess", tuple(ids)))
        if self.fail:
            raise w.FetchError(self.fail)
        return {i: self.lichess[i] for i in ids if i in self.lichess}

    def chesscom_archive(self, username, month):
        self.calls.append(("chess.com", username.lower(), month))
        if self.fail:
            raise w.FetchError(self.fail)
        return self.archives.get((username.lower(), month))


def li(moves, **over):
    raw = {"variant": "standard", "status": "resign", "moves": " ".join(moves), "players": {"white": {"analysis": {"accuracy": 90}}, "black": {}}}
    raw.update(over)
    return raw


def cc(game_id, moves, **over):
    pgn = '[Result "1-0"]\n\n' + " ".join(f"{i // 2 + 1}. {m}" if i % 2 == 0 else m for i, m in enumerate(moves)) + " 1-0\n"
    raw = {"url": f"https://www.chess.com/game/{game_id}", "rules": "chess", "pgn": pgn, "initial_setup": gd.STANDARD_START, "accuracies": {"white": 80.5, "black": 70.25}}
    raw.update(over)
    return raw


def record(site, game_id, white, black, n=1):
    return {"site": site, "game_id": game_id, "month": MONTH, "ended_at": 1_000_000 + n, "result": "white", "white_username": white, "black_username": black}


def row(game_id):
    with store.transaction() as conn:
        return dict(conn.execute("SELECT * FROM game_analysis WHERE game_id = ?", (game_id,)).fetchone())


def setup_queue(*records):
    store.add_player("lichess", "alice_example", 1, MONTH, 1500)
    store.add_player("chess.com", "carol_example", 1, MONTH, 1500)
    q.queue_games(list(records), NOW)


def test_a_mixed_batch_ends_up_analysed_or_skipped_for_the_right_reasons():
    setup_queue(
        record("lichess", "aaaaaaa1", "alice_example", "zed_example", 1),      # analysable
        record("lichess", "aaaaaaa2", "zed_example", "alice_example", 2),      # ends in mate
        record("lichess", "aaaaaaa3", "alice_example", "zed_example", 3),      # a variant
        record("lichess", "aaaaaaa4", "alice_example", "zed_example", 4),      # Lichess does not return it
        record("lichess", "aaaaaaa5", "alice_example", "zed_example", 5),      # too short
        record("chess.com", "live/1", "carol_example", "rival_example", 6),   # in the white player's archive
        record("chess.com", "live/2", "rival_example", "carol_example", 7),   # only in the black player's archive
        record("chess.com", "live/3", "carol_example", "rival_example", 8),   # in neither
    )
    http = FakeHttp()
    http.lichess = {"aaaaaaa1": li(RUY), "aaaaaaa2": li(SCHOLAR), "aaaaaaa3": li(RUY, variant="chess960"), "aaaaaaa5": li(FOOLS)}
    http.archives = {("carol_example", MONTH): {"games": [cc("live/1", RUY)]}, ("rival_example", MONTH): {"games": [cc("live/2", SCHOLAR)]}}
    engine = FakeEngine()
    assert w.run_once(Direct(), http, engine, CONFIG) == 8

    done = {g: row(g) for g in ("aaaaaaa1", "aaaaaaa2", "live/1", "live/2")}
    assert {r["status"] for r in done.values()} == {"done"}
    assert (row("aaaaaaa3")["status"], row("aaaaaaa3")["skip_reason"]) == ("skipped", "not_standard_start")
    assert (row("aaaaaaa4")["status"], row("aaaaaaa4")["skip_reason"]) == ("skipped", "unavailable")
    assert (row("aaaaaaa5")["status"], row("aaaaaaa5")["skip_reason"]) == ("skipped", "too_short")
    assert (row("live/3")["status"], row("live/3")["skip_reason"]) == ("skipped", "unavailable")

    first = done["aaaaaaa1"]
    assert (first["engine"], first["nodes"], first["plies"], first["method_version"]) == ("FakeFish 1", 1234, 20, analysis.METHOD_VERSION)
    assert first["site_white_accuracy"] == 90.0 and first["site_black_accuracy"] is None
    assert len(analysis.unpack_evals(first["evals"])) == 20
    for r in done.values():
        assert 0 <= r["white_accuracy"] <= 100 and 0 <= r["black_accuracy"] <= 100 and r["analysed_at"] == NOW
    assert analysis.unpack_evals(done["aaaaaaa2"]["evals"])[-1] == ("mate", 1)
    assert (done["live/1"]["site_white_accuracy"], done["live/1"]["site_black_accuracy"]) == (80.5, 70.25)
    assert q.status(NOW)["counts"] == {"pending": 0, "claimed": 0, "done": 4, "skipped": 4, "failed": 0}


def test_each_chesscom_archive_is_fetched_once_per_batch_and_lichess_in_one_request():
    setup_queue(*[record("chess.com", f"live/{n}", "carol_example", "rival_example", n) for n in range(1, 4)],
                *[record("lichess", f"bbbbbbb{n}", "alice_example", "zed_example", 10 + n) for n in range(1, 4)])
    http = FakeHttp()
    http.lichess = {f"bbbbbbb{n}": li(RUY) for n in range(1, 4)}
    http.archives = {("carol_example", MONTH): {"games": [cc(f"live/{n}", RUY) for n in range(1, 4)]}}
    w.run_once(Direct(), http, FakeEngine(), CONFIG)
    assert sum(1 for c in http.calls if c[0] == "chess.com") == 1
    assert [c for c in http.calls if c[0] == "lichess"] == [("lichess", ("bbbbbbb3", "bbbbbbb2", "bbbbbbb1"))]   # newest first, as claimed


def test_a_failure_to_fetch_gives_every_game_back_to_try_again_and_submits_nothing():
    setup_queue(record("lichess", "ccccccc1", "alice_example", "zed_example", 1), record("chess.com", "live/9", "carol_example", "x_example", 2))
    http = FakeHttp()
    http.fail = "Lichess answered 503"
    gateway = Direct()
    assert w.run_once(gateway, http, FakeEngine(), CONFIG) == 2
    assert all(row(g)["status"] == "pending" and "503" in row(g)["last_error"] for g in ("ccccccc1", "live/9"))
    assert not any(r.startswith("submit") for r in gateway.log)


def test_one_game_that_cannot_be_analysed_does_not_lose_the_others(caplog):
    setup_queue(record("lichess", "ddddddd1", "alice_example", "zed_example", 1), record("lichess", "ddddddd2", "alice_example", "zed_example", 2),
                record("lichess", "ddddddd3", "alice_example", "zed_example", 3))
    http = FakeHttp()
    http.lichess = {"ddddddd1": li(RUY), "ddddddd2": li(RUY[:5] + ["Qxh7", "a6", "Ba4"]), "ddddddd3": li(RUY)}
    with caplog.at_level(logging.ERROR, logger="playmoreblitz.worker"):
        w.run_once(Direct(), http, FakeEngine(), CONFIG)
    assert (row("ddddddd1")["status"], row("ddddddd3")["status"]) == ("done", "done")
    assert row("ddddddd2")["status"] == "pending" and "ValueError" in row("ddddddd2")["last_error"] and "Qxh7" in row("ddddddd2")["last_error"]
    assert "could not analyse lichess ddddddd2" in caplog.text


def test_an_engine_that_fails_on_a_game_is_survived():
    setup_queue(record("lichess", "eeeeeee1", "alice_example", "zed_example", 1), record("lichess", "eeeeeee2", "alice_example", "zed_example", 2))
    http = FakeHttp()
    http.lichess = {"eeeeeee1": li(RUY), "eeeeeee2": li(SCHOLAR)}
    calls = {"n": 0}

    def flaky(board, token):
        calls["n"] += 1
        if board.fullmove_number == 3 and len(board.move_stack) == 4 and board.piece_at(chess.C4):   # partway through the Scholar's game
            raise chess.engine.EngineTerminatedError("the engine died")
        return material(board, token)

    w.run_once(Direct(), http, FakeEngine(flaky), CONFIG)
    assert row("eeeeeee1")["status"] == "done" and row("eeeeeee2")["status"] == "pending"
    assert "EngineTerminatedError" in row("eeeeeee2")["last_error"]


def test_a_result_the_bot_refuses_is_given_back_with_the_reason():
    class Refusing(Direct):
        def submit(self, results):
            return [{"site": r["site"], "game_id": r["game_id"], "outcome": "rejected", "detail": "white accuracy is out of range"} for r in results]

    setup_queue(record("lichess", "fffffff1", "alice_example", "zed_example", 1))
    http = FakeHttp()
    http.lichess = {"fffffff1": li(RUY)}
    w.run_once(Refusing(), http, FakeEngine(), CONFIG)
    assert row("fffffff1")["status"] == "pending" and "result refused: white accuracy is out of range" in row("fffffff1")["last_error"]


def test_an_empty_queue_claims_nothing_and_does_no_work():
    engine = FakeEngine()
    assert w.run_once(Direct(), FakeHttp(), engine, CONFIG) == 0 and engine.calls == 0


def test_a_chesscom_username_that_is_not_a_real_one_is_passed_over():
    setup_queue(record("chess.com", "live/5", "carol_example", "anonymous user!", 1))
    http = FakeHttp()
    http.archives = {("carol_example", MONTH): {"games": [cc("live/5", RUY)]}}
    w.run_once(Direct(), http, FakeEngine(), CONFIG)
    assert row("live/5")["status"] == "done"


# --- the loop ---------------------------------------------------------------------------------------------------------------------

def test_once_stops_when_the_queue_is_empty():
    setup_queue(*[record("lichess", f"ggggggg{n}", "alice_example", "zed_example", n) for n in range(1, 24)])
    http = FakeHttp()
    http.lichess = {f"ggggggg{n}": li(RUY) for n in range(1, 24)}
    config = w.Config("user@host", "key", "sf", batch_size=10, nodes=1000)
    w.serve(Direct(), http, FakeEngine(), config, once=True)
    assert q.status(NOW)["counts"]["done"] == 23 and q.status(NOW)["counts"]["pending"] == 0


def test_an_idle_worker_sleeps_between_looks():
    naps = []
    stops = iter([False, False, True])
    w.serve(Direct(), FakeHttp(), FakeEngine(), w.Config("user@host", "key", "sf", idle_sleep=42), sleep=naps.append, stop=lambda: next(stops))
    assert naps == [42, 42]


def test_a_lost_connection_is_waited_out_and_tried_again():
    class Flaky(Direct):
        def __init__(self):
            super().__init__()
            self.failures = 2

        def claim(self, n):
            if self.failures:
                self.failures -= 1
                raise w.GatewayError("could not reach the bot's machine")
            return super().claim(n)

    naps, stops = [], iter([False, False, False, True])
    w.serve(Flaky(), FakeHttp(), FakeEngine(), w.Config("user@host", "key", "sf", idle_sleep=5), sleep=naps.append, stop=lambda: next(stops))
    assert naps == [60, 60, 5]


def test_with_once_a_lost_connection_is_an_error_not_a_wait():
    class Down(Direct):
        def claim(self, n):
            raise w.GatewayError("down")
    with pytest.raises(w.GatewayError):
        w.serve(Down(), FakeHttp(), FakeEngine(), CONFIG, once=True)


@pytest.mark.parametrize("hello, why", [({"protocol": 99, "method_version": analysis.METHOD_VERSION}, "protocol"),
                                        ({"protocol": w.PROTOCOL, "method_version": analysis.METHOD_VERSION + 1}, "method version")])
def test_a_worker_and_a_bot_that_do_not_match_refuse_to_work_together(hello, why):
    class Odd(Direct):
        def hello(self):
            return hello
    with pytest.raises(w.GatewayError) as refused:
        w.check_gateway(Odd())
    assert why in str(refused.value)
    with pytest.raises(w.GatewayError):
        w.serve(Odd(), FakeHttp(), FakeEngine(), CONFIG, once=True)


# --- the connection to the bot's machine --------------------------------------------------------------------------------------

class Runner:
    """A stand-in for subprocess.run that behaves like ssh: reads its standard input, writes files it was handed."""

    def __init__(self, stdout="", returncode=0, stderr="", raises=None):
        self.stdout, self.returncode, self.stderr, self.raises, self.calls = stdout, returncode, stderr, raises, []

    def __call__(self, command, **kwargs):
        stdin = kwargs["stdin"]
        body = None if stdin == subprocess.DEVNULL else stdin.read().decode("ascii")
        self.calls.append((command, kwargs, body))
        if self.raises:
            raise self.raises
        kwargs["stdout"].write(self.stdout.encode())
        kwargs["stderr"].write(self.stderr.encode())
        return subprocess.CompletedProcess(command, self.returncode)


def gateway(runner):
    return w.Gateway("user@host", "/keys/id", runner=runner, timeout=5)


def test_the_ssh_command_is_a_list_with_the_request_as_its_last_argument_and_no_shell():
    runner = Runner('{"protocol": 1}\n')
    assert gateway(runner).claim(20) == {"protocol": 1}
    command, kwargs, body = runner.calls[0]
    assert command[0] == "ssh" and command[-2:] == ["user@host", "claim 20"]
    assert command[command.index("-i") + 1] == "/keys/id" and "BatchMode=yes" in command and "IdentitiesOnly=yes" in command
    assert kwargs.get("shell") is None and kwargs["timeout"] == 5
    assert kwargs["stdin"] == subprocess.DEVNULL and body is None      # never inherit a standard input that might not end


def test_output_goes_to_files_never_to_pipes_because_windows_ssh_hangs_on_pipes():
    runner = Runner("[]\n")
    gateway(runner).hello()
    _, kwargs, _ = runner.calls[0]
    assert kwargs["stdout"] not in (subprocess.PIPE, None) and kwargs["stderr"] not in (subprocess.PIPE, None)
    assert hasattr(kwargs["stdout"], "write") and hasattr(kwargs["stderr"], "write")


def test_results_and_releases_go_as_json_on_standard_input_from_a_file():
    runner = Runner("[]\n")
    g = gateway(runner)
    g.submit([{"site": "lichess", "game_id": "abcd1234"}])
    g.release([{"site": "lichess", "game_id": "abcd1234", "error": "x"}])
    assert runner.calls[0][0][-1] == "submit" and json.loads(runner.calls[0][2]) == {"results": [{"site": "lichess", "game_id": "abcd1234"}]}
    assert runner.calls[1][0][-1] == "release" and json.loads(runner.calls[1][2])["releases"][0]["error"] == "x"
    assert hasattr(runner.calls[0][1]["stdin"], "read") and runner.calls[0][1]["stdin"].closed


def test_nothing_to_send_means_no_call():
    runner = Runner("[]\n")
    assert gateway(runner).submit([]) == [] and gateway(runner).release([]) == [] and runner.calls == []


def test_a_big_batch_of_results_is_sent_in_several_calls_in_order_and_the_answers_are_joined():
    class Echo(Runner):
        def __call__(self, command, **kwargs):
            body = json.loads(kwargs["stdin"].read().decode("ascii"))
            self.calls.append((command, kwargs, body))
            key = next(iter(body))
            kwargs["stdout"].write((json.dumps([{"game_id": item["game_id"], "outcome": "accepted"} for item in body[key]]) + "\n").encode())
            return subprocess.CompletedProcess(command, 0)

    runner = Echo()
    results = [{"game_id": f"g{n:03d}", "pad": "x" * 5000} for n in range(30)]
    answers = gateway(runner).submit(results)
    assert [a["game_id"] for a in answers] == [f"g{n:03d}" for n in range(30)]
    assert len(runner.calls) > 1 and all(len(json.dumps(c[2])) <= w.Gateway.MAX_BODY + 100 for c in runner.calls)
    assert [item["game_id"] for c in runner.calls for item in c[2]["results"]] == [f"g{n:03d}" for n in range(30)]


def test_a_single_result_larger_than_the_limit_is_still_sent_alone():
    runner = Runner("[]\n")
    gateway(runner).submit([{"game_id": "a", "pad": "x" * 100_000}, {"game_id": "b"}])
    sent = [json.loads(c[2])["results"] for c in runner.calls]
    assert len(sent) == 2 and sent[0][0]["game_id"] == "a" and sent[1][0]["game_id"] == "b"


def test_the_temporary_files_are_gone_afterwards():
    runner = Runner("[]\n")
    gateway(runner).submit([{"game_id": "a"}])
    stdin, stdout = runner.calls[0][1]["stdin"], runner.calls[0][1]["stdout"]
    assert not os.path.exists(stdin.name) and not os.path.exists(stdout.name)


def test_noise_before_the_answer_is_ignored_and_the_last_line_is_the_answer():
    assert gateway(Runner("a warning\n\n[1, 2]\n")).hello() == [1, 2]


@pytest.mark.parametrize("runner, why", [
    (Runner('{"error": "claim takes a number"}\n', returncode=1), "claim takes a number"),
    (Runner("", returncode=255, stderr="Permission denied (publickey)."), "Permission denied"),
    (Runner("not json", returncode=0), "no answer"),
    (Runner("", returncode=0), "no answer"),
    (Runner(raises=subprocess.TimeoutExpired("ssh", 5)), "could not reach"),
    (Runner(raises=FileNotFoundError("ssh")), "could not reach"),
])
def test_every_way_the_connection_can_fail_is_one_error_with_a_reason(runner, why):
    with pytest.raises(w.GatewayError) as failed:
        gateway(runner).hello()
    assert why in str(failed.value)


# --- fetching from the sites ---------------------------------------------------------------------------------------------------

class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener(body=None, error=None, log=None):
    def open_(request, timeout=None):
        if log is not None:
            log.append((request, timeout))
        if error:
            raise error
        return Response(body.encode())
    return open_


def http_error(code):
    return urllib.error.HTTPError("https://x.test/", code, "msg", {}, io.BytesIO(b""))


def test_a_chesscom_archive_is_asked_for_by_lowercase_name_with_our_agent_and_timeout():
    log = []
    http = w.Http("me", 7, 0, opener=opener('{"games": []}', log=log))
    assert http.chesscom_archive("Carol_Example", "2026-09") == {"games": []}
    request, timeout = log[0]
    assert request.full_url == "https://api.chess.com/pub/player/carol_example/games/2026/09" and timeout == 7
    assert request.get_header("User-agent") == "PlayMoreBlitz-Analysis/1.0 (contact: me)"


def test_a_missing_archive_is_none_and_other_failures_are_fetch_errors():
    assert w.Http(opener=opener(error=http_error(404))).chesscom_archive("carol_example", "2026-09") is None
    for error in (http_error(500), http_error(429), urllib.error.URLError("down"), TimeoutError("slow")):
        with pytest.raises(w.FetchError):
            w.Http(opener=opener(error=error)).chesscom_archive("carol_example", "2026-09")
    with pytest.raises(w.FetchError):
        w.Http(opener=opener("not json")).chesscom_archive("carol_example", "2026-09")


@pytest.mark.parametrize("username, month", [("../etc", "2026-09"), ("a", "2026-09"), ("carol example", "2026-09"), ("carol_example", "2026-9"),
                                             ("carol_example", "2026-09/../x"), ("x" * 31, "2026-09")])
def test_a_name_or_month_that_could_bend_the_url_is_refused_before_any_request(username, month):
    log = []
    with pytest.raises(w.FetchError):
        w.Http(opener=opener("{}", log=log)).chesscom_archive(username, month)
    assert log == []


def test_lichess_games_are_fetched_by_id_in_one_post_and_read_as_ndjson():
    log = []
    body = json.dumps({"id": "abcd1234", "moves": "e4"}) + "\n\n" + json.dumps({"id": "wxyz9876", "moves": "d4"}) + "\n"
    http = w.Http("", 30, 0, opener=opener(body, log=log))
    games = http.lichess_games(["abcd1234", "wxyz9876", "missing1"])
    assert set(games) == {"abcd1234", "wxyz9876"}
    request, _ = log[0]
    assert request.get_method() == "POST" and request.data == b"abcd1234,wxyz9876,missing1"
    assert "moves=true" in request.full_url and "accuracy=true" in request.full_url and request.get_header("Accept") == "application/x-ndjson"
    assert "contact" not in request.get_header("User-agent")


@pytest.mark.parametrize("ids", [[], ["short"], ["abcd1234", "bad id!!!"], ["abcd1234/../"]])
def test_lichess_ids_that_are_not_ids_are_refused(ids):
    with pytest.raises(w.FetchError):
        w.Http(opener=opener("")).lichess_games(ids)


def test_lichess_exports_are_spaced_out_even_after_a_failure():
    clock = {"t": 100.0}
    naps = []
    http = w.Http("", 30, 2.0, opener=opener(error=http_error(500)), clock=lambda: clock["t"], sleep=naps.append)
    with pytest.raises(w.FetchError):
        http.lichess_games(["abcd1234"])
    assert naps == []                                        # the first request never waits
    clock["t"] = 100.5
    with pytest.raises(w.FetchError):
        http.lichess_games(["abcd1234"])
    assert naps == [pytest.approx(1.5)]                      # 2 s since the last one ended, 0.5 s have passed
    clock["t"] = 110.0
    with pytest.raises(w.FetchError):
        http.lichess_games(["abcd1234"])
    assert len(naps) == 1                                    # long enough ago: no wait


# --- the engine ----------------------------------------------------------------------------------------------------------------------

class FakeUci:
    def __init__(self, replies):
        self.replies, self.configured, self.analysed, self.quit_called = list(replies), None, [], False
        self.id = {"name": "Stockfish 99"}

    def configure(self, options):
        self.configured = options

    def analyse(self, board, limit, game=None):
        self.analysed.append((limit, game))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def quit(self):
        self.quit_called = True


def patch_uci(monkeypatch, *engines):
    opened = []
    queue = list(engines)

    def popen(path, **kwargs):
        opened.append((path, kwargs))
        return queue.pop(0)
    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", staticmethod(popen))
    return opened


def info(score, best="e2e4"):
    return {"score": chess.engine.PovScore(score, chess.WHITE if not isinstance(score, tuple) else chess.WHITE), "pv": [chess.Move.from_uci(best)]}


def test_the_engine_is_opened_when_first_needed_configured_and_asked_for_nodes(monkeypatch):
    fake = FakeUci([info(chess.engine.Cp(35))])
    opened = patch_uci(monkeypatch, fake)
    engine = w.Engine("sf.exe", 6, 256, 50_000, 30.0)
    assert opened == []
    token = object()
    assert engine.evaluate(chess.Board(), token) == (("cp", 35), "e2e4")
    assert opened[0][0] == "sf.exe" and fake.configured == {"Threads": 6, "Hash": 256} and engine.name == "Stockfish 99"
    limit, game = fake.analysed[0]
    assert limit.nodes == 50_000 and limit.time == 30.0 and game is token
    if os.name == "nt":
        assert opened[0][1]["creationflags"] == subprocess.BELOW_NORMAL_PRIORITY_CLASS


def test_scores_are_always_from_whites_side_and_mates_keep_their_sign(monkeypatch):
    replies = [{"score": chess.engine.PovScore(chess.engine.Cp(50), chess.BLACK), "pv": []},
               {"score": chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE), "pv": [chess.Move.from_uci("d2d4")]},
               {"score": chess.engine.PovScore(chess.engine.Mate(2), chess.BLACK), "pv": [chess.Move.from_uci("d2d4")]}]
    patch_uci(monkeypatch, FakeUci(replies))
    engine = w.Engine("sf", 1, 16, 1000, 5.0)
    assert engine.evaluate(chess.Board(), 1) == (("cp", -50), None)
    assert engine.evaluate(chess.Board(), 1) == (("mate", 3), "d2d4")
    assert engine.evaluate(chess.Board(), 1) == (("mate", -2), "d2d4")


def test_a_dead_engine_is_closed_the_error_passes_on_and_the_next_call_starts_a_new_one(monkeypatch):
    first = FakeUci([chess.engine.EngineTerminatedError("gone")])
    second = FakeUci([info(chess.engine.Cp(10))])
    opened = patch_uci(monkeypatch, first, second)
    engine = w.Engine("sf", 1, 16, 1000, 5.0)
    with pytest.raises(chess.engine.EngineTerminatedError):
        engine.evaluate(chess.Board(), 1)
    assert first.quit_called
    assert engine.evaluate(chess.Board(), 1)[0] == ("cp", 10) and len(opened) == 2


def test_closing_an_engine_that_was_never_opened_is_fine():
    w.Engine("sf", 1, 16, 1000, 5.0).close()


# --- settings ------------------------------------------------------------------------------------------------------------------------

GOOD = {"GATEWAY_TARGET": "user@host", "GATEWAY_KEY": "/k", "STOCKFISH_PATH": "/sf"}


def test_the_defaults_and_the_required_settings():
    c = w.load_config(GOOD)
    assert (c.gateway_target, c.gateway_key, c.stockfish_path) == ("user@host", "/k", "/sf")
    assert (c.engine_threads, c.engine_hash_mb, c.nodes, c.batch_size, c.idle_sleep, c.min_plies, c.lichess_interval) == (8, 512, 200_000, 20, 300.0, 6, 2.0)
    for name in GOOD:
        with pytest.raises(w.ConfigError) as why:
            w.load_config({k: v for k, v in GOOD.items() if k != name})
        assert name in str(why.value)


def test_settings_can_be_changed_and_bad_ones_are_named():
    c = w.load_config({**GOOD, "ENGINE_THREADS": "4", "NODES_PER_POSITION": "100000", "LICHESS_MIN_INTERVAL_SECONDS": "0", "CONTACT": " a-contact "})
    assert (c.engine_threads, c.nodes, c.lichess_interval, c.contact) == (4, 100_000, 0.0, "a-contact")
    for name, value in (("ENGINE_THREADS", "many"), ("ENGINE_THREADS", "0"), ("NODES_PER_POSITION", "5"), ("BATCH_SIZE", "-1"), ("IDLE_SLEEP_SECONDS", "0")):
        with pytest.raises(w.ConfigError) as why:
            w.load_config({**GOOD, name: value})
        assert name in str(why.value)


@pytest.mark.parametrize("target", ["-oProxyCommand=evil", "user host", "user@host extra", "  "])
def test_a_gateway_target_that_could_become_an_ssh_option_or_a_second_argument_is_refused(target):
    with pytest.raises(w.ConfigError):
        w.load_config({**GOOD, "GATEWAY_TARGET": target})


def test_the_settings_file_is_read_and_environment_variables_win(tmp_path):
    path = tmp_path / "worker.env"
    path.write_text("# comment\nGATEWAY_TARGET=file@host\nGATEWAY_KEY='/file/key'\nSTOCKFISH_PATH=/file/sf\nENGINE_THREADS=2\nUNRELATED=1\n", encoding="utf-8")
    env = {"ENGINE_THREADS": "5"}
    gw.load_env_file(path, env, names=w.SETTING_NAMES)
    c = w.load_config(env)
    assert (c.gateway_target, c.gateway_key, c.engine_threads) == ("file@host", "/file/key", 5) and "UNRELATED" not in env


def test_the_program_tells_you_what_is_wrong_with_the_settings_instead_of_crashing(tmp_path, capsys):
    assert w.main(["--config", str(tmp_path / "missing.env")]) == 2
    assert "GATEWAY_TARGET is not set" in capsys.readouterr().err


# --- gaps found by breaking the code -------------------------------------------------------------------------------------------

def test_an_endgame_with_no_middlegame_is_never_sent(monkeypatch):
    monkeypatch.setattr(divider, "divide", lambda positions: (None, 5))
    result = w.make_result(JOB, gd.GameData(tuple(RUY), None, None), w.analyse_moves(material, RUY), "E", 1)
    assert (result["middle_ply"], result["end_ply"]) == (None, None)


def test_a_chesscom_game_only_in_the_black_players_archive_is_still_found():
    setup_queue(record("chess.com", "live/7", "rival_example", "carol_example", 1))
    http = FakeHttp()
    http.archives = {("rival_example", MONTH): {"games": []}, ("carol_example", MONTH): {"games": [cc("live/7", RUY)]}}
    w.run_once(Direct(), http, FakeEngine(), CONFIG)
    assert row("live/7")["status"] == "done"
    assert [c[1] for c in http.calls if c[0] == "chess.com"] == ["rival_example", "carol_example"]


def test_a_name_that_chesscom_could_not_have_is_never_requested():
    setup_queue(record("chess.com", "live/8", "carol_example", "not a name!", 1))
    http = FakeHttp()
    http.archives = {("carol_example", MONTH): {"games": []}}
    w.run_once(Direct(), http, FakeEngine(), CONFIG)
    assert [c[1] for c in http.calls if c[0] == "chess.com"] == ["carol_example"]
    assert row("live/8")["skip_reason"] == "unavailable"


def test_the_same_archive_asked_for_in_different_capitals_is_fetched_once():
    setup_queue(record("chess.com", "live/11", "Carol_Example", "x_example", 1), record("chess.com", "live/12", "carol_example", "y_example", 2))
    http = FakeHttp()
    http.archives = {("carol_example", MONTH): {"games": [cc("live/11", RUY), cc("live/12", RUY)]}}
    w.run_once(Direct(), http, FakeEngine(), CONFIG)
    assert sum(1 for c in http.calls if c[0] == "chess.com") == 1 and row("live/11")["status"] == row("live/12")["status"] == "done"


def test_a_failed_ssh_is_an_error_even_if_it_printed_something_that_looks_like_an_answer():
    with pytest.raises(w.GatewayError):
        gateway(Runner("[]\n", returncode=255, stderr="Connection closed")).hello()
