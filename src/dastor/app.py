"""The dastor HTTP API."""

import asyncio
import contextlib
import logging
import os
import tempfile
import threading
from collections.abc import Iterator
from typing import Annotated, Literal

import anyio
import dascore as dc
import dascore.exceptions as dce
import numpy as np
import pandas as pd
import pyarrow as pa
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from dastor import __version__
from dastor.auth import Authenticator, InvalidKeyError, Principal
from dastor.catalog import Catalog, ChangedFileError, View
from dastor.config import Settings
from dastor.query import QueryError, selection
from dastor.wire import FRAMED_MEDIA_TYPE, PATCH_MEDIA_TYPE, encode_patch, frame

log = logging.getLogger(__name__)

# Query parameters the endpoints use themselves rather than select on.
_RESERVED = frozenset({"format", "dim"})

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


def estimated_bytes(contents: pd.DataFrame) -> int:
    """
    Estimate the array bytes the rows of a selected spool hold.

    Each row's samples are counted from its dims' bounds and steps; a dim
    without a usable step falls back to the row's stored size, which a
    trimmed row may lack. The estimate rejects requests before any loading;
    the limit is enforced again on the patches as they load.
    """
    total = 0
    for row in contents.to_dict("records"):
        count = 1
        for dim in str(row.get("dims", "")).split(","):
            low, high = row.get(f"{dim}_min"), row.get(f"{dim}_max")
            step = row.get(f"{dim}_step")
            try:
                samples = round((high - low) / step) + 1
            except TypeError, ValueError, ZeroDivisionError, OverflowError:
                samples = None
            if samples is None or samples < 1:
                count = row.get("data_size") or 0
                break
            count *= samples
        total += int(count) * np.dtype(row.get("dtype") or "float64").itemsize
    return total


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

    def selected_with_view(request, who) -> tuple[View, dc.BaseSpool]:
        """Return a principal's view and its patches matching the query."""
        kwargs = selection(request.query_params.multi_items(), _RESERVED)
        view = catalog.view(who)
        try:
            return view, view.select(**kwargs)
        except (TypeError, ValueError) as exc:
            # e.g. a malformed time, or text bounding a numeric coordinate
            raise QueryError(str(exc)) from exc

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

        With no query, that is all the caller may read. Gives counts, the
        contents columns, the dims and each dim's extent.
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
        try:
            return _dasdae_response(list(patches), loads)
        except ChangedFileError as exc:
            raise HTTPException(409, f"{exc}; retry after the next refresh") from None
        except _TooLargeError as exc:
            raise HTTPException(413, str(exc)) from None

    return app


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


def _dasdae_response(patches: list[dc.Patch], loads) -> StreamingResponse:
    """Return the patches as one DASDAE file, deleted once it is open."""
    handle, path = tempfile.mkstemp(suffix=".h5", prefix="dastor-")
    os.close(handle)
    try:
        with loads:
            dc.write(dc.spool(patches), path, "DASDAE")
        file = open(path, "rb")  # noqa: SIM115 (closed by _chunks)
    finally:
        # The open handle keeps the bytes (on POSIX), so nothing is left on
        # disk however the response ends.
        os.unlink(path)
    headers = {
        "Content-Length": str(os.fstat(file.fileno()).st_size),
        "Content-Disposition": 'attachment; filename="dastor.h5"',
    }
    return StreamingResponse(
        _chunks(file), media_type="application/x-hdf5", headers=headers
    )


def _chunks(file) -> Iterator[bytes]:
    """Yield a file's bytes in pieces, closing it at the end."""
    with file:
        while chunk := file.read(1 << 20):
            yield chunk
