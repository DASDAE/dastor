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
from dastor.sync import SyncNameError, check_name, refuse_elsewhere, synced_file

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
    # This app adds the key to whatever it passes on, so a change another
    # website's page makes the browser ask for must stop here.
    app.middleware("http")(refuse_elsewhere)
    # Redirects are not followed: the key would go wherever one pointed.
    opener = urllib.request.build_opener(_NoRedirect)

    def open_remote(method: str, path: str, query: str, body=None, headers=None):
        """Open a request to the server, returning its response or its error."""
        target = f"{base}/{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(target, data=body, method=method)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        if key is not None:
            request.add_header("Authorization", f"Bearer {key}")
        try:
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return error  # an answer too: its status and body are passed on

    def redirected(remote) -> JSONResponse | None:
        """Return the answer to give for a redirect, which is not followed."""
        if not 300 <= remote.status < 400 or remote.status == 304:
            return None
        where = remote.headers.get("location", "elsewhere")
        detail = f"{base} redirects to {where}; browse that address instead"
        return JSONResponse({"detail": detail}, 502)

    def refused(remote) -> JSONResponse:
        """Return the answer to give when the server refuses the key, or wants one."""
        with remote:
            detail = json.loads(remote.read() or b"{}").get("detail", "")
        if key is None:
            hint = (
                f"{base} needs an API key: set DASTOR_KEY and start dastor browse again"
            )
        else:
            hint = f"{base} refused the key in DASTOR_KEY ({detail})"
        return JSONResponse({"detail": hint}, 401)

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
                if (moved := redirected(remote)) is not None:
                    return moved
                if remote.status == 401:
                    return refused(remote)
                if remote.status != 200:
                    return _relay(remote, remote.read())
                try:
                    with synced_file(sync_dir, name) as out:
                        with open(out["path"], "wb") as file:
                            shutil.copyfileobj(remote, file, 1 << 20)
                            _whole(remote, file.tell())
                except OSError as exc:  # the connection dropped part way
                    return unreachable(exc)
            return {"name": out["name"], "bytes": out["bytes"]}

    @app.get("/v1/session")
    async def session():
        """
        Say who the server takes this app's key for.

        The page does not sign in here (`sign_in` is false): the key is the
        one this app was started with.
        """
        try:
            remote = await anyio.to_thread.run_sync(
                open_remote, "GET", "v1/session", ""
            )
        except OSError as exc:
            return unreachable(exc)
        with remote:
            body = await anyio.to_thread.run_sync(remote.read)
        if remote.status == 404:  # a server from before sign-in
            return {"user": None, "sign_in": False}
        if remote.status != 200:
            return _relay(remote, body)
        return {**json.loads(body), "sign_in": False}

    @app.api_route("/v1/session", methods=["POST", "DELETE"])
    def no_sign_in():
        """Refuse to sign in: the key is the one this app was started with."""
        detail = "dastor browse reads the server with the key in DASTOR_KEY"
        return JSONResponse({"detail": detail}, 400)

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
        if (moved := redirected(remote)) is not None:
            remote.close()
            return moved
        if remote.status in (204, 304):
            remote.close()
            return Response(status_code=remote.status)
        if remote.status == 401:
            return await anyio.to_thread.run_sync(refused, remote)
        return _relay(remote)

    app.mount("/viewer", StaticFiles(directory=_STATIC, html=True), name="viewer")
    return app


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Leave a redirect as the answer, where urllib would follow it."""

    def redirect_request(self, *args, **kwargs):
        return None


def _whole(remote, received: int) -> None:
    """Raise if a response ended before the length it declared."""
    declared = remote.headers.get("content-length")
    if declared is not None and int(declared) != received:
        msg = f"the download ended at {received} of {declared} bytes"
        raise ConnectionError(msg)


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
