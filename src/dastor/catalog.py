"""The served spool and each principal's view of it."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass

import dascore as dc
from dascore.io.index.backend import get_backend

from dastor.acl import (
    matching_rows,
    missing_columns,
    patch_matches,
    visible_files,
    whole_acquisitions,
)
from dastor.auth import Principal
from dastor.config import Rule, Settings

log = logging.getLogger(__name__)


class ChangedFileError(RuntimeError):
    """Raised when a file no longer matches the rules that granted it."""


@dataclass(frozen=True)
class View:
    """
    A principal's access to the spool.

    The visible files are fixed when the view is made, from the contents of
    the last refresh; rows are checked again on every selection and loaded
    patches are checked again as they load, so a refresh or a changed file
    can narrow what is served but never widen it.
    """

    spool: dc.BaseSpool
    files: frozenset[str]
    rules: tuple[Rule, ...]
    principal: Principal
    # The acquisition keys ("" for none) all of whose files are visible.
    acquisitions: frozenset[str] = frozenset()

    def select(self, **kwargs) -> dc.BaseSpool:
        """Return the visible patches matching a selection."""
        spool = self.spool.select(**kwargs) if kwargs else self.spool
        contents = spool.get_contents()
        if contents.empty:
            return spool
        in_files = contents["source_path"].astype(str).isin(self.files).to_numpy()
        return spool[in_files & matching_rows(contents, self.rules, self.principal)]

    def export_index(self, path: str) -> None:
        """
        Write a DASCore index at `path` holding only the visible patches.

        It holds their metadata, attributes and coordinates, but no data:
        clients open it to select and plan, then fetch patches by id.
        """
        catalog = self.select()._catalog
        rows = catalog.to_df()["_patch_row"].tolist()
        backend = get_backend(path)
        try:
            backend.write_sources(catalog.backend.export_records(patch_rows=rows))
        finally:
            backend.close()

    def check(self, patch: dc.Patch, row: Mapping) -> dc.Patch:
        """Return a loaded patch, or raise if its file changed to hide it."""
        if not patch_matches(patch, row, self.rules, self.principal):
            path = row.get("source_path")
            raise ChangedFileError(f"{path} changed since it was indexed")
        return patch


class Catalog:
    """
    Open the served spool and hand out access-controlled views.

    The spool is the configured directory's unless one is given, as
    `dastor.view` does with a spool which may hold patches in memory.
    """

    def __init__(self, settings: Settings, spool: dc.BaseSpool | None = None):
        self.config = settings.spool
        if spool is None and self.config.path is None:
            raise ValueError("the config names no spool path")
        self._source = spool
        self._rules = tuple(self.config.rules)
        self._spool: dc.BaseSpool | None = None
        self._seen: dict[Principal, tuple[frozenset[str], frozenset[str]]] = {}
        self._lock = threading.Lock()

    def refresh(self) -> None:
        """
        Index the directory again, if one is served, and forget what was visible.

        Refuses rules naming columns the contents lack.
        """
        cfg = self.config
        # A spool handed in is served as it is: it may be a selection or
        # another server's, which can't be indexed again.
        spool = self._source
        if spool is None:
            spool = dc.spool(cfg.path, index=cfg.index).update()
        contents = spool.get_contents()
        if len(contents) and (missing := missing_columns(contents, self._rules)):
            raise ValueError(f"rules name columns the contents lack: {missing}")
        with self._lock:
            self._spool = spool
            self._seen = {}
        log.info("indexed %d patches", len(contents))

    def view(self, principal: Principal) -> View:
        """
        Return a principal's view of the spool.

        Every request reads through here, so access control has one choke
        point.
        """
        with self._lock:
            spool = self._spool
            assert spool is not None, "refresh() runs before requests are served"
            if (seen := self._seen.get(principal)) is None:
                contents = spool.get_contents()
                files = visible_files(contents, self._rules, principal)
                seen = files, whole_acquisitions(contents, files)
                self._seen[principal] = seen
        return View(spool, seen[0], self._rules, principal, seen[1])
