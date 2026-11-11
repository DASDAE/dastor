"""The dastor HTTP API."""

import asyncio
import contextlib
import gzip
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path as FilePath
from typing import Annotated, Literal

import anyio
import dascore as dc
import dascore.exceptions as dce
import numpy as np
import pandas as pd
import pyarrow as pa
from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from dastor import __version__
from dastor.annotations import (
    AnnotationError,
    AnnotationStore,
    MissingSetError,
    RevisionConflictError,
)
from dastor.auth import Authenticator, InvalidKeyError, Principal
from dastor.catalog import Catalog, ChangedFileError, View
from dastor.config import Settings
from dastor.query import QueryError, selection
from dastor.tiles import (
    Stat,
    decode_product,
    encode_tile,
    list_products,
    product_axes,
    product_rows,
    render_tile,
    tile_range,
)
from dastor.wire import FRAMED_MEDIA_TYPE, PATCH_MEDIA_TYPE, encode_patch, frame

log = logging.getLogger(__name__)

# Query parameters the endpoints use themselves rather than select on.
_RESERVED = frozenset({"format", "dim", "product"})

# The viewer's page, scripts and styles.
_STATIC = FilePath(__file__).parent / "static"

# DASCore errors that mean the request asked for something invalid.
_CLIENT_ERRORS = (
    dce.ParameterError,
    dce.InvalidSpoolQueryError,
    dce.InvalidTimeRangeError,
    dce.TimeError,
    dce.UnitError,
    dce.UnknownCoordinateError,
    dce.CoordError,
)


def _numbers(column: pd.Series) -> np.ndarray:
    """Return a contents column as floats (times in ns), NaN where it has none."""
    if column.dtype.kind in "Mm":
        return np.where(column.isna(), np.nan, column.astype("int64").astype(float))
    if column.dtype.kind in "iufb":
        return column.to_numpy(dtype=float, na_value=np.nan)
    # Mixed columns (dims which are times in some files, numbers in others)
    out = np.full(len(column), np.nan)
    for number, value in enumerate(column):
        if isinstance(value, pd.Timestamp | pd.Timedelta):
            out[number] = value.value
        elif isinstance(value, int | float | np.number) and not pd.isna(value):
            out[number] = float(value)
    return out


def estimated_bytes(contents: pd.DataFrame) -> int:
    """
    Estimate the array bytes the rows of a selected spool hold.

    Each row's samples are counted from its dims' bounds and steps; a dim
    without a usable step falls back to the row's stored size, which a
    trimmed row may lack. The estimate rejects requests before any loading;
    the limit is enforced again on the patches as they load.
    """
    if contents.empty:
        return 0
    count = np.ones(len(contents))
    counted = np.ones(len(contents), dtype=bool)
    dims = contents["dims"].astype("string").fillna("").astype(str)
    for names, rows in dims.groupby(dims.to_numpy(), sort=False).indices.items():
        for dim in names.split(","):
            columns = [f"{dim}_min", f"{dim}_max", f"{dim}_step"]
            if not set(columns) <= set(contents.columns):
                counted[rows] = False
                break
            low, high, step = (_numbers(contents[x].iloc[rows]) for x in columns)
            with np.errstate(invalid="ignore", divide="ignore"):
                samples = np.round((high - low) / step) + 1
            usable = np.isfinite(samples) & (samples >= 1)
            count[rows] *= np.where(usable, samples, 1)
            counted[rows] &= usable
    if not counted.all():
        stored = contents.get("data_size", pd.Series(0, index=contents.index))
        stored = pd.to_numeric(stored, errors="coerce").fillna(0).to_numpy(dtype=float)
        count = np.where(counted, count, stored)
    kinds = contents.get("dtype", pd.Series("float64", index=contents.index))
    kinds = kinds.astype("string").fillna("").astype(str)
    sizes = {x: np.dtype(x or "float64").itemsize for x in kinds.unique()}
    return int((count * kinds.map(sizes).to_numpy(dtype=float)).sum())


