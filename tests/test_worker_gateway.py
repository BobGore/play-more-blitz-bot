"""The gateway the analysis worker's SSH key is locked to: what it understands, what it refuses, and what it reads."""

import base64
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import analysis
import analysis_queue as q
import settings
import store
import worker_gateway as gw

MONTH = "2026-09"
NOW = 1_790_000_000
ROOT = Path(gw.__file__).parent


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")


def register(*names):
    for name in names:
        store.add_player("lichess", name, 1001, MONTH, 1500)


def game(n, white="alice_example", black="zed_example"):
    return {"site": "lichess", "game_id": f"g{n:05d}", "month": MONTH, "ended_at": 1_000_000 + n, "result": "white",
            "white_username": white, "black_username": black}


def ask(request, body=None, worker="desk"):
    stdin = io.StringIO(body if isinstance(body, str) else json.dumps(body) if body is not None else "")
    return gw.handle(worker, request, stdin, NOW, q)


def result(game_id="g00001", **over):
    plies = 40
    side = {"accuracy": 90.0, "acc_opening": 95.0, "acc_middle": 85.0, "acc_end": None, "inaccuracies": 2, "mistakes": 1,
            "blunders": 0, "acpl": 30}
    r = {"site": "lichess", "game_id": game_id, "method_version": 1, "engine": "Stockfish 19", "nodes": 200000, "plies": plies,
         "middle_ply": 12, "end_ply": None, "eval_ply20": 10, "white": side, "black": dict(side, accuracy=70.0),
         "evals": base64.b64encode(analysis.pack_evals([("cp", i) for i in range(plies)])).decode()}
    r.update(over)
    return r


# --- what it understands -----------------------------------------------------------------------------------------

def test_hello_reports_the_protocol_the_method_version_and_the_clock():
    assert ask("hello", worker="desk") == {"protocol": gw.PROTOCOL, "method_version": analysis.METHOD_VERSION, "time": NOW, "worker": "desk"}


def test_a_claim_returns_jobs_that_are_plain_json_and_marks_them_as_the_workers():
    register("alice_example")
    q.queue_games([game(1), game(2)], NOW)
    jobs = ask("claim 5", worker="rig")
    assert [j["game_id"] for j in jobs] == ["g00002", "g00001"]
    json.dumps(jobs)
    assert {j["attempts"] for j in jobs} == {1}
    with store.transaction() as conn:
        assert {r["claimed_by"] for r in conn.execute("SELECT claimed_by FROM game_analysis")} == {"rig"}


@pytest.mark.parametrize("request_", ["", "   ", "claim", "claim 0", "claim 101", "claim -1", "claim abc", "claim 5 6", "claim 1.5",
                                      "claim 1e2", "claim 5;ls", "claim $(id)", "claim 99999", "hello now", "submit now", "release now",
                                      "status", "drop table game_analysis", "CLAIM 5", "claim٥"])
def test_anything_else_is_refused_with_a_reason(request_):
    with pytest.raises(gw.Refused) as why:
        ask(request_)
    assert str(why.value)


def test_the_largest_claim_allowed_is_a_hundred():
    register("alice_example")
    q.queue_games([game(n) for n in range(1, 121)], NOW)
    assert len(ask(f"claim {gw.MAX_CLAIM}")) == 100 and len(ask("claim 1")) == 1


def test_a_good_result_arrives_as_base64_and_is_stored_as_bytes():
    register("alice_example")
    q.queue_games([game(1)], NOW)
    ask("claim 1")
    assert ask("submit", {"results": [result()]}) == [{"site": "lichess", "game_id": "g00001", "outcome": "accepted", "detail": None}]
    with store.transaction() as conn:
        row = conn.execute("SELECT status, evals FROM game_analysis").fetchone()
    assert row["status"] == "done" and analysis.unpack_evals(row["evals"]) == [("cp", i) for i in range(40)]


def test_results_that_cannot_be_decoded_are_refused_one_by_one_and_the_order_is_kept():
    register("alice_example")
    q.queue_games([game(1), game(2)], NOW)
    ask("claim 2")
    body = {"results": [result("g00001", evals="not base64!!"), result("g00002"), "junk", result("g00003", evals=12345), {"evals": None}]}
    answer = ask("submit", body)
    assert [a["outcome"] for a in answer] == ["rejected", "accepted", "rejected", "rejected", "rejected"]
    assert answer[0]["game_id"] == "g00001" and "base64" in answer[0]["detail"]
    assert answer[2] == {"site": None, "game_id": None, "outcome": "rejected", "detail": "a result must be an object"}
    with store.transaction() as conn:
        assert {r["game_id"]: r["status"] for r in conn.execute("SELECT game_id, status FROM game_analysis")} == {"g00001": "claimed", "g00002": "done"}


