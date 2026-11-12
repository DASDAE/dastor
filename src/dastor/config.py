"""Server configuration, loaded from a YAML file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

_ALLOW_PATTERN = re.compile(r"^(\*|user:.+|group:.+)$")

# Owners and names of annotation sets: plain words, since they name folders.
SET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


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
    The served spool: a directory of DAS files and who may read them.

    Datasets share the directory and are told apart by their attributes or
    paths (`source_path` is a column rules can match too).

    Parameters
    ----------
    path
        The data directory, indexed recursively.
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
    # The most array bytes one viewer tile may read, estimated before loading.
    max_tile_bytes: int = Field(default=64 * 1024**2, gt=0)
    # The largest annotation set which may be uploaded, as a file.
    max_upload_bytes: int = Field(default=64 * 1024**2, gt=0)
    # The most array bytes the viewer's page reads for one screen. The page
    # keeps to it (the server enforces only the tile limit): beyond it a
    # coarser product stands in, and with none the data is only outlined.
    max_view_bytes: int = Field(default=512 * 1024**2, gt=0)


class AnnotationConfig(_Model):
    """
    Where annotation sets are kept, which turns the annotation API on.

    Parameters
    ----------
    path
        A directory outside the data directory; sets are stored in it as
        `<owner>/<name>/`.
    anonymous_owner
        The owner anonymous requests write as (`dastor browse` uses the
        system user). None refuses anonymous writes.
    """

    path: Path
    anonymous_owner: str | None = None

    @field_validator("anonymous_owner")
    @classmethod
    def _plain_owner(cls, value: str | None) -> str | None:
        """Refuse an owner which could not name a folder."""
        if value is not None and not SET_NAME.fullmatch(value):
            raise ValueError(f"anonymous_owner {value!r} is not a plain name")
        return value


class Settings(_Model):
    """The whole server configuration."""

    spool: SpoolConfig
    users: list[User] = []
    limits: Limits = Limits()
    annotations: AnnotationConfig | None = None

    @field_validator("users")
    @classmethod
    def _unique_users(cls, value: list[User]) -> list[User]:
        """Refuse duplicate user names or keys, which would be ambiguous."""
        for field in ("name", "key_sha256"):
            seen = [getattr(x, field) for x in value]
            if len(seen) != len(set(seen)):
                raise ValueError(f"duplicate user {field}")
        return value

    @model_validator(mode="after")
    def _owners(self) -> Settings:
        """
        Refuse users who could not own annotation sets.

        Sets are stored under their owner's name, so with annotations on
        each user's name must be plain, and anonymous requests must not
        write as a user.
        """
        if self.annotations is None:
            return self
        names = [x.name for x in self.users]
        if bad := [x for x in names if not SET_NAME.fullmatch(x)]:
            msg = f"with annotations on, user names must be plain words: {bad}"
            raise ValueError(msg)
        if self.annotations.anonymous_owner in names:
            raise ValueError("anonymous_owner is the name of a user")
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        """Load settings from a YAML file."""
        with open(path) as fi:
            return cls.model_validate(yaml.safe_load(fi))
