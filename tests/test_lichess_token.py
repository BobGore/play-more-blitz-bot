"""The Lichess API token: sent to Lichess only, never shown, and a refused export says so."""

import asyncio
import json
import logging

import pytest

import sources

TOKEN = "lip_example_not_a_real_token"


class FakeResponse:
    def __init__(self, status, body=""):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._body


class FakeSession:
    """Records every request; replies from a {url prefix: (status, body)} table."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"url": url, "headers": dict(headers or {}), "allow_redirects": allow_redirects})
        for prefix, reply in self.replies.items():
            if url.startswith(prefix):
                return FakeResponse(*reply)
        return FakeResponse(404)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(sources, "LICHESS_TOKEN", TOKEN)
    monkeypatch.setattr(sources, "LICHESS_EXPORT_MIN_INTERVAL", 0)


@pytest.fixture
def no_token(monkeypatch):
    monkeypatch.setattr(sources, "LICHESS_TOKEN", "")
    monkeypatch.setattr(sources, "LICHESS_EXPORT_MIN_INTERVAL", 0)


def get(session, site, url):
    return asyncio.run(sources._get(session, site, url, "alice", timeout=None))


def test_lichess_requests_carry_the_token(token):
    session = FakeSession({"https://lichess.org/": (200, "{}")})
    get(session, "lichess", "https://lichess.org/api/user/alice")
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_with_the_token_redirects_are_not_followed(token):
    # A redirect could carry the Authorization header somewhere else.
    session = FakeSession({"https://lichess.org/": (200, "{}")})
    get(session, "lichess", "https://lichess.org/api/user/alice")
    assert session.calls[0]["allow_redirects"] is False


def test_chesscom_never_gets_the_lichess_token(token):
    session = FakeSession({"https://api.chess.com/": (200, "{}")})
    get(session, "chess.com", "https://api.chess.com/pub/player/alice")
    assert "Authorization" not in session.calls[0]["headers"]
    assert session.calls[0]["allow_redirects"] is True


def test_a_lichess_named_call_to_another_host_never_gets_the_token(token):
    session = FakeSession({"https://example.org/": (200, "{}")})
    get(session, "lichess", "https://example.org/api/user/alice")
    assert "Authorization" not in session.calls[0]["headers"]


def test_without_a_token_nothing_changes(no_token):
    session = FakeSession({"https://lichess.org/": (200, "{}")})
    get(session, "lichess", "https://lichess.org/api/user/alice")
    assert "Authorization" not in session.calls[0]["headers"]
    assert session.calls[0]["allow_redirects"] is True


def test_a_refused_token_says_so_without_showing_it(token):
    session = FakeSession({"https://lichess.org/": (401, "")})
    with pytest.raises(sources.SourceError) as err:
        get(session, "lichess", "https://lichess.org/api/user/alice")
    assert str(err.value) == sources.LICHESS_TOKEN_REFUSED
    assert TOKEN not in str(err.value)


def test_a_401_without_a_token_is_a_plain_http_error(no_token):
    session = FakeSession({"https://lichess.org/": (401, "")})
    with pytest.raises(sources.SourceError, match="HTTP 401"):
        get(session, "lichess", "https://lichess.org/api/user/alice")


def month(session):
    return asyncio.run(sources.month_games(session, "lichess", "alice", "2026-11"))


def test_a_refused_export_for_an_existing_account_is_not_no_account(no_token):
    session = FakeSession({
        "https://lichess.org/api/games/user/": (404, ""),
        "https://lichess.org/api/user/": (200, json.dumps({"username": "alice"})),
    })
    with pytest.raises(sources.SourceError) as err:
        month(session)
    assert not isinstance(err.value, sources.NoSuchUser)
    assert str(err.value) == sources.LICHESS_EXPORT_REFUSED


def test_an_account_that_really_is_gone_is_still_no_account(no_token):
    session = FakeSession({"https://lichess.org/api/games/user/": (404, "")})
    with pytest.raises(sources.NoSuchUser):
        month(session)


def test_an_export_with_the_token_reads_the_games(token):
    session = FakeSession({"https://lichess.org/api/games/user/": (200, "")})
    assert month(session) == []
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_the_token_never_reaches_the_log(token, caplog):
    caplog.set_level(logging.DEBUG)
    session = FakeSession({"https://lichess.org/": (401, "")})
    with pytest.raises(sources.SourceError):
        month(session)
    assert TOKEN not in caplog.text