def _json_frame(df: pd.DataFrame) -> Response:
    """Return a frame as JSON records: times in ISO 8601, durations in seconds."""
    df = df.copy()
    for column in df.columns:
        if df[column].dtype.kind == "m":
            df[column] = df[column].dt.total_seconds()
    body = df.to_json(orient="records", date_format="iso", date_unit="ns")
    return Response(body, media_type="application/json")


def _arrow_frame(df: pd.DataFrame) -> Response:
    """Return a frame as an Arrow IPC stream."""
    table = pa.Table.from_pandas(df, preserve_index=False)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return Response(sink.getvalue().to_pybytes(), media_type=PATCH_MEDIA_TYPE)


def _shown(df: pd.DataFrame) -> pd.DataFrame:
    """
    Drop the columns empty (null or "") in a caller's rows.

    The index has a column for every attribute of every file, so columns
    only hidden files fill would otherwise reveal their attribute names.
    """
    filled = df.notna() & df.astype("string").ne("").fillna(True)
    return df.loc[:, filled.any().to_numpy()]


def _frame_response(df: pd.DataFrame, format: str) -> Response:
    """Return a frame's shown columns in the requested table format."""
    df = _shown(df)
    return _arrow_frame(df) if format == "arrow" else _json_frame(df)


def _json_value(value):
    """Return a contents value as JSON: times in ISO 8601, durations in seconds."""
    if isinstance(value, np.datetime64 | pd.Timestamp):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.timedelta64 | pd.Timedelta):
        return pd.Timedelta(value).total_seconds()
    return value.item() if isinstance(value, np.generic) else value


def summarize(df: pd.DataFrame) -> dict:
    """
    Summarize spool contents: counts, columns, dims and each dim's extent.

    Each dim has one extent per unit its patches use, since bounds in
    different units can't be compared. Extents span the patches' bounds, so
    they say nothing about gaps.
    """
    df = _shown(df)
    dims = sorted({str(x) for x in df.get("dims", ())})
    extents = {}
    for dim in sorted({y for x in dims for y in x.split(",") if y}):
        names = [f"{dim}_min", f"{dim}_max", f"{dim}_units"]
        if not set(names[:2]) <= set(df.columns):
            continue
        rows = df.reindex(columns=names)
        units = rows[names[2]].astype("string")
        extents[dim] = [
            {
                "units": None if pd.isna(unit) else str(unit),
                "min": _json_value(group[names[0]].min()),
                "max": _json_value(group[names[1]].max()),
            }
            for unit, group in rows.groupby(units, dropna=False, sort=True)
        ]
    return {
        "patches": len(df),
        "files": int(df["source_path"].nunique()) if len(df) else 0,
        "bytes": estimated_bytes(df),
        "columns": list(df.columns),
        "dims": [x.split(",") for x in dims],
        "extents": extents,
    }


async def _refresh_forever(catalog: Catalog, seconds: float) -> None:
    """Re-index the spool every `seconds`, keeping the old index if that fails."""
    while True:
        await asyncio.sleep(seconds)
        try:
            await anyio.to_thread.run_sync(catalog.refresh)
        except Exception:
            log.exception("refreshing failed; still serving the last index")