def test_the_answers_line_up_with_the_results_even_when_some_never_reach_the_queue():
    register("alice_example")
    q.queue_games([game(1), game(2), game(3)], NOW)
    ask("claim 2", worker="rig")   # games 3 and 2: game 1 stays pending
    body = {"results": [result("g00003"), result("g00002", evals="AAAA$$AAAA"), result("g00001"), result("g00002")]}
    answer = ask("submit", body, worker="rig")
    assert [(a["game_id"], a["outcome"]) for a in answer] == [
        ("g00003", "accepted"), ("g00002", "rejected"), ("g00001", "rejected"), ("g00002", "accepted")]
    assert "base64" in answer[1]["detail"] and "pending" in answer[2]["detail"]


def test_a_release_by_a_named_worker_is_that_workers():
    register("alice_example")
    q.queue_games([game(1)], NOW)
    ask("claim 1", worker="rig")
    assert ask("release", {"releases": [{"site": "lichess", "game_id": "g00001"}]}, worker="desk")[0]["released"] is False
    assert ask("release", {"releases": [{"site": "lichess", "game_id": "g00001"}]}, worker="rig")[0]["released"] is True


def test_an_empty_submit_is_fine():
    assert ask("submit", {"results": []}) == []


def test_a_result_for_a_game_claimed_by_another_worker_is_refused():
    register("alice_example")
    q.queue_games([game(1)], NOW)
    ask("claim 1", worker="desk")
    (answer,) = ask("submit", {"results": [result()]}, worker="other")
    assert answer["outcome"] == "rejected" and "not claimed by other" in answer["detail"]


def test_releases_report_what_happened_to_each_game():
    register("alice_example")
    q.queue_games([game(1), game(2), game(3)], NOW)
    ask("claim 3")
    body = {"releases": [
        {"site": "lichess", "game_id": "g00001", "error": "timed out"},
        {"site": "lichess", "game_id": "g00002", "skip_reason": "too_short"},
        {"site": "lichess", "game_id": "g00003", "skip_reason": "over_monthly_limit"},
        {"site": "lichess", "game_id": "g99999"},
        {"site": "lichess"},
        "junk",
        {"site": "lichess", "game_id": "g00003", "error": 5},
    ]}
    answer = ask("release", body)
    assert [a["released"] for a in answer] == [True, True, False, False, False, False, False]
    assert "over_monthly_limit" in answer[2]["error"] and answer[4]["site"] is None and answer[6]["error"] == "skip_reason and error must be text"
    with store.transaction() as conn:
        rows = {r["game_id"]: (r["status"], r["skip_reason"], r["last_error"]) for r in conn.execute("SELECT * FROM game_analysis")}
    assert rows["g00001"] == ("pending", None, "timed out") and rows["g00002"] == ("skipped", "too_short", None)
    assert rows["g00003"][0] == "claimed"   # the invented reason changed nothing


def test_a_worker_can_only_give_back_its_own_games():
    register("alice_example")
    q.queue_games([game(1)], NOW)
    ask("claim 1", worker="desk")
    assert ask("release", {"releases": [{"site": "lichess", "game_id": "g00001"}]}, worker="other")[0]["released"] is False


# --- what it refuses to read ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body, why", [
    ("not json at all", "not valid JSON"),
    ("", "not valid JSON"),
    ("[1, 2]", 'a list called "results"'),
    ('{"results": 5}', 'a list called "results"'),
    ('{"other": []}', 'a list called "results"'),
    ("null", 'a list called "results"'),
])
def test_input_that_is_not_the_expected_shape_is_refused(body, why):
    with pytest.raises(gw.Refused) as refused:
        ask("submit", body)
    assert why in str(refused.value)


def test_a_request_body_that_is_too_large_is_refused_without_being_parsed(monkeypatch):
    monkeypatch.setattr(gw, "MAX_INPUT_BYTES", 100)
    with pytest.raises(gw.Refused) as refused:
        ask("submit", '{"results": [' + '"x", ' * 100 + '"x"]}')
    assert "too large" in str(refused.value)


# --- the name of the worker and the program as a whole ---------------------------------------------------------------

def run_main(argv, request="hello", body="", environ=None):
    out = io.StringIO()
    env = {"SSH_ORIGINAL_COMMAND": request, **(environ or {})}
    code = gw.main(argv, env, io.StringIO(body), out, NOW)
    return code, json.loads(out.getvalue())


@pytest.mark.parametrize("argv", [["gw"], ["gw", ""], ["gw", "a b"], ["gw", "x;y"], ["gw", "a" * 41], ["gw", "desk", "extra"], ["gw", "../etc"], ["gw", "désk"]])
def test_without_a_valid_worker_name_from_authorized_keys_nothing_runs(argv):
    code, said = run_main(argv)
    assert code == 2 and "worker name" in said["error"]


def test_a_refused_request_prints_the_reason_and_exits_non_zero():
    code, said = run_main(["gw", "desk"], request="claim 0")
    assert code == 1 and "claim takes" in said["error"]
    code, said = run_main(["gw", "desk"], request="")
    assert code == 1 and "no request" in said["error"]


# --- the settings file -------------------------------------------------------------------------------------------------

