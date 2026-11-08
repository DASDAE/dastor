"""Open a dastor server's spool as a DASCore spool."""

from __future__ import annotations

import tempfile
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path

import dascore as dc
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

    def get_patch(self, data_id: str) -> dc.Patch:
        """Return one whole patch by its `data_id`."""
        return decode_patch(self._get(f"/v1/patch/{urllib.parse.quote(data_id)}"))

    def spool(self) -> dc.BaseSpool:
        """
        Return the patches this client may read as a DASCore spool.

        Selecting, chunking and coverage work on a downloaded index; each
        patch is fetched whole when it loads, then trimmed locally.
        """
        prefix = f"dastor://{urllib.parse.urlsplit(self.url).netloc}/"
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


class _RemoteResolver(PatchResolver):
    """
    Load a row's whole patch from the server; trim hints are ignored.

    Rows are matched by source path and patch key, which trimmed rows and
    chunk plan members keep (unlike `data_id`).
    """

    def __init__(self, client: Client, prefix: str):
        self.client = client
        self.prefix = prefix
        self.ids: dict[tuple[str, str], str] = {}

    def plan_entries(self) -> dict[str, PatchResolver]:
        """Route this server's paths here when DASCore plans a chunk."""
        return {self.prefix: self}

    def resolve(self, row, **trim) -> dc.Patch:
        """Return the whole patch a row comes from."""
        key = (str(row["source_path"]), str(row["source_patch_key"]))
        return self.client.get_patch(self.ids[key])