def create_app(settings: Settings) -> FastAPI:
    """Return the dastor app for a configuration."""
    catalog = Catalog(settings)
    authenticate = Authenticator(settings.users)
    loads = threading.BoundedSemaphore(settings.limits.max_concurrent_loads)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await anyio.to_thread.run_sync(catalog.refresh)
        seconds = settings.spool.refresh_seconds
        task = seconds and asyncio.create_task(_refresh_forever(catalog, seconds))
        yield
        if task:
            task.cancel()

    app = FastAPI(title="dastor", version=__version__, lifespan=lifespan)
    app.state.catalog = catalog

    def principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Principal:
        try:
            return authenticate(authorization)
        except InvalidKeyError as exc:
            headers = {"WWW-Authenticate": "Bearer"}
            raise HTTPException(401, str(exc), headers=headers) from None

    Who = Annotated[Principal, Depends(principal)]
    TableFormat = Annotated[Literal["json", "arrow"], Query()]

    def selected_with_view(
        request, who, reserved=_RESERVED, **extra
    ) -> tuple[View, dc.BaseSpool]:
        """
        Return a principal's view and its patches matching the query.

        `product=<id>` (from `/v1/viewer/products`) keeps one product. Its
        attributes go into the selection, so the index narrows to it before
        any rows are read; its dims and steps are matched on what is left.
        """
        kwargs = selection(request.query_params.multi_items(), reserved)
        product = request.query_params.get("product")
        wanted = decode_product(product) if product else None
        attrs = wanted["attrs"] if wanted else {}
        kwargs.update({k: v for k, v in attrs.items() if k not in kwargs})
        view = catalog.view(who)
        try:
            spool = view.select(**kwargs, **extra)
        except (TypeError, ValueError) as exc:
            # e.g. a malformed time, or text bounding a numeric coordinate
            raise QueryError(str(exc)) from exc
        if wanted and len(contents := spool.get_contents()):
            spool = spool[product_rows(contents, wanted)]
        return view, spool

    def selected(request: Request, who: Principal) -> dc.BaseSpool:
        """Return the visible patches matching the query."""
        return selected_with_view(request, who)[1]

    @app.exception_handler(QueryError)
    async def _bad_query(request: Request, exc: QueryError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    for error in _CLIENT_ERRORS:
        app.add_exception_handler(error, _bad_query)

    @app.get("/health")
    def health() -> dict:
        """Report that the server is up."""
        return {"status": "ok", "version": __version__}

    @app.get("/v1/summary")
    def summary(request: Request, who: Who) -> dict:
        """
        Summarize the patches matching the query.

        With no query, that is all the caller may read. Gives counts, an
        estimate of the array bytes, the contents columns, the dims and
        each dim's extent.
        """
        return summarize(selected(request, who).get_contents())

    @app.get("/v1/contents")
    def contents(request: Request, who: Who, format: TableFormat = "json"):
        """
        Return the metadata of the patches matching the query.

        Query parameters `<coord>_min`/`<coord>_max` bound coordinates and
        others select attributes, e.g. `?time_min=2020-01-03&tag=public*`.
        """
        df = selected(request, who).get_contents()
        return _frame_response(df, format)

    @app.get("/v1/coverage")
    def coverage(
        request: Request,
        who: Who,
        dim: str = "time",
        format: TableFormat = "json",
    ):
        """Return the spans of `dim` the matching patches cover."""
        df = selected(request, who).get_coverage(dim=dim)
        return _frame_response(df, format)

    @app.get("/v1/gaps")
    def gaps(
        request: Request,
        who: Who,
        dim: str = "time",
        format: TableFormat = "json",
    ):
        """Return the gaps in `dim` between the matching patches."""
        df = selected(request, who).get_gaps(dim=dim)
        return _frame_response(df, format)

    @app.get("/v1/data")
    def data(
        request: Request,
        who: Who,
        format: Annotated[Literal["arrow", "dasdae"], Query()] = "arrow",
    ):
        """
        Return the patches matching the query, trimmed to it.

        `format=arrow` streams length-prefixed Arrow IPC streams, one per
        patch (see `dastor.wire`); `format=dasdae` returns one DASDAE HDF5
        file. No matching data is 204 and too much is 413.
        """
        view, spool = selected_with_view(request, who)
        if not len(spool):
            return Response(status_code=204)
        limit = settings.limits.max_request_bytes
        if (size := estimated_bytes(spool.get_contents())) > limit:
            msg = f"request would return about {size} bytes; the limit is {limit}"
            raise HTTPException(413, msg)
        patches = _checked_patches(view, spool, limit)
        if format == "arrow":
            stream = _framed(patches, loads)
            return StreamingResponse(stream, media_type=FRAMED_MEDIA_TYPE)
        with _load_errors():
            return _dasdae_response(list(patches), loads)

    @app.get("/v1/index")
    def index(who: Who):
        """
        Return a DASCore spool index of the patches the caller may read.

        It is a SQLite file holding their metadata but no data;
        `dastor.Client.spool` opens it and fetches patches from
        `/v1/patch/{data_id}`.
        """
        view = catalog.view(who)
        return _file_response(view.export_index, ".sqlite", "application/vnd.sqlite3")

    @app.get("/v1/patch/{data_id}")
    def patch(
        data_id: Annotated[str, Path(pattern="^[0-9a-f]+$")],
        request: Request,
        who: Who,
        source_path: str | None = None,
        source_patch_key: str | None = None,
    ):
        """
        Return one patch, by its `data_id`, as an Arrow IPC stream.

        Query parameters trim it as they do `/v1/data`, e.g.
        `?time_min=2020-01-03T00:00:04`. `source_path` and
        `source_patch_key` pick one copy of a patch stored more than once.
        Patches the caller may not read are 404, as missing ones are (and
        ones the trim leaves empty).
        """
        reserved = _RESERVED | {"source_path", "source_patch_key"}
        view, spool = selected_with_view(request, who, reserved, data_id=data_id)
        picks = {"source_path": source_path, "source_patch_key": source_patch_key}
        if picks := {k: v for k, v in picks.items() if v is not None}:
            df = spool.get_contents()
            keep = np.ones(len(df), dtype=bool)
            for name, value in picks.items():
                keep &= (df[name].astype(str) == value).to_numpy()
            spool = spool[keep]
        if not len(spool):
            raise HTTPException(404, f"no patch {data_id!r}")
        limit = settings.limits.max_request_bytes
        with _load_errors(), loads:
            body = encode_patch(next(_checked_patches(view, spool[:1], limit)))
        return Response(bytes(body), media_type=PATCH_MEDIA_TYPE)

    @app.get("/v1/viewer/products")
    def products(who: Who) -> dict:
        """
        Return the products the caller may read, for the viewer.

        A product is the patches of one kind with the same dims and steps:
        one layer at one resolution. Each has an `id` for `/v1/viewer/tile`.
        `limits` are the bytes one tile and one screen of tiles may read,
        and `sync` says whether areas can be synced into a local data
        directory (not on a server; see `dastor browse`).
        """
        contents = _shown(catalog.view(who).select().get_contents())
        limits = settings.limits
        return {
            "products": list_products(contents),
            "sync": False,
            "limits": {
                "max_tile_bytes": limits.max_tile_bytes,
                "max_view_bytes": limits.max_view_bytes,
            },
        }

    @app.get("/v1/viewer/tile")
    def tile(
        request: Request,
        who: Who,
        product: str,
        width: Annotated[int, Query(ge=1, le=1024)] = 256,
        height: Annotated[int, Query(ge=1, le=1024)] = 256,
        stat: Annotated[Stat, Query()] = "max",
    ):
        """
        Return one image tile of a product, reduced to `height` x `width`.

        `<x>_min`/`<x>_max` and `<y>_min`/`<y>_max` bound the tile along the
        product's two dims. The body is little-endian float32 rows, the
        lowest y first, with NaN where there is no data. A tile with no
        data is 204, and one needing too many samples is 413 (zoom in, or
        use a coarser product).
        """
        wanted = decode_product(product)
        x, y = product_axes(wanted["dims"])
        reserved = _RESERVED | {"width", "height", "stat"}
        kwargs = selection(request.query_params.multi_items(), reserved)
        if len(wanted["dims"]) != 2 or not {x, y} <= set(kwargs):
            raise QueryError("a tile needs a product of two dims and bounds on both")
        view, spool = selected_with_view(request, who, reserved)
        contents = spool.get_contents()
        if not len(contents):
            return Response(status_code=204)
        ranges = [
            tile_range(kwargs[dim], contents[f"{dim}_min"].dtype.kind in "Mm")
            for dim in (x, y)
        ]
        limit = settings.limits.max_tile_bytes
        if (size := estimated_bytes(contents)) > limit:
            msg = f"tile would read about {size} bytes; the limit is {limit}"
            raise HTTPException(413, msg)
        with _load_errors(), loads:
            patches = _checked_patches(view, spool, limit)
            values = render_tile(patches, x, y, *ranges, (height, width), stat)
        if values is None:
            return Response(status_code=204)
        body, headers = encode_tile(values), {"Vary": "Accept-Encoding"}
        if "gzip" in request.headers.get("accept-encoding", ""):
            body = gzip.compress(body, compresslevel=1)
            headers["Content-Encoding"] = "gzip"
        return Response(body, media_type="application/octet-stream", headers=headers)

    store = settings.annotations and AnnotationStore(settings.annotations.path)

    def annotation_store() -> AnnotationStore:
        if not store:
            raise HTTPException(404, "this server keeps no annotations")
        return store

    Store = Annotated[AnnotationStore, Depends(annotation_store)]

    def owner_name(who: Principal) -> str | None:
        """Return the name a principal owns annotation sets under, if any."""
        if who.authenticated:
            return who.name
        return settings.annotations and settings.annotations.anonymous_owner

    def readable(who: Principal) -> Callable[[str, str], bool]:
        """
        Return a test of whether the caller may read a set.

        A caller reads their own sets, and sets on an acquisition every
        file of which they may read: annotations describe data, so seeing
        one file of a recording is no licence to read notes on the rest.
        A set naming no acquisition counts as on the files which name none.
        """
        name, whole = owner_name(who), catalog.view(who).acquisitions

        def check(owner: str, acquisition_key: str) -> bool:
            return owner == name or acquisition_key in whole

        return check

    def own(who: Principal, owner: str) -> str:
        """Return the caller's owner name, refusing writes to others' sets."""
        name = owner_name(who)
        if name is None or name != owner:
            raise HTTPException(403, "only a set's owner may change it")
        return name

    for error, status in ((AnnotationError, 400), (MissingSetError, 404)):

        def _annotation_error(request: Request, exc: Exception, status=status):
            return JSONResponse({"detail": str(exc)}, status_code=status)

        app.add_exception_handler(error, _annotation_error)

    @app.get("/v1/annotations")
    def annotation_sets(who: Who, store: Store) -> dict:
        """
        List the annotation sets the caller may read.

        Those are the caller's own, and the sets on acquisitions all of
        whose files the caller may read.

        `owner` is the name the caller's own sets are saved under (null if
        the caller can't save), and each set says whether it is `editable`.
        """
        name, check = owner_name(who), readable(who)
        sets = [x for x in store.list() if check(x["owner"], x["acquisition_key"])]
        for entry in sets:
            entry["editable"] = entry["owner"] == name
        return {"owner": name, "sets": sets}

    @app.get("/v1/annotations/{owner}/{name}")
    def annotation_set(owner: str, name: str, who: Who, store: Store) -> dict:
        """
        Return one annotation set as its annotations and features tables.

        Both are JSON records of a DASCore annotation set, with times in
        ISO 8601; `revision` is what a save must be based on.
        """
        document = store.document(owner, name)
        if not readable(who)(owner, document["acquisition_key"]):
            raise MissingSetError(f"no annotation set {owner}/{name}")
        return document

    @app.put("/v1/annotations/{owner}/{name}")
    def save_annotation_set(
        owner: str,
        name: str,
        who: Who,
        store: Store,
        document: Annotated[dict, Body()],
    ):
        """
        Save an annotation set as its next revision; only its owner may.

        The body is as `GET` returns, with `base_revision` the revision it
        was read at (0 for a new set); a save based on an older revision is
        409. The server sets each feature's `created_time`, `modified_time`
        and `modified_by`.
        """
        user = own(who, owner)
        try:
            return store.save(owner, name, document, user)
        except RevisionConflictError as exc:
            detail = {"detail": str(exc), "revision": exc.current}
            return JSONResponse(detail, status_code=409)

    @app.get("/v1/annotations/{owner}/{name}/export")
    def export_annotation_set(owner: str, name: str, who: Who, store: Store):
        """
        Return one annotation set as a zip archive of its directory.

        Unpacked, it is a folder `dascore.annotations` reads.
        """
        key = store.document(owner, name)["acquisition_key"]
        if not readable(who)(owner, key):
            raise MissingSetError(f"no annotation set {owner}/{name}")
        headers = {"Content-Disposition": f'attachment; filename="{name}.zip"'}
        body = store.export_zip(owner, name)
        return Response(body, media_type="application/zip", headers=headers)

    @app.post("/v1/annotations/{owner}/{name}/import")
    def import_annotation_set(
        owner: str,
        name: str,
        who: Who,
        store: Store,
        filename: str,
        payload: Annotated[bytes, Body(media_type="application/octet-stream")],
        dims: str = "",
        acquisition_key: str = "",
    ):
        """
        Save an uploaded file as a new annotation set; only its owner may.

        The body is the file: a zip of a set's directory (as `export`
        gives) or a bare annotations table, `.parquet` or `.csv`, named by
        `filename`. `dims` (comma separated) are for a table stating none,
        and `acquisition_key` for a set naming none. A set of the name
        already stored is 409.
        """
        user = own(who, owner)
        if len(payload) > settings.limits.max_upload_bytes:
            raise HTTPException(413, "the file is larger than is accepted")
        wanted = tuple(x for x in dims.split(",") if x)
        try:
            return store.import_file(
                owner, name, filename, payload, user, wanted, acquisition_key
            )
        except RevisionConflictError as exc:
            detail = {"detail": f"{owner}/{name} exists", "revision": exc.current}
            return JSONResponse(detail, status_code=409)

    @app.delete("/v1/annotations/{owner}/{name}", status_code=204)
    def delete_annotation_set(owner: str, name: str, who: Who, store: Store):
        """Delete an annotation set; only its owner may."""
        own(who, owner)
        store.delete(owner, name)

    app.mount("/viewer", StaticFiles(directory=_STATIC, html=True), name="viewer")
    return app


@contextlib.contextmanager
def _load_errors():
    """Answer a changed file with 409 and patches over the limit with 413."""
    try:
        yield
    except ChangedFileError as exc:
        raise HTTPException(409, f"{exc}; retry after the next refresh") from None
    except _TooLargeError as exc:
        raise HTTPException(413, str(exc)) from None


class _TooLargeError(Exception):
    """Raised when loaded patches exceed the request limit."""


def _checked_patches(view: View, spool, limit: int) -> Iterator[dc.Patch]:
    """
    Yield a spool's patches, checking each against the rules and the limit.

    A patch whose file changed to fall outside the rules, or patches whose
    real size passes the limit the estimate missed, end the response.
    """
    total = 0
    for index, row in enumerate(spool.get_contents().to_dict("records")):
        try:
            patch = view.check(spool[index], row)
        except dce.MissingPatchError as exc:
            # The file no longer holds the indexed patch.
            msg = f"{row.get('source_path')} changed since it was indexed"
            raise ChangedFileError(msg) from exc
        total += int(np.prod(patch.shape)) * np.dtype(patch.dtype).itemsize
        if total > limit:
            msg = f"request returns more than the limit of {limit} bytes"
            raise _TooLargeError(msg)
        yield patch


def _framed(patches: Iterator[dc.Patch], loads) -> Iterator:
    """Yield framed patches, loading at most a configured number at once."""
    while True:
        with loads:
            if (patch := next(patches, None)) is None:
                return
            stream = encode_patch(patch)
        yield from frame(stream)


def _file_response(write, suffix: str, media_type: str) -> StreamingResponse:
    """Return the file `write(path)` makes, deleted from disk once it is open."""
    handle, path = tempfile.mkstemp(suffix=suffix, prefix="dastor-")
    os.close(handle)
    try:
        write(path)
        file = open(path, "rb")  # noqa: SIM115 (closed by _chunks)
    finally:
        # The open handle keeps the bytes (on POSIX), so nothing is left on
        # disk however the response ends.
        os.unlink(path)
    headers = {
        "Content-Length": str(os.fstat(file.fileno()).st_size),
        "Content-Disposition": f'attachment; filename="dastor{suffix}"',
    }
    return StreamingResponse(_chunks(file), media_type=media_type, headers=headers)


def _dasdae_response(patches: list[dc.Patch], loads) -> StreamingResponse:
    """Return the patches as one DASDAE file."""

    def write(path):
        with loads:
            dc.write(dc.spool(patches), path, "DASDAE")

    return _file_response(write, ".h5", "application/x-hdf5")


def _chunks(file) -> Iterator[bytes]:
    """Yield a file's bytes in pieces, closing it at the end."""
    with file:
        while chunk := file.read(1 << 20):
            yield chunk
