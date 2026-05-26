"""Cowork invite URL format.

We encode the (server_url, invite_token) pair into a single string that
recipients can paste into the TUI with `/join <invite>` (no need to type the
server URL and token separately). Two formats are accepted:

  cowork://<host>[:port][/path]#<token>             (https → cowork+https://)
  cowork+https://<host>[:port][/path]#<token>

The host/path/scheme tells the client where to reach the server; the token
sits in the URL fragment so it never gets shipped to HTTP servers if pasted
into a browser by mistake.

A bare token + explicit server URL is still accepted by /join so old links
keep working.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


class InviteParseError(ValueError):
    pass


def format_invite(server_url: str, token: str) -> str:
    """Render the canonical cowork:// URL for a given server + invite token."""
    if not token:
        raise InviteParseError("empty invite token")
    parsed = urlparse(server_url.rstrip("/"))
    if parsed.scheme == "https":
        scheme = "cowork+https"
    else:
        scheme = "cowork"
    return urlunparse((scheme, parsed.netloc, parsed.path, "", "", token))


def parse_invite(invite: str) -> tuple[str, str]:
    """Decode a cowork:// (or cowork+https://) URL into (server_url, token).

    Raises InviteParseError if the input isn't a valid cowork invite URL.
    """
    parsed = urlparse(invite.strip())
    if parsed.scheme not in {"cowork", "cowork+https"}:
        raise InviteParseError(
            f"not a cowork invite URL (got scheme {parsed.scheme!r})"
        )
    if not parsed.netloc:
        raise InviteParseError("invite URL missing host")
    token = parsed.fragment
    if not token:
        raise InviteParseError("invite URL missing token (use cowork://host#TOKEN)")
    http_scheme = "https" if parsed.scheme == "cowork+https" else "http"
    server_url = urlunparse((http_scheme, parsed.netloc, parsed.path, "", "", ""))
    return server_url, token
