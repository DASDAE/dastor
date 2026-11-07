"""The served spools and each principal's view of them."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field

import dascore as dc

from dastor.acl import matching_rows, missing_columns, patch_matches, visible_files
from dastor.auth import Principal
from dastor.config import Rule, Settings, SpoolConfig

log = logging.getLogger(__name__)


class UnknownSpoolError(KeyError):
    """Raised for a spool that doesn't exist or the principal can't see."""


class ChangedFileError(RuntimeError):
    """Raised when a file no longer matches the rules that granted it."""


@dataclass
class _Entry:
    """One served spool and the files each principal may see in it."""

    config: SpoolConfig
    spool: dc.BaseSpool | None = None
    files: dict[Principal, frozenset[str]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class View:
    """
    A principal's access to one spool.

    The visible files are fixed when the view is made, from the contents of
    the last refresh; rows are checked again on every selection and loaded
    patches are checked again as they load, so a refresh or a changed file
    can narrow what is served but never widen it.
    """

    spool: dc.BaseSpool
    files: frozenset[str]
    rules: tuple[Rule, ...]
    principal: Principal

    def select(self, **kwargs) -> dc.BaseSpool:
        """Return the visible patches matching a selection."""
        spool = self.spool.select(**kwargs) if kwargs else self.spool
        contents = spool.get_contents()
        if contents.empty:
            return spool
        in_files = contents["source_path"].astype(str).isin(self.files).to_numpy()
        return spool[in_files & matching_rows(contents, self.rules, self.principal)]

    def check(self, patch: dc.Patch, row: Mapping) -> dc.Patch:
        """Return a loaded patch, or raise if its file changed to hide it."""
        if not patch_matches(patch, row, self.rules, self.principal):
            path = row.get("source_path")
            raise ChangedFileError(f"{path} changed since it was indexed")
        return patch


class Catalog:
    """Open the configured spools and hand out access-controlled views."""

    def __init__(self, settings: Settings):
        self._entries = {name: _Entry(cfg) for name, cfg in settings.spools.items()}

    def open(self) -> None:
        """Index and open every spool; refuse rules naming missing columns."""
        for name in self._entries:
            self.refresh(name)

    def refresh(self, name: str) -> None:
        """Re-index a spool's directory and forget what it last made visible."""
        entry = self._entries[name]
        cfg = entry.config
        spool = dc.spool(cfg.path, index=cfg.index).update()
        contents = spool.get_contents()
        if len(contents) and (missing := missing_columns(contents, cfg.rules)):
            msg = f"spool {name!r} rules name columns its contents lack: {missing}"
            raise ValueError(msg)
        with entry.lock:
            entry.spool = spool
            entry.files = {}
        log.info("spool %s: %d patches", name, len(contents))

    def names(self, principal: Principal) -> list[str]:
        """Return the spools any rule grants the principal, sorted."""
        return sorted(
            name
            for name, entry in self._entries.items()
            if any(principal.granted(x.allow) for x in entry.config.rules)
        )

    def refresh_intervals(self) -> dict[str, float]:
        """Return the spools which refresh periodically, with their periods."""
        return {
            name: x.config.refresh_seconds
            for name, x in self._entries.items()
            if x.config.refresh_seconds is not None
        }

    def view(self, name: str, principal: Principal) -> View:
        """
        Return a principal's view of a spool.

        Every request reads through here, so access control has one choke
        point. A spool no rule grants the principal is reported as unknown,
        so its existence isn't revealed.
        """
        if name not in self.names(principal):
            raise UnknownSpoolError(name)
        entry = self._entries[name]
        rules = tuple(entry.config.rules)
        with entry.lock:
            spool = entry.spool
            assert spool is not None, "open() runs before requests are served"
            if (files := entry.files.get(principal)) is None:
                contents = spool.get_contents()
                files = entry.files[principal] = visible_files(
                    contents, rules, principal
                )
        return View(spool, files, rules, principal)
