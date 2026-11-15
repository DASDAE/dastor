"""The served spool and each principal's view of it."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass

import dascore as dc
import dascore.exceptions as dce
import numpy as np
import pandas as pd
from dascore.io.index.backend import get_backend

from dastor.acl import (
    matching_rows,
    missing_columns,
    patch_matches,
    visible_files,
    whole_acquisitions,
)
from dastor.auth import Principal
from dastor.config import RawConfig, Rule, Settings
from dastor.query import QueryError
from dastor.tiles import product_rows

# The tag raw rows take: the product the viewer offers as level 0.
RAW_TAG = "raw DAS"
_LINKED = ("acquisition_key", "network", "station")


def read_links(config: RawConfig) -> pd.DataFrame:
    """
    Return the links table, indexed by `source_path`.

    Its columns are text, read as written (a station "0012" stays so, a
    blank stays blank); a row naming no file or no acquisition is refused.
    """
    path, columns = str(config.links), ("source_path", *_LINKED)
    if path.endswith(".parquet"):
        table = pd.read_parquet(path)
    else:
        table = pd.read_csv(path, dtype=str, keep_default_na=False)
    if missing := [x for x in columns if x not in table.columns]:
        raise ValueError(f"{path} lacks the columns {missing}")
    table = table[list(columns)].fillna("").astype(str)
    if (table["source_path"] == "").any() or (table["acquisition_key"] == "").any():
        raise ValueError(f"{path} has a row naming no file or no acquisition_key")
    return table.drop_duplicates("source_path").set_index("source_path")


def linked(contents: pd.DataFrame, links: pd.DataFrame) -> np.ndarray:
    """Return a mask of the raw rows whose files `links` names."""
    return contents["source_path"].astype(str).isin(links.index).to_numpy()


def link(contents: pd.DataFrame, links: pd.DataFrame) -> pd.DataFrame:
    """
    Return raw contents joined to their recordings.

    Rows `links` names take its attributes and the raw tag; the others are
    dropped. The joined attributes say which product a row is in, never who
    may read it: access is judged on the raw files' own attributes.
    """
    out = contents.loc[linked(contents, links)].copy()
    paths = out["source_path"].astype(str)
    for name in _LINKED:
        out[name] = paths.map(links[name]).to_numpy()
    out["tag"] = RAW_TAG
    return out


log = logging.getLogger(__name__)


class ChangedFileError(RuntimeError):
    """Raised when a file no longer matches the rules that granted it."""


class TooLargeError(Exception):
    """Raised when loaded patches exceed the request limit."""


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
    # Raw data as level 0 (see `RawConfig`): its spool, links and visible files.
    raw: dc.BaseSpool | None = None
    links: pd.DataFrame | None = None
    raw_files: frozenset[str] = frozenset()

    def select(self, keep=None, /, **kwargs) -> dc.BaseSpool:
        """
        Return the visible patches matching a selection.

        `keep`, given the selection's contents, returns a mask of the rows
        wanted besides (a product, say): applied with the access mask, the
        selection is narrowed once.
        """
        spool = self.spool.select(**kwargs) if kwargs else self.spool
        contents = spool.get_contents()
        if contents.empty:
            return spool
        in_files = contents["source_path"].astype(str).isin(self.files).to_numpy()
        mask = in_files & matching_rows(contents, self.rules, self.principal)
        if keep is not None:
            mask &= keep(contents)
        return spool[mask]

    def products(self, wanted: list[dict], **kwargs) -> dc.BaseSpool:
        """
        Return the visible patches of the products `wanted` matching a selection.

        Products are those of `/v1/viewer/products` (none: every patch). The
        attributes they share go into the selection, so the index narrows
        before any rows are read; dims, steps and the other attributes are
        matched on what is left. Level 0 is read from the raw data, whose rows
        carry their recording only through the links.
        """

        def rows_of(chosen):
            def keep(contents):
                mask = np.zeros(len(contents), dtype=bool)
                for product in chosen:
                    mask |= product_rows(contents, product)
                return mask

            return keep

        raw = [w for w in wanted if w["attrs"].get("tag") == RAW_TAG]
        rest = [w for w in wanted if w["attrs"].get("tag") != RAW_TAG]
        try:
            parts = []
            if raw and self.raw is not None:
                parts.append(self.select_raw(rows_of(raw), **kwargs))
            if rest or not wanted:
                shared = {}
                if rest:
                    first = rest[0]["attrs"]
                    shared = {
                        k: v
                        for k, v in first.items()
                        if all(w["attrs"].get(k) == v for w in rest)
                    }
                kwargs = {**shared, **kwargs}
                # the products narrow the selection with the access check, once
                parts.append(self.select(rows_of(rest) if rest else None, **kwargs))
            if not parts:  # raw asked for where none is served
                parts.append(self.select(lambda c: np.zeros(len(c), dtype=bool)))
        except (TypeError, ValueError) as exc:
            # e.g. a malformed time, or text bounding a numeric coordinate
            raise QueryError(str(exc)) from exc
        spool = parts[0]
        for part in parts[1:]:
            spool = spool + part
        return spool

    def checked(self, spool, limit: int):
        """
        Yield a spool's patches, checking each against the rules and the limit.

        A patch whose file changed to fall outside the rules, or patches whose
        real size passes the limit the estimate missed, end the response.
        """
        total = 0
        for index, row in enumerate(spool.get_contents().to_dict("records")):
            try:
                patch = self.check(spool[index], row)
            except dce.MissingPatchError as exc:
                # The file no longer holds the indexed patch.
                msg = f"{row.get('source_path')} changed since it was indexed"
                raise ChangedFileError(msg) from exc
            total += int(np.prod(patch.shape)) * np.dtype(patch.dtype).itemsize
            if total > limit:
                msg = f"request returns more than the limit of {limit} bytes"
                raise TooLargeError(msg)
            yield patch

    def raw_contents(self) -> pd.DataFrame:
        """Return the visible raw rows, joined to their recordings."""
        if self.raw is None:
            return pd.DataFrame()
        contents = link(self.raw.get_contents(), self.links)
        return contents[contents["source_path"].astype(str).isin(self.raw_files)]

    def select_raw(self, keep, **kwargs) -> dc.BaseSpool:
        """
        Return the visible raw patches of the rows `keep` picks.

        `kwargs` (coordinate bounds) narrow the index first; `keep` is then
        given the rows joined to their recordings, and returns a mask.
        """
        assert self.raw is not None, "only a raw product routes here"
        spool = self.raw.select(**kwargs) if kwargs else self.raw
        contents = spool.get_contents()
        if contents.empty:
            return spool
        rows = linked(contents, self.links)
        joined = link(contents, self.links)
        own = contents.loc[rows]  # the rows as the files state them, for the rules
        visible = own["source_path"].astype(str).isin(self.raw_files).to_numpy()
        allowed = matching_rows(own, self.rules, self.principal)
        mask = np.zeros(len(contents), dtype=bool)
        mask[np.flatnonzero(rows)] = visible & allowed & keep(joined)
        return spool[mask]

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
        self._raw: dc.BaseSpool | None = None
        self._links: pd.DataFrame | None = None
        self._seen: dict[Principal, tuple] = {}
        self.generation = 0
        # told the new generation after each refresh (the tile workers' signal)
        self.listeners: list = []
        self._lock = threading.Lock()

    def refresh(self, update: bool = True) -> None:
        """
        Index the directory again, if one is served, and forget what was visible.

        Without `update`, the index is read as it is (a tile worker does so,
        the server keeping it current). Refuses rules naming columns the
        contents lack.
        """
        cfg = self.config
        # A spool handed in is served as it is: it may be a selection or
        # another server's, which can't be indexed again.
        spool = self._source
        if spool is None:
            spool = dc.spool(cfg.path, index=cfg.index)
            spool = spool.update() if update else spool
        contents = spool.get_contents()
        if len(contents) and (missing := missing_columns(contents, self._rules)):
            raise ValueError(f"rules name columns the contents lack: {missing}")
        raw = links = None
        if (rc := cfg.raw) is not None:
            raw = dc.spool(rc.path, index=rc.index)
            raw = raw.update() if rc.update and update else raw
            links = read_links(rc)
            log.info(
                "raw: %d patches, %d linked files", len(raw.get_contents()), len(links)
            )
        with self._lock:
            self._spool, self._raw, self._links = spool, raw, links
            self._seen = {}
            self.generation += 1  # what was worked out from the last contents is stale
            generation = self.generation
        for listener in self.listeners:
            listener(generation)
        log.info("indexed %d patches", len(contents))

    def view(self, principal: Principal) -> View:
        """
        Return a principal's view of the spool.

        Every request reads through here, so access control has one choke
        point.
        """
        with self._lock:  # one generation: the spools, links and what they show
            spool, raw, links = self._spool, self._raw, self._links
            assert spool is not None, "refresh() runs before requests are served"
            if (seen := self._seen.get(principal)) is None:
                seen = self._visible(spool, raw, links, principal)
                self._seen[principal] = seen
        files, acquisitions, raw_files = seen
        return View(
            spool,
            files,
            self._rules,
            principal,
            acquisitions,
            raw=raw,
            links=links,
            raw_files=raw_files,
        )

    def _visible(self, spool, raw, links, principal: Principal) -> tuple:
        """
        Return what a principal may read: files, acquisitions, raw files.

        An acquisition is theirs to read about when every file of it is,
        the raw files linked to it among them.
        """
        contents = spool.get_contents()
        files = visible_files(contents, self._rules, principal)
        if raw is None:
            return files, whole_acquisitions(contents, files), frozenset()
        rows = raw.get_contents()
        own = rows.loc[linked(rows, links)]  # judged on the files' own attributes
        raw_files = visible_files(own, self._rules, principal)
        # a raw file belongs to the recording it is linked to (kept apart by a prefix)
        joined = link(rows, links)
        both = pd.concat(
            [
                contents[["source_path", "acquisition_key"]]
                if "acquisition_key" in contents
                else contents[["source_path"]].assign(acquisition_key=""),
                pd.DataFrame(
                    {
                        "source_path": "raw:" + joined["source_path"].astype(str),
                        "acquisition_key": joined["acquisition_key"].to_numpy(),
                    }
                ),
            ],
            ignore_index=True,
        )
        seen = files | {f"raw:{x}" for x in raw_files}
        return files, whole_acquisitions(both, seen), raw_files
