"""Identify who is making a request."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

from starlette.responses import JSONResponse

from dastor.config import User


@dataclass(frozen=True)
class Principal:
    """Who a request acts for."""

    name: str
    groups: frozenset[str] = frozenset()
    authenticated: bool = False

    def granted(self, allow: Iterable[str]) -> bool:
        """Return True if any entry of a rule's `allow` list names this principal."""
        for entry in allow:
            if entry == "*":
                return True
            kind, _, value = entry.partition(":")
            if not self.authenticated:
                continue
            if (kind == "user" and value == self.name) or (
                kind == "group" and value in self.groups
            ):
                return True
        return False


ANONYMOUS = Principal("anonymous")


class InvalidKeyError(Exception):
    """Raised when a request presents an unknown API key."""


def hash_key(key: str) -> str:
    """Return the SHA-256 hex digest the config stores for a key."""
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> tuple[str, str]:
    """Return a new random API key and its digest."""
    key = secrets.token_urlsafe(32)
    return key, hash_key(key)


class Authenticator:
    """Map `Authorization: Bearer <key>` headers to principals."""

    def __init__(self, users: Iterable[User]):
        self._by_digest = {
            x.key_sha256: Principal(x.name, frozenset(x.groups), True) for x in users
        }

    def __call__(self, authorization: str | None) -> Principal:
        """Return the principal for a header; no header is anonymous."""
        if not authorization:
            return ANONYMOUS
        scheme, _, key = authorization.partition(" ")
        if scheme.lower() != "bearer" or not key.strip():
            raise InvalidKeyError("expected 'Authorization: Bearer <key>'")
        # Keys are random and high entropy, so looking up their digest leaks
        # nothing useful through timing.
        principal = self._by_digest.get(hash_key(key.strip()))
        if principal is None:
            raise InvalidKeyError("unknown API key")
        return principal


class TokenGate:
    """
    Refuse requests of the API which don't carry a token, as Jupyter does.

    A server started for one person (`dastor.view`) makes a random token
    and puts it in the address it hands out. The page sends it back as
    `Authorization: token <token>`, or as `?token=` on a link; `Bearer` is
    accepted too, so `dastor.Client(url, key=token)` reads the same server.
    Without it other users of the machine, and pages of other websites,
    are refused. The token is taken off the request before the app sees it,
    so it is neither an API key nor a selection.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send) -> None:
        """Pass on a request with the token; answer 403 to others of the API."""
        if scope["type"] != "http" or not scope["path"].startswith("/v1/"):
            await self.app(scope, receive, send)
            return
        headers = [(k, v) for k, v in scope["headers"] if k != b"authorization"]
        query = parse_qsl(scope["query_string"].decode(), keep_blank_values=True)
        given = [v.encode() for k, v in query if k == "token"]
        for name, value in scope["headers"]:
            scheme, _, text = value.partition(b" ")
            if name == b"authorization" and scheme.lower() in (b"token", b"bearer"):
                given.append(text.strip())
        if not any(secrets.compare_digest(x, self.token) for x in given):
            detail = {"detail": "this viewer needs the token in its address"}
            await JSONResponse(detail, 403)(scope, receive, send)
            return
        kept = urlencode([(k, v) for k, v in query if k != "token"]).encode()
        scope = {**scope, "headers": headers, "query_string": kept}
        await self.app(scope, receive, send)
