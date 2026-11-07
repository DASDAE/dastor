"""Identify who is making a request."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable
from dataclasses import dataclass

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