def test_only_the_named_settings_are_read_from_the_file_and_never_the_discord_token(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# a comment\n\nDISCORD_TOKEN=very-secret\nCONTACT=a-contact-address\nPLAYMOREBLITZ_DB = '/mnt/big/x.db'\n"
                        'ANALYSIS_MAX_GAMES="900"\nnot a setting line\nANALYSIS_MAX_ATTEMPTS=7\nEMPTY=\n', encoding="utf-8")
    environ = {"ANALYSIS_MAX_ATTEMPTS": "3"}
    gw.load_env_file(env_file, environ)
    assert environ == {"PLAYMOREBLITZ_DB": "/mnt/big/x.db", "ANALYSIS_MAX_GAMES": "900", "ANALYSIS_MAX_ATTEMPTS": "3"}


def test_a_missing_settings_file_is_fine(tmp_path):
    environ = {}
    gw.load_env_file(tmp_path / "nope.env", environ)
    assert environ == {}


def test_the_gateway_reads_every_setting_its_queue_functions_use():
    used = set()
    for name in ("analysis_queue.py", "store.py", "analysis.py"):
        used |= set(re.findall(r"settings\.([A-Z_]+)", (ROOT / name).read_text(encoding="utf-8")))
    used = {"PLAYMOREBLITZ_DB" if u == "DB_PATH" else u for u in used}
    assert used <= set(gw.ENV_NAMES), used - set(gw.ENV_NAMES)
    assert set(gw.ENV_NAMES) <= set(settings.NAMES)


# --- the program run the way sshd runs it ------------------------------------------------------------------------------

@pytest.fixture
def deployed(tmp_path):
    """The program and the modules it needs copied beside a .env of their own, as on the server."""
    home = tmp_path / "deploy"
    home.mkdir()
    for name in ("worker_gateway.py", "analysis_queue.py", "analysis.py", "store.py", "settings.py"):
        shutil.copy(ROOT / name, home / name)
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "q.db"
    (home / ".env").write_text(f"DISCORD_TOKEN=very-secret\nPLAYMOREBLITZ_DB={db}\nANALYSIS_MAX_GAMES=900\n", encoding="utf-8")
    return home, db


def run_deployed(home, request, body="", name="desk", env_extra=None):
    import os
    env = {k: v for k, v in os.environ.items() if k not in settings.NAMES and k not in ("SSH_ORIGINAL_COMMAND", "DISCORD_TOKEN")}
    if request is not None:
        env["SSH_ORIGINAL_COMMAND"] = request
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "worker_gateway.py", name], cwd=home, input=body, capture_output=True, text=True, env=env)


def test_run_the_way_sshd_runs_it_it_uses_the_database_named_in_the_settings_file(deployed, monkeypatch):
    home, db = deployed
    monkeypatch.setattr(store, "DB_PATH", db)
    register("alice_example")
    q.queue_games([game(1)], NOW)
    done = run_deployed(home, "claim 3")
    assert done.returncode == 0 and done.stderr == ""
    (job,) = json.loads(done.stdout)
    assert job["game_id"] == "g00001" and done.stdout.count("\n") == 1
    with store.transaction() as conn:
        assert conn.execute("SELECT status, claimed_by FROM game_analysis").fetchone()["claimed_by"] == "desk"


def test_run_the_way_sshd_runs_it_a_full_round_trip_works(deployed, monkeypatch):
    home, db = deployed
    monkeypatch.setattr(store, "DB_PATH", db)
    register("alice_example")
    q.queue_games([game(1)], NOW)
    run_deployed(home, "claim 1")
    done = run_deployed(home, "submit", json.dumps({"results": [result()]}))
    assert json.loads(done.stdout)[0]["outcome"] == "accepted"
    hello = json.loads(run_deployed(home, "hello").stdout)
    assert hello["protocol"] == gw.PROTOCOL and hello["worker"] == "desk"


def test_the_settings_file_really_is_read_by_the_program(deployed):
    home, _ = deployed
    (home / ".env").write_text("ANALYSIS_MAX_GAMES=lots\n", encoding="utf-8")
    done = run_deployed(home, "hello")
    assert done.returncode != 0 and "ANALYSIS_MAX_GAMES" in done.stderr


def test_a_missing_request_or_worker_name_is_refused_when_run_for_real(deployed):
    home, _ = deployed
    done = run_deployed(home, None)
    assert done.returncode == 1 and "no request" in json.loads(done.stdout)["error"]
    done = subprocess.run([sys.executable, "worker_gateway.py"], cwd=home, capture_output=True, text=True)
    assert done.returncode == 2 and "worker name" in json.loads(done.stdout)["error"]


def test_an_attempt_to_run_a_shell_command_through_the_request_does_nothing(deployed, tmp_path):
    home, _ = deployed
    marker = tmp_path / "pwned"
    for request in (f"claim 1; touch {marker}", f"hello && touch {marker}", f"$(touch {marker})", f"`touch {marker}`"):
        done = run_deployed(home, request)
        assert done.returncode == 1
    assert not marker.exists()
