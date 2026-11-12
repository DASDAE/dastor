"""The viewer on another dastor server's data (`dastor browse <url>`)."""

import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path as FilePath

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from dastor import __version__
from dastor.sync import SyncNameError, check_name, from_elsewhere, synced_file

_STATIC = FilePath(__file__).parent / "static"
# Headers of the server's answer which are passed on to the page.
_PASSED = ("content-type", "content-disposition", "content-encoding", "vary")
_LISTING = "v1/viewer/products"


def create_proxy(
    url: str,
    key: str | None = None,
    sync_dir: FilePath | None = None,
    timeout: float = 300,
) -> FastAPI:
    """
    Return an app which shows the viewer on the dastor server at `url`.

    The page is served from here and its requests are passed to the server
    with `key`, which therefore never reaches the browser. `/v1/sync` copies
    selected data into `sync_dir`, a local data directory. Anyone who can
    reach this app reads the server as the key's owner: it is for one user,
    on their own machine.
    """
    base = url.rstrip("/")
    app = FastAPI(title="dastor browse", version=__version__)

    def open_remote(method: str, path: str, query: str, body=None, headers=None):
        """Open a request to the server, returning its response or its error."""
        target = f"{base}/{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(target, data=body, method=method)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        if key is not None:
            request.add_header("Authorization", f"Bearer {key}")
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return error  # an answer too: its status and body are passed on

    def unreachable(exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": f"could not reach {base}: {exc}"}, 502)

    if sync_dir is not None:

        @app.post("/v1/sync")
        def sync(request: Request, name: str | None = None):
            """
            Download the matching data into the local data directory.

            It is one DASDAE file, as the server's `/v1/data` gives. Returns
            the file's `name` and `bytes` (`name` is null if nothing matched).
            """
            if from_elsewhere(request.headers):
                detail = {"detail": "sync is for the viewer's own page"}
                return JSONResponse(detail, 403)
            try:
                name = check_name(name)
            except SyncNameError as exc:
                return JSONResponse({"detail": str(exc)}, 400)
            params = [(k, v) for k, v in request.query_params.multi_items()]
            params = [(k, v) for k, v in params if k not in ("name", "format")]
            query = urllib.parse.urlencode([*params, ("format", "dasdae")])
            try:
                remote = open_remote("GET", "v1/data", query)
            except OSError as exc:
                return unreachable(exc)
            with remote:
                if remote.status == 204:
                    return {"name": None, "bytes": 0}
                if remote.status != 200:
                    return _relay(remote, remote.read())
                try:
                    with synced_file(sync_dir, name) as out:
                        with open(out["path"], "wb") as file:
                            shutil.copyfileobj(remote, file, 1 << 20)
                except OSError as exc:  # the connection dropped part way
                    return unreachable(exc)
            return {"name": out["name"], "bytes": out["bytes"]}

    @app.get("/health")
    @app.api_route("/v1/{rest:path}", methods=["GET", "PUT", "POST", "DELETE"])
    async def passed(request: Request):
        """Pass a request for the API to the server and its answer back."""
        path = request.url.path.lstrip("/")
        body = await request.body() or None
        kind = request.headers.get("content-type")
        headers = {"Content-Type": kind} if kind and body else {}
        # The listing is read here, so it is asked for plain; the rest go as they come.
        if path != _LISTING and (coding := request.headers.get("accept-encoding")):
            headers["Accept-Encoding"] = coding
        query = request.url.query
        try:
            remote = await anyio.to_thread.run_sync(
                open_remote, request.method, path, query, body, headers
            )
        except OSError as exc:
            return unreachable(exc)
        if path == _LISTING and remote.status == 200:
            with remote:
                listing = json.loads(await anyio.to_thread.run_sync(remote.read))
            listing["sync"] = sync_dir is not None
            return listing
        if remote.status in (204, 304):
            remote.close()
            return Response(status_code=remote.status)
        return _relay(remote)

    app.mount("/viewer", StaticFiles(directory=_STATIC, html=True), name="viewer")
    return app


def _relay(remote, body: bytes | None = None) -> Response:
    """Return the server's answer for the page: status, headers and body."""
    headers = {name: value for name in _PASSED if (value := remote.headers.get(name))}
    if body is not None:
        return Response(body, status_code=remote.status, headers=headers)
    return StreamingResponse(_chunks(remote), remote.status, headers)


def _chunks(remote) -> Iterator[bytes]:
    """Yield the server's answer in pieces, closing it at the end."""
    with remote:
        while chunk := remote.read(1 << 16):
            yield chunk
