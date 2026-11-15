"""The dastor HTTP API."""

import asyncio
import contextlib
import gzip
import json
import logging
import multiprocessing
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path as FilePath
from typing import Annotated, Literal
from urllib.parse import urlencode

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
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from dastor import __version__, tilework
from dastor.annotations import (
    AnnotationError,
    AnnotationStore,
    MissingSetError,
    RevisionConflictError,
)
from dastor.auth import (
    ANONYMOUS,
    SECURE_SESSION_COOKIE,
    SESSION_COOKIE,
    Authenticator,
    InvalidKeyError,
    Principal,
    Sessions,
    TokenGate,
)
from dastor.catalog import RAW_TAG, Catalog, ChangedFileError, TooLargeError, View
from dastor.config import SET_NAME, Settings
from dastor.query import QueryError, estimated_bytes, selection
from dastor.sync import SyncNameError, check_name, refuse_elsewhere, synced_file
from dastor.tiles import (
    Stat,
    decode_product,
    encode_tile,
    list_products,
    product_axes,
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


def create_app(
    settings: Settings,
    *,
    spool: dc.BaseSpool | None = None,
    token: str | None = None,
    sync_dir: FilePath | None = None,
) -> FastAPI:
    """
    Return the dastor app for a configuration.

    `spool` is served in place of the configured directory's, and with a
    `token` the API answers only requests which carry it (see `TokenGate`).
    Both are for `dastor.view`. `sync_dir` is a local data directory which
    `/v1/sync` copies selected data into; it is for `dastor browse`, where the
    server and its one user share a machine. No configuration file can set
    any of them.
    """
    catalog = Catalog(settings, spool)
    authenticate = Authenticator(settings.users, settings.trusting)
    signs_in = bool(settings.users) or settings.trusting
    sessions = Sessions()
    loads = threading.BoundedSemaphore(settings.limits.max_concurrent_loads)

    pool: list[ProcessPoolExecutor | None] = [None]  # tile workers, while serving

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await anyio.to_thread.run_sync(catalog.refresh)
        seconds = settings.spool.refresh_seconds
        task = seconds and asyncio.create_task(_refresh_forever(catalog, seconds))
        try:  # however serving ends, the refresh stops and the workers go
            if (workers := settings.limits.tile_workers) and spool is None:
                args = (workers, settings, catalog.generation)
                pool[0], current = await anyio.to_thread.run_sync(_tile_pool, *args)
                # the workers read the server's refresh as they draw each tile
                catalog.listeners.append(lambda g: setattr(current, "value", g))
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if pool[0] is not None:
                pool[0].shutdown(cancel_futures=True)
                pool[0] = None

    app = FastAPI(title="dastor", version=__version__, lifespan=lifespan)
    app.state.catalog = catalog
    # A request with a key can't be forged by a page elsewhere, but one acting
    # for `anonymous_owner`, or syncing, can: those from other pages are refused.
    app.middleware("http")(refuse_elsewhere)
    if token:
        app.add_middleware(TokenGate, token=token)

    def principal(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> Principal:
        """
        Return who a request acts for: its key's user, else anonymous.

        The key is in the `Authorization` header or, on a server with users,
        it was presented once to sign a page in, and the request carries
        that session's cookie. The header wins.
        """
        session_id = _session_id(request, settings.https)
        if authorization or not (session_id and signs_in):
            try:
                return authenticate(authorization)
            except InvalidKeyError as exc:
                headers = {"WWW-Authenticate": "Bearer"}
                raise HTTPException(401, str(exc), headers=headers) from None
        who = sessions.get(session_id)
        if who is None:
            raise HTTPException(401, "your sign-in has ended: sign in again")
        return who

    Who = Annotated[Principal, Depends(principal)]

    def signed_in(request: Request) -> bool:
        """Return True for a request with a known key or a live session."""
        try:
            return principal(
                request, request.headers.get("authorization")
            ).authenticated
        except HTTPException:
            return False

    # Without users no one could sign in, so a server open to no visitors
    # merely shows nothing, as it always has.
    if settings.users and not settings.open_to_visitors:

        @app.middleware("http")
        async def turn_visitors_away(request: Request, call_next):
            """
            Answer a visitor on a server open to none before doing any work.

            The API refuses them, and the viewer's page sends them to sign
            in. `/v1/session`, `/signin`, `/health` and the page's scripts,
            styles and icons stay open, since signing in needs them. A
            request with a key goes on, to be told if the key is unknown.
            """
            path = request.url.path.removeprefix(request.scope.get("root_path", ""))
            api = path.startswith("/v1/") and path != "/v1/session"
            page = path in ("/viewer", "/viewer/", "/viewer/index.html")
            keyed = "authorization" in request.headers
            if (api or page) and not keyed and not signed_in(request):
                if api:
                    detail = {"detail": "sign in to use this server"}
                    return JSONResponse(detail, 401)
                # Relative, so the way there and back holds behind a proxy
                # which serves the app under a prefix it strips.
                up = "../" * path[1:].count("/")
                here = path[1:] + (f"?{q}" if (q := request.url.query) else "")
                where = f"{up}signin?" + urlencode({"next": here})
                return RedirectResponse(where, status_code=303)
            return await call_next(request)

    TableFormat = Annotated[Literal["json", "arrow"], Query()]

    def selected_with_view(
        request, who, reserved=_RESERVED, **extra
    ) -> tuple[View, dc.BaseSpool]:
        """
        Return a principal's view and its patches matching the query.

        `product=<id>` (from `/v1/viewer/products`) keeps one product, and
        may be given more than once to keep several. The attributes they
        share go into the selection, so the index narrows before any rows
        are read; dims, steps and the other attributes are matched on what
        is left.
        """
        kwargs = selection(request.query_params.multi_items(), reserved)
        wanted = [decode_product(p) for p in request.query_params.getlist("product")]
        view = catalog.view(who)
        return view, view.products(wanted, **kwargs, **extra)

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

    @app.get("/v1/session")
    def session(
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """
        Say who the caller is, and whether its pages can sign in here.

        `sign_in` is true on a server with users, and only there. An ended
        sign-in is cleared rather than refused.
        """
        try:
            who = principal(request, authorization)
        except HTTPException:
            if authorization:
                raise
            _forget_session(request, response)
            who = ANONYMOUS
        return _session(who, settings)

    @app.post("/v1/session")
    def sign_in(
        request: Request,
        response: Response,
        key: Annotated[str | None, Body(embed=True)] = None,
        user: Annotated[str | None, Body(embed=True)] = None,
    ) -> dict:
        """
        Sign the browser in with an API key, for every page of this server.

        The key is checked and not kept: the cookie names a session of the
        server's, so it can't be read for the key and ends on sign-out. It
        is `HttpOnly` (the pages' scripts don't see it) and `SameSite=Strict`
        (other websites' pages don't send it); over HTTPS it is also
        `Secure` and `__Host-`, as always when the config says `https`.
        Only on a server with users, or a trusting one. Given `user`, it
        signs in as that user: with the user's key if it has one; with none,
        or as a new name, only on a trusting server.
        """
        if not signs_in:
            raise HTTPException(404, "this server has no users to sign in as")
        key = key.strip() if key else None
        try:
            if user:
                who = authenticate.named(user.strip(), key)
            elif key:
                who = authenticate(f"Bearer {key}")
            else:
                raise InvalidKeyError("give a key, or a user to sign in as")
        except InvalidKeyError as exc:
            raise HTTPException(401, str(exc) if user else "unknown API key") from None
        if old := _session_id(request, settings.https):
            sessions.end(old)
        secure = _secure(request, settings.https)
        if not secure and request.client and request.client.host not in _LOCAL:
            log.warning(
                "signed %s in over plain HTTP from %s: if a proxy serves this "
                "server over HTTPS, set `https: true` in the config",
                who.name,
                request.client.host,
            )
        response.set_cookie(
            SECURE_SESSION_COOKIE if secure else SESSION_COOKIE,
            sessions.start(who),
            max_age=int(sessions.lifetime),
            httponly=True,
            samesite="strict",
            secure=secure,
            # The server's pages need it as well as its API; `__Host-` cookies
            # must cover the whole host.
            path="/" if secure else _root_path(request),
        )
        return _session(who, settings)

    @app.delete("/v1/session", status_code=204)
    def sign_out(request: Request, response: Response) -> None:
        """End the browser's sign-in, for every page of this server."""
        if session_id := _session_id(request, settings.https):
            sessions.end(session_id)
        _forget_session(request, response)

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
        patches = view.checked(spool, limit)
        if format == "arrow":
            stream = _framed(patches, loads)
            return StreamingResponse(stream, media_type=FRAMED_MEDIA_TYPE)
        with _load_errors():
            return _dasdae_response(list(patches), loads)

    if sync_dir is not None:

        @app.post("/v1/sync")
        def sync(request: Request, who: Who, name: str | None = None) -> dict:
            """
            Write the matching patches into the local data directory.

            They are trimmed to the query and written as one DASDAE file.
            The query is that of `/v1/data`; `name` names the file (less its
            suffix), with a number added if the name is taken. Returns the
            file's `name` and `bytes` (`name` is null if nothing matched).
            """
            try:
                name = check_name(name)
            except SyncNameError as exc:
                raise QueryError(str(exc)) from exc
            reserved = _RESERVED | {"name"}
            view, spool = selected_with_view(request, who, reserved)
            if not len(spool):
                return {"name": None, "bytes": 0}
            limit = settings.limits.max_request_bytes
            if (size := estimated_bytes(spool.get_contents())) > limit:
                msg = f"request would copy about {size} bytes; the limit is {limit}"
                raise HTTPException(413, msg)
            with _load_errors(), loads, synced_file(sync_dir, name) as out:
                patches = list(view.checked(spool, limit))
                dc.write(dc.spool(patches), out["path"], "DASDAE")
            return {"name": out["name"], "bytes": out["bytes"]}

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
            body = encode_patch(next(view.checked(spool[:1], limit)))
        return Response(bytes(body), media_type=PATCH_MEDIA_TYPE)

    listed: dict[tuple, list] = {}  # product listings, by caller and spool generation
    raw_seconds = settings.spool.raw and settings.spool.raw.max_view_seconds

    @app.get("/v1/viewer/products")
    def products(request: Request, who: Who) -> Response:
        """
        Return the products the caller may read, for the viewer.

        A product is the patches of one kind with the same dims and steps:
        one layer at one resolution. Each has an `id` for `/v1/viewer/tile`.
        `limits` are the bytes one tile and one screen of tiles may read,
        and `sync` says whether areas can be synced into a local data
        directory (not on a server; see `dastor browse`).
        """
        # worked out once for each caller until the spool is indexed again
        key = (who, catalog.generation)
        if (found := listed.get(key)) is None:
            view = catalog.view(who)
            contents = _shown(view.select().get_contents())
            raw = _shown(view.raw_contents())
            found = list_products(contents) + [
                {**p, "raw": True, "max_view_seconds": raw_seconds}
                for p in list_products(raw)
                if p["attrs"].get("tag") == RAW_TAG
            ]
            if len(listed) > 256:
                listed.clear()
            listed[key] = found
        limits = settings.limits
        return _json_maybe_gzip(
            request,
            {
                "products": found,
                "sync": sync_dir is not None,
                "limits": {
                    "max_tile_bytes": limits.max_tile_bytes,
                    "max_view_bytes": limits.max_view_bytes,
                },
            },
        )

    @app.get("/v1/viewer/tile")
    def tile(
        request: Request,
        who: Who,
        product: str,
        width: Annotated[int, Query(ge=1, le=1024)] = 256,
        height: Annotated[int, Query(ge=1, le=1024)] = 256,
        stat: Annotated[Stat, Query()] = "max",
        precision: Annotated[Literal["single", "half"], Query()] = "single",
    ):
        """
        Return one image tile of a product, reduced to `height` x `width`.

        `<x>_min`/`<x>_max` and `<y>_min`/`<y>_max` bound the tile along the
        product's two dims. The body is little-endian float32 rows, the
        lowest y first, with NaN where there is no data. A tile with no
        data is 204, and one needing too many samples is 413 (zoom in, or
        use a coarser product).
        """
        clock = time.perf_counter
        start = clock()
        wanted = decode_product(product)
        x, y = product_axes(wanted["dims"])
        reserved = _RESERVED | {"width", "height", "stat", "precision"}
        kwargs = selection(request.query_params.multi_items(), reserved)
        if len(wanted["dims"]) != 2 or not {x, y} <= set(kwargs):
            raise QueryError("a tile needs a product of two dims and bounds on both")
        job = (
            wanted,
            kwargs,
            x,
            y,
            (height, width),
            stat,
            settings.limits.max_tile_bytes,
        )
        with _load_errors(), loads:
            # a tile the page gave up on while it waited (a quick zoom) is not
            # worked out: the work would only delay the tiles still wanted
            if anyio.from_thread.run(request.is_disconnected):
                log.debug("tile abandoned before drawing: %s", _tile_name(wanted))
                return Response(status_code=204)
            waited = clock()
            # selected through the reader's view, checked, loaded and reduced:
            # here, or in a worker, which reads the same index
            if pool[0] is None:
                values, info = tilework.draw(catalog.view(who), *job)
            else:
                work = pool[0].submit(tilework.in_worker, who, *job)
                values, info = work.result()
        drawn = clock()
        timing = {
            "wait": waited - start,
            "select": info["select"],
            "load": info["load"],
        }
        if values is None:
            return Response(status_code=204, headers=_timing(**timing))
        body, dtype = encode_tile(values, half=precision == "half")
        headers = {"Vary": "Accept-Encoding", "X-Tile-Dtype": dtype}
        if "gzip" in request.headers.get("accept-encoding", ""):
            body = gzip.compress(body, compresslevel=1)
            headers["Content-Encoding"] = "gzip"
        # how long each part took, for the browser's network panel and the profiler
        headers |= _timing(**timing, encode=clock() - drawn)
        log.debug(
            "tile %s: %d patches, wait %.0f ms, select %.0f ms, load %.0f ms,"
            " %d bytes %s%s",
            _tile_name(wanted),
            info["patches"],
            1000 * timing["wait"],
            1000 * timing["select"],
            1000 * timing["load"],
            len(body),
            dtype,
            " in a worker" if pool[0] is not None else "",
        )
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

    @app.get("/signin", include_in_schema=False)
    def signin_page() -> FileResponse:
        """Return the page which signs the browser in, then goes on to `?next=`."""
        return FileResponse(_STATIC / "signin.html")

    app.mount("/viewer", _Revalidated(directory=_STATIC, html=True), name="viewer")
    return app


def _tile_pool(workers: int, settings: Settings, generation: int) -> tuple:
    """
    Start the tile workers, warm.

    They fork from a server which has imported what tiles need, each opens
    its catalog from the settings, and all start now rather than on a first
    tile. A spool handed to the app in memory can't be shared with them, so
    it never has workers.
    """
    context = multiprocessing.get_context("forkserver")
    context.set_forkserver_preload(["dascore", "dastor.tilework"])
    current = context.Value("q", generation, lock=False)  # the server's refresh
    pool = ProcessPoolExecutor(
        workers,
        mp_context=context,
        initializer=tilework.start,
        initargs=(settings.model_dump(mode="json"), dc.get_config(), current),
    )
    try:
        for future in [pool.submit(os.getpid) for _ in range(workers)]:
            future.result()
    except BaseException:
        pool.shutdown(cancel_futures=True)
        raise
    return pool, current


def _timing(**parts: float) -> dict:
    """Return a `Server-Timing` header of named durations, given in seconds."""
    return {
        "Server-Timing": ", ".join(f"{k};dur={1000 * v:.1f}" for k, v in parts.items())
    }


def _tile_name(product: dict) -> str:
    """Return how a log line names a product: its tag and steps."""
    steps = ",".join(f"{k}={v}" for k, v in product["steps"].items())
    return f"{product['attrs'].get('tag', '')!r} [{steps}]"


def _json_maybe_gzip(request: Request, content) -> Response:
    """
    Return JSON, gzipped for a client which takes it.

    The viewer's product listing runs to hundreds of kilobytes, which gzip
    makes a tenth of; done here rather than by middleware, which would also
    squeeze the binary data responses.
    """
    body = json.dumps(
        jsonable_encoder(content),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    headers = {"Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        body = gzip.compress(body, compresslevel=5)
        headers["Content-Encoding"] = "gzip"
    return Response(body, media_type="application/json", headers=headers)


class _Revalidated(StaticFiles):
    """
    The viewer's files, which browsers ask after on every load.

    `no-cache` keeps them cached but checked (a 304 when unchanged), so a
    page is never a mix of an old script and a new one after an update.
    """

    def file_response(self, *args, **kwargs):
        """Return a file, marked to be checked before it is used again."""
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _session(who: Principal, settings: Settings) -> dict:
    """
    Return who a caller is, as `/v1/session` gives it.

    A trusting server also lists the users one may sign in as: those of the
    config (`key` says one is needed) and the owners of annotation sets.
    """
    out = {
        "user": who.name if who.authenticated else None,
        "sign_in": bool(settings.users) or settings.trusting,
        "trusting": settings.trusting,
    }
    if settings.trusting:
        users = {x.name: bool(x.key_sha256) for x in settings.users}
        if settings.annotations and settings.annotations.path.is_dir():
            for path in settings.annotations.path.iterdir():
                if path.is_dir() and SET_NAME.fullmatch(path.name):
                    users.setdefault(path.name, False)
        out["users"] = [{"name": k, "key": v} for k, v in sorted(users.items())]
    return out


# Clients of the server's own machine, which sign in over plain HTTP unremarked.
_LOCAL = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _secure(request: Request, https: bool) -> bool:
    """Return True if the page is served over HTTPS, here or by a proxy."""
    return https or request.url.scheme == "https"


def _session_id(request: Request, https: bool) -> str | None:
    """Return the session id a request's cookie names, if any."""
    # Over HTTPS only the `__Host-` cookie counts: a plain one may have been
    # planted by another host of the domain, to sign the browser in as
    # someone else.
    if _secure(request, https):
        return request.cookies.get(SECURE_SESSION_COOKIE)
    return request.cookies.get(SESSION_COOKIE)


def _root_path(request: Request) -> str:
    """Return the path the server is mounted at, under which its cookie is sent."""
    return request.scope.get("root_path", "").rstrip("/") + "/"


def _forget_session(request: Request, response: Response) -> None:
    """Tell the browser to drop its session cookie."""
    response.delete_cookie(SESSION_COOKIE, path=_root_path(request))
    response.delete_cookie(SECURE_SESSION_COOKIE, path="/", secure=True)


@contextlib.contextmanager
def _load_errors():
    """Answer a changed file with 409 and patches over the limit with 413."""
    try:
        yield
    except ChangedFileError as exc:
        raise HTTPException(409, f"{exc}; retry after the next refresh") from None
    except TooLargeError as exc:
        raise HTTPException(413, str(exc)) from None


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
