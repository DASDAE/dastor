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
        requests), `user:*` for every user, `user:<name>`, or `group:<name>`.
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


class RawConfig(_Model):
    """
    Raw data served as level 0 of the recordings it belongs to.

    Its rows join a recording through `links`, a table (Parquet or CSV) of
    `source_path` (relative to `path`) and the recording's
    `acquisition_key`, `network` and `station`. Rows it does not name are
    not served. Prototype: the access rules are checked against the raw
    patches' own attributes.

    Parameters
    ----------
    path
        The raw data directory.
    index
        Its DASCore index, when not in the directory.
    links
        The table joining raw files to recordings.
    update
        Index the directory again at start; off, the index is read as it is
        (build it beforehand: a large remote directory takes hours).
    max_view_seconds
        The most time one screen of raw data may span. Raw files are often
        laid out so that any window reads every channel of its time span,
        whatever its distance; wider than this, a coarser level stands in.
    """

    path: Path
    index: Path | None = None
    links: Path
    update: bool = False
    max_view_seconds: float | None = Field(default=None, gt=0)


class SpoolConfig(_Model):
    """
    The served spool: a directory of DAS files and who may read them.

    Datasets share the directory and are told apart by their attributes or
    paths (`source_path` is a column rules can match too).

    Parameters
    ----------
    path
        The data directory, indexed recursively. None only when the app is
        handed a spool instead (see `dastor.view`).
    index
        Where the directory's DASCore index lives, when not in the
        directory itself (e.g. a read-only data mount).
    rules
        Access rules. A file is visible to a user only when every row of
        it matches a rule granting that user; with no rules, nothing is.
    refresh_seconds
        How often to re-index the directory; None to index only at start.
    raw
        Raw data served as level 0 of the recordings (see `RawConfig`).
    """

    path: Path | None = None
    index: Path | None = None
    rules: list[Rule] = []
    refresh_seconds: float | None = Field(default=None, gt=0)
    raw: RawConfig | None = None


class User(_Model):
    """
    A user of the server, with an API key or none.

    Only the SHA-256 hex digest of a key is stored; `dastor new-key` makes a
    key and its digest. A user with no key signs in by name alone, and only
    on a trusting server (see `Settings.trusting`).
    """

    name: str
    key_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = (
        None
    )
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
    # Processes which load and reduce viewer tiles, beside the server's: a
    # tile's work holds Python's lock, so threads alone run tiles one at a
    # time. 0 does the work in the server's process.
    tile_workers: int = Field(default=0, ge=0)


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


class MapConfig(_Model):
    """
    The viewer's map of the fibre.

    The fibre itself is drawn from the DASCore inventory in the data
    directory; nothing here describes it.

    Parameters
    ----------
    layers
        A directory of GeoJSON files drawn behind the fibre (geology, roads,
        a site plan): background only. Each feature may state `fill`,
        `stroke` and `name` in its properties.
    basemap_url
        A web map's tile address with `{z}`, `{x}`, `{y}` (and optionally
        `{s}`). Off unless set: with it the page can load tiles from that
        site, the one case in which it reaches the internet.
    basemap_url_dark
        The same, for the dark theme.
    basemap_attribution
        The credit the tiles' terms ask for, shown on the map.
    """

    layers: Path | None = None
    basemap_url: str | None = None
    basemap_url_dark: str | None = None
    basemap_attribution: str = ""

    @field_validator("layers")
    @classmethod
    def _a_directory(cls, value: Path | None) -> Path | None:
        """Refuse a layers path which is not a directory."""
        if value is not None and not value.is_dir():
            raise ValueError(f"layers {str(value)!r} is not a directory")
        return value

    @field_validator("basemap_url", "basemap_url_dark")
    @classmethod
    def _a_tile_address(cls, value: str | None, info) -> str | None:
        """Refuse an address the page could not make tile addresses from."""
        if value is None:
            return value
        if not value.startswith(("https://", "http://")):
            msg = f"{info.field_name} {value!r} must start with https:// or http://"
            raise ValueError(msg)
        if missing := [x for x in ("{z}", "{x}", "{y}") if x not in value]:
            msg = f"{info.field_name} {value!r} lacks {' '.join(missing)}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _dark_with_light(self) -> MapConfig:
        """Refuse a dark web map without the one it stands in for."""
        if self.basemap_url_dark and not self.basemap_url:
            raise ValueError("basemap_url_dark needs basemap_url")
        return self


class Settings(_Model):
    """The whole server configuration."""

    spool: SpoolConfig
    users: list[User] = []
    limits: Limits = Limits()
    annotations: AnnotationConfig | None = None
    map: MapConfig = MapConfig()
    # The pages are served over HTTPS, perhaps by a proxy which passes them
    # on over HTTP: the sign-in cookie is then always `Secure` and `__Host-`.
    https: bool = False
    # Sign-in lists the users, and anyone may sign in as one with no key, or
    # under a new name: for a lab's private network, not a public server.
    # A user with a key still needs it.
    trusting: bool = True

    @field_validator("users")
    @classmethod
    def _unique_users(cls, value: list[User]) -> list[User]:
        """Refuse duplicate user names or keys, which would be ambiguous."""
        if any(x.name == "*" for x in value):
            raise ValueError("no user may be named '*': 'user:*' grants every user")
        for field in ("name", "key_sha256"):
            seen = [y for x in value if (y := getattr(x, field)) is not None]
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
        if (
            self.annotations.anonymous_owner
            and self.users
            and not self.open_to_visitors
        ):
            msg = (
                "anonymous_owner: no rule grants visitors ('*'), so with users "
                "they are turned away and can save nothing"
            )
            raise ValueError(msg)
        return self

    @property
    def open_to_visitors(self) -> bool:
        """
        Return True if some rule grants anonymous requests (`*`).

        A server open to no visitors turns them away before any work: its
        API answers only signed-in requests, and its viewer sends them to
        sign in.
        """
        return any("*" in rule.allow for rule in self.spool.rules)

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        """Load settings from a YAML file."""
        with open(path) as fi:
            return cls.model_validate(yaml.safe_load(fi))
