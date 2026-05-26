"""Round-trip tests for the cowork:// invite URL format."""

from __future__ import annotations

import pytest

from cowork.client.invite import InviteParseError, format_invite, parse_invite


def test_round_trip_http() -> None:
    url = format_invite("http://127.0.0.1:8765", "abcDEF123")
    assert url == "cowork://127.0.0.1:8765#abcDEF123"
    server, token = parse_invite(url)
    assert server == "http://127.0.0.1:8765"
    assert token == "abcDEF123"


def test_round_trip_https() -> None:
    url = format_invite("https://cowork.example.com", "tkn-xyz_-")
    assert url == "cowork+https://cowork.example.com#tkn-xyz_-"
    server, token = parse_invite(url)
    assert server == "https://cowork.example.com"
    assert token == "tkn-xyz_-"


def test_round_trip_with_path() -> None:
    url = format_invite("https://example.com/api", "T")
    server, token = parse_invite(url)
    assert server == "https://example.com/api"
    assert token == "T"


def test_strips_trailing_slash() -> None:
    url = format_invite("http://localhost:1234/", "tok")
    server, _ = parse_invite(url)
    assert server == "http://localhost:1234"


@pytest.mark.parametrize(
    "bad",
    [
        "http://example.com#tok",        # wrong scheme
        "cowork://example.com",           # missing token
        "cowork+https:///path#tok",       # missing host
        "not even a url",
        "",
    ],
)
def test_parse_rejects_garbage(bad: str) -> None:
    with pytest.raises(InviteParseError):
        parse_invite(bad)


def test_format_rejects_empty_token() -> None:
    with pytest.raises(InviteParseError):
        format_invite("http://x", "")
