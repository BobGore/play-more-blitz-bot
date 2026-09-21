"""The analysis worker: fetches games, runs the chess engine over them, and reports the figures back.

It runs on the machine that has the engine (not the machine that runs the bot). It asks the bot's machine for
games to analyse over SSH, through worker_gateway.py, then for each game:

    fetch the game from Chess.com or Lichess  ->  check it is a standard game  ->  have Stockfish evaluate every
    position  ->  work out accuracy, inaccuracies, mistakes, blunders and phases (analysis.py, divider.py)  ->
    send the figures back.

The moves are held in memory while a game is analysed and never written to disk or sent to the bot's machine.
Settings live in worker.env beside this file (see worker.env.example). Run `python worker.py`; add --once to
stop when the queue is empty, or --check to test the connection and the engine and stop.
"""

import argparse
import base64
import dataclasses
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import analysis
import divider
import game_data
import worker_gateway

log = logging.getLogger("playmoreblitz.worker")

PROTOCOL = worker_gateway.PROTOCOL
_USERNAME = re.compile(r"^[A-Za-z0-9_-]{2,30}$")
_MONTH = re.compile(r"^[0-9]{4}-[0-9]{2}$")

# --- settings -----------------------------------------------------------------------------------------------------

SETTING_NAMES = ("GATEWAY_TARGET", "GATEWAY_KEY", "STOCKFISH_PATH", "ENGINE_THREADS", "ENGINE_HASH_MB", "NODES_PER_POSITION",
                 "SECONDS_PER_POSITION_LIMIT", "BATCH_SIZE", "IDLE_SLEEP_SECONDS", "MIN_PLIES", "LICHESS_MIN_INTERVAL_SECONDS",
                 "REQUEST_TIMEOUT_SECONDS", "CONTACT", "GATEWAY_KNOWN_HOSTS", "LOG_FILE")


@dataclasses.dataclass(frozen=True)
class Config:
    gateway_target: str
    gateway_key: str
    stockfish_path: str
    engine_threads: int = 8
    engine_hash_mb: int = 512
    nodes: int = 200_000
    position_seconds: float = 60.0
    batch_size: int = 20
    idle_sleep: float = 300.0
    min_plies: int = game_data.MIN_PLIES
    lichess_interval: float = 2.0
    request_timeout: float = 30.0
    contact: str = ""
    known_hosts: str = ""  # a known_hosts file for ssh to use; needed when the worker runs as a service under another account
    log_file: str = ""  # where to write the log; empty for the screen only


class ConfigError(Exception):
    pass


def _number(env, name, default, kind, minimum):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} must be a number") from None
    if value < minimum:
        raise ConfigError(f"{name}={raw!r} must be at least {minimum}")
    return value


def load_config(env):
    """A Config from the KEY=VALUE settings in `env` (a dict); raises ConfigError naming what is wrong."""
    for required in ("GATEWAY_TARGET", "GATEWAY_KEY", "STOCKFISH_PATH"):
        if not (env.get(required) or "").strip():
            raise ConfigError(f"{required} is not set (see worker.env.example)")
    target = env["GATEWAY_TARGET"].strip()
    if target.startswith("-") or any(c.isspace() for c in target):
        raise ConfigError("GATEWAY_TARGET must be user@host")
    return Config(
        gateway_target=target,
        gateway_key=env["GATEWAY_KEY"].strip(),
        stockfish_path=env["STOCKFISH_PATH"].strip(),
        engine_threads=_number(env, "ENGINE_THREADS", 8, int, 1),
        engine_hash_mb=_number(env, "ENGINE_HASH_MB", 512, int, 16),
        nodes=_number(env, "NODES_PER_POSITION", 200_000, int, 1000),
        position_seconds=_number(env, "SECONDS_PER_POSITION_LIMIT", 60.0, float, 1.0),
        batch_size=_number(env, "BATCH_SIZE", 20, int, 1),
        idle_sleep=_number(env, "IDLE_SLEEP_SECONDS", 300.0, float, 1.0),
        min_plies=_number(env, "MIN_PLIES", game_data.MIN_PLIES, int, 2),
        lichess_interval=_number(env, "LICHESS_MIN_INTERVAL_SECONDS", 2.0, float, 0.0),
        request_timeout=_number(env, "REQUEST_TIMEOUT_SECONDS", 30.0, float, 1.0),
        contact=(env.get("CONTACT") or "").strip(),
        known_hosts=(env.get("GATEWAY_KNOWN_HOSTS") or "").strip(),
        log_file=(env.get("LOG_FILE") or "").strip(),
    )


