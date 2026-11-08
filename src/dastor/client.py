"""Open a dastor server's spool as a DASCore spool."""

from __future__ import annotations

import tempfile
import urllib.parse
import urllib.request
import uuid
from dataclasses import replace
from pathlib import Path

import dascore as dc
import numpy as np
from dascore.core.spool import Spool
from dascore.io.index.backend import get_backend
from dascore.io.index.catalog import _DIRECTORY_ORDER, PatchCatalog, PatchResolver

from dastor.wire import decode_patch


class Client:
    """
    A dastor server.

    Parameters
    ----------
    url
        The server's base URL, e.g. ``"https://das.example.org"``.
    key
        An API key, or None to read as anonymous.
    timeout
        Seconds to wait for each response.
    """

    def __init__(self, url: str, key: str | None = None, timeout: float = 60):
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout

    def _get(self, path: str) -> bytes:
        """Return the body of a GET request, raising for error statuses."""
        request = urllib.request.Request(self.url + path)
        if self.key is not None:
            request.add_header("Authorization", f"Bearer {self.key}")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def get_patch(
        self,
        data_id: str,
        source_path: str | None = None,
        source_patch_key: str | None = None,
        **bounds,
    ) -> dc.Patch:
        """
        Return one patch by its `data_id`, trimmed on the server.

        `source_path` and `source_patch_key` pick one copy of a patch stored
        more than once. `bounds` map coordinates to `(min, max)` ranges,
        either end None, e.g. ``time=(t1, None)``; the server trims the
        patch before sending.
        """
        path = f"/v1/patch/{urllib.parse.quote(data_id)}"
        picks = {"source_path": source_path, "source_patch_key": source_patch_key}
        params = [(k, v) for k, v in picks.items() if v is not None]
        if query := urllib.parse.urlencode(params + _bound_params(bounds)):
            path += f"?{query}"
        return decode_patch(self._get(path))

    def spool(self) -> dc.BaseSpool:
        """
        Return the patches this client may read as a DASCore spool.

        Selecting, chunking and coverage work on a downloaded index; each
        patch is fetched when it loads, trimmed on the server to the part
        the selection needs.
        """
        # Unique per spool, so spools combined in one catalog keep their
        # own routes (DASCore merges routes by prefix).
        prefix = f"dastor://{uuid.uuid4().hex}/"
        with tempfile.TemporaryDirectory(prefix="dastor-") as folder:
            path = Path(folder) / "index.sqlite"
            path.write_bytes(self._get("/v1/index"))
            backend = get_backend(path, read_only=True)
            try:
                records = backend.export_records()
            finally:
                backend.close()
        # Rows load through the resolver routed by this prefix, which
        # DASCore's chunk plans also follow.
        records = [replace(x, source_path=prefix + x.source_path) for x in records]
        resolver = _RemoteResolver(self, prefix)
        catalog = PatchCatalog(resolver=resolver, default_order=_DIRECTORY_ORDER)
        catalog.backend.write_sources(records)
        df = catalog.to_df()
        keys = zip(df["source_path"], df["source_patch_key"], strict=True)
        resolver.ids = dict(zip(keys, df["data_id"], strict=True))
        out = Spool()
        out._catalog = catalog
        return out


def _bound_params(bounds: dict) -> list[tuple[str, str]]:
    """
    Return query parameters for coordinate ranges.

    Ends that aren't times or real numbers are left out, which only means
    the server sends more of the patch.
    """
    out = []
    for name, ends in bounds.items():
        if not isinstance(ends, tuple | list) or len(ends) != 2:
            continue
        for suffix, value in zip(("_min", "_max"), ends, strict=True):
            if isinstance(value, np.datetime64) and not np.isnat(value):
                out.append((name + suffix, str(value)))
            elif isinstance(value, int | np.integer) and not isinstance(value, bool):
                out.append((name + suffix, str(int(value))))
            elif isinstance(value, float | np.floating) and np.isfinite(value):
                out.append((name + suffix, repr(float(value))))
    return out


class _RemoteResolver(PatchResolver):
    """
    Load a row's patch from the server, trimmed to DASCore's read hints.

    The hints may be wider than the selection; DASCore trims exactly after.

    Rows are matched by source path and patch key, which trimmed rows and
    chunk plan members keep (unlike `data_id`).
    """

    def __init__(self, client: Client, prefix: str):
        self.client = client
        self.prefix = prefix
        self.ids: dict[tuple[str, str], str] = {}

    def resolve_path(self, path):
        """Refuse DASCore's reads of source files, which stay on the server."""
        msg = (
            "dastor spools can't rebuild source coordinates locally, which "
            "selecting a list of ranges after a trimming selection needs; "
            "select the ranges in one call instead"
        )
        raise NotImplementedError(msg)

    def plan_entries(self) -> dict[str, PatchResolver]:
        """Route this server's paths here when DASCore plans a chunk."""
        return {self.prefix: self}

    def resolve(self, row, **trim) -> dc.Patch:
        """Return the patch a row comes from, trimmed to the hints."""
        path, key = str(row["source_path"]), str(row["source_patch_key"])
        source = path.removeprefix(self.prefix)
        data_id = self.ids[(path, key)]
        return self.client.get_patch(data_id, source, key, **trim)
