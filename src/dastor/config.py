"""Server configuration, loaded from a YAML file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# Spool names appear in URLs, so keep them to URL-safe characters.
SpoolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+$")]

_ALLOW_PATTERN = re.compile(r"^(\*|user:.+|group:.+)$")


class _Model(BaseModel):
    """Base model which rejects unknown keys, so typos in config fail loudly."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Rule(_Model):
    """
    Grant access to the files whose rows match a pattern.

    Parameters
    ----------
    column
        A column of the spool contents, such as `tag` or `station`.
    pattern
        A regular expression searched for in the column's text (use `^` and
        `$` to anchor it).
    allow
        Who the rule grants: `*` for everyone (including anonymous
        requests), `user:<name>`, or `group:<name>`.
    """

    column: str
    pattern: str
    allow: list[str] = Field(min_length=1)

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        """Fail at load time on an invalid regular expression."""
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid pattern {value!r}: {exc}") from None
        return value

    @field_validator("allow")
    @classmethod
    def _known_grants(cls, value: list[str]) -> list[str]:
        """Accept only `*`, `user:<name>` and `group:<name>`."""
        bad = [x for x in value if not _ALLOW_PATTERN.match(x)]
        if bad:
            msg = f"allow entries must be '*', 'user:<name>' or 'group:<name>': {bad}"
            raise ValueError(msg)
        return value


class SpoolConfig(_Model):
    """
    One served spool: a directory of DAS files and who may read them.

    Parameters
    ----------
    path
        The data directory.
    index
        Where the directory's DASCore index lives, when not in the
        directory itself (e.g. a read-only data mount).
    rules
        Access rules. A file is visible to a user only when every row of
        it matches a rule granting that user; with no rules, nothing is.
    refresh_seconds
        How often to re-index the directory; None to index only at start.
    """

    path: Path
    index: Path | None = None
    rules: list[Rule] = []
    refresh_seconds: float | None = Field(default=None, gt=0)


class User(_Model):
    """
    An API key holder.

    Only the SHA-256 hex digest of the key is stored; `dastor new-key`
    makes a key and its digest.
    """

    name: str
    key_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    groups: list[str] = []


class Limits(_Model):
    """Request limits that protect the server."""

    # An estimate of the uncompressed array bytes one data request returns.
    max_request_bytes: int = Field(default=2 * 1024**3, gt=0)
    # Patches loading at once across all requests.
    max_concurrent_loads: int = Field(default=4, gt=0)


class Settings(_Model):
    """The whole server configuration."""

    spools: dict[SpoolName, SpoolConfig]
    users: list[User] = []
    limits: Limits = Limits()

    @field_validator("users")
    @classmethod
    def _unique_users(cls, value: list[User]) -> list[User]:
        """Refuse duplicate user names or keys, which would be ambiguous."""
        for field in ("name", "key_sha256"):
            seen = [getattr(x, field) for x in value]
            if len(seen) != len(set(seen)):
                raise ValueError(f"duplicate user {field}")
        return value

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        """Load settings from a YAML file."""
        with open(path) as fi:
            return cls.model_validate(yaml.safe_load(fi))