# --- the connection to the bot's machine ---------------------------------------------------------------------------------

class GatewayError(Exception):
    pass


class Gateway:
    """Talks to worker_gateway.py over SSH. `runner` is subprocess.run (replaced in tests).

    Input and output go through ordinary temporary files, not pipes: Windows' ssh.exe stops responding when its
    standard output is a pipe made by another program, and when a large request is fed to it through one.
    """

    MAX_BODY = 60_000  # characters of request body per call; bigger batches of results are sent in several calls

    def __init__(self, target, key, runner=subprocess.run, timeout=120, known_hosts=""):
        self.command = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
                        "-o", "StrictHostKeyChecking=accept-new"]
        if known_hosts:
            self.command += ["-o", f"UserKnownHostsFile={known_hosts}"]
        self.command.append(target)
        self.runner, self.timeout = runner, timeout

    def _call(self, request, body=None):
        with tempfile.TemporaryDirectory() as folder:
            paths = {name: os.path.join(folder, name) for name in ("in", "out", "err")}
            try:
                with open(paths["out"], "wb") as out, open(paths["err"], "wb") as err:
                    if body is None:
                        done = self.runner(self.command + [request], stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=self.timeout)
                    else:
                        with open(paths["in"], "wb") as f:
                            f.write(json.dumps(body).encode("ascii"))
                        with open(paths["in"], "rb") as source:
                            done = self.runner(self.command + [request], stdin=source, stdout=out, stderr=err, timeout=self.timeout)
            except (OSError, subprocess.TimeoutExpired) as why:
                raise GatewayError(f"could not reach the bot's machine: {why}") from None
            with open(paths["out"], "rb") as f:
                stdout = f.read().decode("utf-8", "replace")
            with open(paths["err"], "rb") as f:
                stderr = f.read().decode("utf-8", "replace")
        lines = [line for line in stdout.splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1]) if lines else None
        except ValueError:
            payload = None
        if isinstance(payload, dict) and "error" in payload:
            raise GatewayError(f"the gateway refused {request.split()[0]!r}: {payload['error']}")
        if done.returncode != 0 or payload is None:
            detail = stderr.strip().splitlines()[-1:] or ["no answer"]
            raise GatewayError(f"{request.split()[0]} failed (status {done.returncode}): {detail[0][:200]}")
        return payload

    def hello(self):
        return self._call("hello")

    def claim(self, count):
        return self._call(f"claim {int(count)}")

    def _in_chunks(self, request, key, items):
        answers, chunk, size = [], [], 0
        for item in items + [None]:
            length = len(json.dumps(item)) if item is not None else 0
            if chunk and (item is None or size + length > self.MAX_BODY):
                answers += self._call(request, {key: chunk})
                chunk, size = [], 0
            if item is not None:
                chunk.append(item)
                size += length
        return answers

    def submit(self, results):
        return self._in_chunks("submit", "results", list(results))

    def release(self, releases):
        return self._in_chunks("release", "releases", list(releases))


# --- fetching games -------------------------------------------------------------------------------------------------------

class FetchError(Exception):
    pass


