"""sources.account_name: the site's own spelling of a username."""

import asyncio
import json

import pytest

import sources


@pytest.fixture
def site_says(monkeypatch):
    """Fake the HTTP layer: set state["reply"] (a dict, or an exception to raise)."""
    state = {"reply": {}, "urls": []}

    async def fake_get(session, site, url, username, *, timeout, params=None, ndjson=False):
        state["urls"].append(url)
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return json.dumps(state["reply"])

    monkeypatch.setattr(sources, "_get", fake_get)
    return state


def name_of(site, typed):
    return asyncio.run(sources.account_name(None, site, typed))


def test_chesscom_takes_the_spelling_from_the_end_of_the_profile_url(site_says):
    site_says["reply"] = {"username": "alice_smith", "url": "https://www.chess.com/member/Alice_Smith"}
    assert name_of("chess.com", "ALICE_SMITH") == "Alice_Smith"


def test_chesscom_asks_for_the_lowercase_profile(site_says):
    # The API wants lowercase paths whatever was typed.
    site_says["reply"] = {"url": "https://www.chess.com/member/Alice"}
    name_of("chess.com", "ALICE")
    assert site_says["urls"] == ["https://api.chess.com/pub/player/alice"]


def test_a_trailing_slash_on_the_url_is_ignored(site_says):
    site_says["reply"] = {"url": "https://www.chess.com/member/Alice/"}
    assert name_of("chess.com", "alice") == "Alice"


def test_lichess_takes_the_username_field(site_says):
    site_says["reply"] = {"id": "alice", "username": "Alice"}
    assert name_of("lichess", "aLiCe") == "Alice"


@pytest.mark.parametrize("site", ["chess.com", "lichess"])
def test_an_answer_that_doesnt_match_what_was_typed_keeps_the_typed_spelling(site_says, site):
    site_says["reply"] = {"url": "https://www.chess.com/member/Someone_Else", "username": "Someone_Else"}
    assert name_of(site, "alice") == "alice"


@pytest.mark.parametrize("site", ["chess.com", "lichess"])
def test_a_reply_with_no_name_keeps_the_typed_spelling(site_says, site):
    site_says["reply"] = {}
    assert name_of(site, "Alice") == "Alice"


@pytest.mark.parametrize("site", ["chess.com", "lichess"])
def test_an_unknown_account_is_passed_up(site_says, site):
    site_says["reply"] = sources.NoSuchUser("no such account")
    with pytest.raises(sources.NoSuchUser):
        name_of(site, "ghost")


def test_a_bad_username_is_refused_before_any_request(site_says):
    with pytest.raises(sources.SourceError, match="valid username"):
        name_of("chess.com", "../x")
    assert site_says["urls"] == []


def test_an_unknown_site_is_a_programming_error(site_says):
    with pytest.raises(ValueError):
        name_of("example.org", "alice")
