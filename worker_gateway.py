"""The one program the analysis worker's SSH key may run.

On the machine that holds the database, the worker's key is locked in ~/.ssh/authorized_keys to this program:

    command="/path/to/venv/bin/python /path/to/worker_gateway.py desk",restrict ssh-ed25519 AAAA...

The name after the program (here `desk`) is the worker's name, fixed by that line and not by the worker, so one
worker can't act as another. SSH hands the worker's own request to the program as SSH_ORIGINAL_COMMAND, and this
program understands only these, nothing else and no shell:

    hello              the protocol version, our method version and the server's clock
    claim N            up to N games to analyse (N from 1 to 100), as a JSON list
    submit             finished analyses as JSON on standard input, {"results": [...]}, each with `evals` as base64
    release            games given back as JSON on standard input, {"releases": [{"site", "game_id", "skip_reason", "error"}]}

Everything printed is one line of JSON. A request that can't be understood prints {"error": "..."} and exits with
a non-zero status. The program reads only the database settings from .env (never the Discord token), and writes
nothing but the queue's own rows.
"""

import base64
import binascii
import json
import os
import re
import sys
import time
from pathlib import Path

PROTOCOL = 1
MAX_CLAIM = 100
MAX_INPUT_BYTES = 4 * 1024 * 1024
WORKER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# The settings the queue functions use. Nothing else is read from .env, so the Discord token never reaches this program.
ENV_NAMES = ("PLAYMOREBLITZ_DB", "DB_LOCK_TIMEOUT", "ANALYSIS_FULL_PRIORITY_GAMES", "ANALYSIS_MAX_GAMES",
             "ANALYSIS_CLAIM_MINUTES", "ANALYSIS_MAX_ATTEMPTS")


class Refused(Exception):
    """The request can't be carried out; the message goes back to the worker."""


def load_env_file(path, environ, names=ENV_NAMES):
    """Copy the `names` found in the KEY=VALUE file at `path` into `environ`, without replacing anything already set.
    A missing file is fine. Blank lines, # comments and quotes round a value are understood; nothing else is."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key in names and key not in environ:
            environ[key] = value


def _json_from(stdin, key):
    raw = stdin.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise Refused("the request is too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise Refused("the request is not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise Refused(f'the request must be an object with a list called "{key}"')
    return data[key]


def _decode_evals(result):
    """The result with `evals` turned from base64 into bytes, or the reason it couldn't be."""
    if not isinstance(result, dict):
        return None, "a result must be an object"
    evals = result.get("evals")
    try:
        decoded = base64.b64decode(evals, validate=True) if isinstance(evals, str) else None
    except (binascii.Error, ValueError):
        decoded = None
    if decoded is None:
        return None, "evals must be base64 text"
    return {**result, "evals": decoded}, None


def handle(worker, request, stdin, now, queue):
    """Carry out one request for `worker`. Returns the JSON-able answer; raises Refused if it can't be done."""
    parts = request.split()
    if not parts:
        raise Refused("no request: use hello, claim N, submit or release")
    verb, args = parts[0], parts[1:]

    if verb == "hello" and not args:
        import analysis
        return {"protocol": PROTOCOL, "method_version": analysis.METHOD_VERSION, "time": now, "worker": worker}

    if verb == "claim":
        if len(args) != 1 or not re.fullmatch(r"[0-9]{1,4}", args[0]) or not 1 <= int(args[0]) <= MAX_CLAIM:
            raise Refused(f"claim takes a number of games from 1 to {MAX_CLAIM}")
        return queue.claim(worker, int(args[0]), now)

    if verb == "submit" and not args:
        results = _json_from(stdin, "results")
        answer = [None] * len(results)
        good = []
        for i, result in enumerate(results):
            decoded, problem = _decode_evals(result)
            if problem:
                answer[i] = {"site": result.get("site") if isinstance(result, dict) else None,
                             "game_id": result.get("game_id") if isinstance(result, dict) else None,
                             "outcome": queue.REJECTED, "detail": problem}
            else:
                good.append((i, decoded))
        reports = queue.submit(worker, [r for _, r in good], now)
        for (i, _), (site, game_id, outcome, detail) in zip(good, reports):
            answer[i] = {"site": site, "game_id": game_id, "outcome": outcome, "detail": detail}
        return answer

    if verb == "release" and not args:
        answer = []
        for item in _json_from(stdin, "releases"):
            if not isinstance(item, dict) or not isinstance(item.get("site"), str) or not isinstance(item.get("game_id"), str):
                answer.append({"site": None, "game_id": None, "released": False, "error": "a release needs a site and a game_id"})
                continue
            reason, error = item.get("skip_reason"), item.get("error")
            entry = {"site": item["site"], "game_id": item["game_id"]}
            if not (reason is None or isinstance(reason, str)) or not (error is None or isinstance(error, str)):
                answer.append({**entry, "released": False, "error": "skip_reason and error must be text"})
                continue
            try:
                answer.append({**entry, "released": queue.release(worker, item["site"], item["game_id"], skip_reason=reason, error=error)})
            except ValueError as why:
                answer.append({**entry, "released": False, "error": str(why)})
        return answer

    raise Refused("unknown request: use hello, claim N, submit or release")


def main(argv=None, environ=None, stdin=None, stdout=None, now=None):
    argv = sys.argv if argv is None else argv
    environ = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    now = int(time.time()) if now is None else now

    def say(payload):
        stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")

    if len(argv) != 2 or not WORKER_NAME.fullmatch(argv[1]):
        say({"error": "the worker name is missing or not valid: it is set in authorized_keys"})
        return 2
    try:
        load_env_file(Path(__file__).with_name(".env"), environ)
        os.environ.update({k: environ[k] for k in ENV_NAMES if k in environ})  # settings read the real environment
        import analysis_queue  # after the environment is set: settings and the database path are read on import
        say(handle(argv[1], environ.get("SSH_ORIGINAL_COMMAND", ""), stdin, now, analysis_queue))
    except Refused as why:
        say({"error": str(why)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