class Http:
    """Calls to Chess.com and Lichess: one at a time, with timeouts, and spacing between Lichess exports."""

    def __init__(self, contact="", timeout=30.0, lichess_interval=2.0, opener=urllib.request.urlopen, clock=time.monotonic, sleep=time.sleep):
        self.agent = "PlayMoreBlitz-Analysis/1.0" + (f" (contact: {contact})" if contact else "")
        self.timeout, self.interval, self.opener, self.clock, self.sleep = timeout, lichess_interval, opener, clock, sleep
        self._last_lichess = None

    def _open(self, request):
        try:
            with self.opener(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as why:
            if why.code == 404:
                return None
            raise FetchError(f"{request.full_url.split('?')[0]} answered {why.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as why:
            raise FetchError(f"could not reach {request.full_url.split('?')[0]}: {why}") from None

    def chesscom_archive(self, username, month):
        """A player's monthly archive from Chess.com as a dict, or None if there isn't one."""
        if not _USERNAME.match(username) or not _MONTH.match(month):
            raise FetchError("not a valid username or month")
        year, mon = month.split("-")
        request = urllib.request.Request(f"https://api.chess.com/pub/player/{username.lower()}/games/{year}/{mon}",
                                         headers={"User-Agent": self.agent})
        text = self._open(request)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise FetchError("Chess.com sent something that is not JSON") from None

    def lichess_games(self, ids):
        """{id: game} for those of `ids` that Lichess returns, from one export request."""
        if not ids or any(not re.fullmatch(r"[A-Za-z0-9]{8}", i) for i in ids):
            raise FetchError("not valid Lichess game ids")
        if self._last_lichess is not None:
            wait = self.interval - (self.clock() - self._last_lichess)
            if wait > 0:
                self.sleep(wait)
        request = urllib.request.Request(
            "https://lichess.org/api/games/export/_ids?moves=true&tags=false&clocks=false&evals=false&opening=false&accuracy=true&pgnInJson=false",
            data=",".join(ids).encode(), method="POST",
            headers={"User-Agent": self.agent, "Accept": "application/x-ndjson", "Content-Type": "text/plain"})
        try:
            text = self._open(request)
        finally:
            self._last_lichess = self.clock()
        games = {}
        for line in (text or "").splitlines():
            if line.strip():
                try:
                    raw = json.loads(line)
                except ValueError:
                    raise FetchError("Lichess sent something that is not JSON") from None
                games[raw.get("id")] = raw
        return games


# --- the engine --------------------------------------------------------------------------------------------------------------

def terminal_score(board):
    """The score of a position where the game is over: a mate for whoever delivered it, or a draw."""
    import chess
    outcome = board.outcome()
    if outcome is not None and outcome.winner == chess.WHITE:
        return ("mate", 1)
    if outcome is not None and outcome.winner == chess.BLACK:
        return ("mate", -1)
    return ("cp", 0)


class Engine:
    """Stockfish through python-chess, at a fixed number of nodes per position. Reopened if it dies."""

    def __init__(self, path, threads, hash_mb, nodes, seconds):
        self.path, self.threads, self.hash_mb, self.nodes, self.seconds = path, threads, hash_mb, nodes, seconds
        self._engine = None
        self.name = "Stockfish"

    def _open(self):
        import chess.engine
        extra = {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS} if os.name == "nt" else {}  # the machine stays usable
        self._engine = chess.engine.SimpleEngine.popen_uci(self.path, **extra)
        self._engine.configure({"Threads": self.threads, "Hash": self.hash_mb})
        self.name = str(self._engine.id.get("name") or "Stockfish")[:100]

    def evaluate(self, board, game_token):
        """(score, best move in UCI) for a position: the score is ("cp", n) or ("mate", n) from White's point of view."""
        import chess.engine
        if self._engine is None:
            self._open()
        try:
            info = self._engine.analyse(board, chess.engine.Limit(nodes=self.nodes, time=self.seconds), game=game_token)
        except (chess.engine.EngineError, chess.engine.EngineTerminatedError, OSError):
            self.close()
            raise
        score = info["score"].white()
        pv = info.get("pv") or []
        return (("mate", score.mate()) if score.is_mate() else ("cp", score.score())), (pv[0].uci() if pv else None)

    def close(self):
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.quit()
            except Exception:
                pass


def analyse_moves(evaluate, moves):
    """Have `evaluate(board, token)` score every position of a game once.

    Returns (scores, bests, played, positions): the score after each ply, the engine's preferred move in the position
    before each ply, the moves played (UCI), and every position from the start as bitboards for divider.divide.
    Raises ValueError if a move is not legal.
    """
    import chess
    board, token = chess.Board(), object()
    current = evaluate(board, token)  # the start position: only its preferred move is used
    scores, bests, played = [], [], []
    positions = [_bitboards(board)]
    for san in moves:
        try:
            move = board.parse_san(san)
        except ValueError:
            raise ValueError(f"{san!r} is not a legal move at ply {len(played) + 1}") from None
        bests.append(current[1] if current else None)
        played.append(move.uci())
        board.push(move)
        positions.append(_bitboards(board))
        if board.is_game_over():
            scores.append(terminal_score(board))
            current = None
        else:
            current = evaluate(board, token)
            scores.append(current[0])
    return scores, bests, played, positions


def _bitboards(board):
    import chess
    return (board.occupied, board.kings, board.pawns, board.occupied_co[chess.WHITE], board.occupied_co[chess.BLACK])


def make_result(job, data, run, engine_name, nodes):
    """The result dict the gateway's `submit` takes, for one analysed game."""
    scores, bests, played, positions = run
    middle, end = divider.divide(positions)
    if middle is None:
        end = None  # there is no endgame without a middlegame
    summary = analysis.summarise(scores, middle, end, bests, played)
    return {
        "site": job["site"],
        "game_id": job["game_id"],
        "method_version": analysis.METHOD_VERSION,
        "engine": engine_name,
        "nodes": nodes,
        "plies": len(scores),
        "middle_ply": middle,
        "end_ply": end,
        "eval_ply20": summary.eval_ply20,
        "evals": base64.b64encode(analysis.pack_evals(scores)).decode("ascii"),
        "white": dataclasses.asdict(summary.white),
        "black": dataclasses.asdict(summary.black),
        "site_white_accuracy": data.site_white_accuracy,
        "site_black_accuracy": data.site_black_accuracy,
    }


# --- one batch -----------------------------------------------------------------------------------------------------------------

def fetch_games(jobs, http, min_plies):
    """{(site, game_id): GameData or an Exception} for a batch of jobs. A failure to fetch at all raises FetchError."""
    found = {}
    lichess = [j for j in jobs if j["site"] == "lichess"]
    if lichess:
        raw = http.lichess_games([j["game_id"] for j in lichess])
        for j in lichess:
            game = raw.get(j["game_id"])
            found[("lichess", j["game_id"])] = (
                game_data.NotAnalysable(game_data.UNAVAILABLE, "Lichess did not return the game") if game is None else _read(game_data.from_lichess, game, min_plies))
    archives = {}
    for j in (j for j in jobs if j["site"] == "chess.com"):
        game = None
        for username in (j["white_username"], j["black_username"]):
            if not _USERNAME.match(username):
                continue  # not a name Chess.com can have (such as "anonymous" placeholders); the other player's archive will do
            key = (username.lower(), j["month"])
            if key not in archives:
                archives[key] = http.chesscom_archive(username, j["month"])
            game = game_data.find_chesscom_game(archives[key] or {}, j["game_id"])
            if game is not None:
                break
        found[("chess.com", j["game_id"])] = (
            game_data.NotAnalysable(game_data.UNAVAILABLE, "the game is not in either player's Chess.com archive") if game is None
            else _read(game_data.from_chesscom, game, min_plies))
    return found


def _read(parse, raw, min_plies):
    try:
        return parse(raw, min_plies)
    except game_data.NotAnalysable as why:
        return why


def process_batch(jobs, http, engine, config):
    """Analyse a batch. Returns (results, releases): results for `submit`, releases for `release`."""
    results, releases = [], []

    def give_back(job, error=None, skip=None):
        item = {"site": job["site"], "game_id": job["game_id"]}
        if skip:
            item["skip_reason"] = skip
        if error:
            item["error"] = str(error)[:200]
        releases.append(item)

    try:
        games = fetch_games(jobs, http, config.min_plies)
    except FetchError as why:
        log.warning("could not fetch games: %s", why)
        for job in jobs:
            give_back(job, error=why)
        return results, releases

    for job in jobs:
        data = games[(job["site"], job["game_id"])]
        if isinstance(data, game_data.NotAnalysable):
            log.info("skipping %s %s: %s", job["site"], job["game_id"], data)
            give_back(job, skip=data.reason, error=str(data))
            continue
        started = time.monotonic()
        try:
            run = analyse_moves(engine.evaluate, data.moves)
            results.append(make_result(job, data, run, engine.name, config.nodes))
            log.info("analysed %s %s (%d plies) in %.1f s", job["site"], job["game_id"], len(data.moves), time.monotonic() - started)
        except Exception as why:  # one bad game or a dead engine must not lose the rest of the batch
            log.exception("could not analyse %s %s", job["site"], job["game_id"])
            give_back(job, error=f"{type(why).__name__}: {why}")
    return results, releases


def run_once(gateway, http, engine, config):
    """Claim one batch, analyse it and report. Returns how many games were claimed."""
    jobs = gateway.claim(config.batch_size)
    if not jobs:
        return 0
    log.info("claimed %d game(s)", len(jobs))
    results, releases = process_batch(jobs, http, engine, config)
    if results:
        for outcome in gateway.submit(results):
            if outcome["outcome"] == "rejected":
                log.error("the bot refused the result for %s %s: %s", outcome["site"], outcome["game_id"], outcome["detail"])
                releases.append({"site": outcome["site"], "game_id": outcome["game_id"], "error": f"result refused: {outcome['detail']}"[:200]})
    if releases:
        gateway.release(releases)
    return len(jobs)


def check_gateway(gateway):
    hello = gateway.hello()
    if hello.get("protocol") != PROTOCOL:
        raise GatewayError(f"the bot speaks protocol {hello.get('protocol')}, this worker speaks {PROTOCOL}: update one of them")
    if hello.get("method_version") != analysis.METHOD_VERSION:
        raise GatewayError(f"the bot expects method version {hello.get('method_version')}, this worker has {analysis.METHOD_VERSION}: update the worker")
    return hello


def serve(gateway, http, engine, config, *, once=False, sleep=time.sleep, stop=lambda: False):
    """Keep claiming batches until asked to stop (or, with `once`, until the queue is empty)."""
    check_gateway(gateway)
    while not stop():
        try:
            claimed = run_once(gateway, http, engine, config)
        except GatewayError as why:
            log.warning("%s", why)
            if once:
                raise
            sleep(60)
            continue
        if claimed == 0:
            if once:
                return
            sleep(config.idle_sleep)


# --- starting up ------------------------------------------------------------------------------------------------------------------

def setup_logging(log_file=""):
    """Log to the screen, and also to `log_file` (rotated at 2 MB, three old files kept) if one is given."""
    import logging.handlers
    handlers = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers, force=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="PlayMoreBlitz analysis worker")
    parser.add_argument("--config", default=str(Path(__file__).with_name("worker.env")), help="the settings file")
    parser.add_argument("--once", action="store_true", help="stop when the queue is empty")
    parser.add_argument("--check", action="store_true", help="test the connection and the engine, then stop")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    worker_gateway.load_env_file(args.config, env, names=SETTING_NAMES)
    try:
        config = load_config(env)
    except ConfigError as why:
        print(f"Settings problem: {why}", file=sys.stderr)
        return 2
    setup_logging(config.log_file)
    gateway = Gateway(config.gateway_target, config.gateway_key, known_hosts=config.known_hosts)
    http = Http(config.contact, config.request_timeout, config.lichess_interval)
    engine = Engine(config.stockfish_path, config.engine_threads, config.engine_hash_mb, config.nodes, config.position_seconds)
    try:
        hello = check_gateway(gateway)
        log.info("connected to the bot's machine (protocol %s, method version %s)", hello["protocol"], hello["method_version"])
        if args.check:
            import chess
            engine.evaluate(chess.Board(), object())
            log.info("engine ready: %s", engine.name)
            return 0
        serve(gateway, http, engine, config, once=args.once)
    except GatewayError as why:
        log.error("Problem: %s", why)  # to the log file too, for a worker that runs as a service with no screen
        return 1
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
