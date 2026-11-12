"""The viewer inside a notebook, on a spool, a patch or a directory."""

from __future__ import annotations

import functools
import html
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path

import dascore as dc
import uvicorn

from dastor.annotations import AnnotationStore
from dastor.app import create_app
from dastor.cli import OPEN_RULE, browse_settings, local_owner
from dastor.config import Settings

# The port viewers use in turn unless told another. The page keeps its
# settings (theme, levels, display) by address, so they last while it does.
DEFAULT_PORT = 8765

# For patches held in memory, where a read is a slice: a tile and a screen
# may read any amount.
_IN_MEMORY = {"max_tile_bytes": 2**50, "max_view_bytes": 2**50}

# The viewers being served, which keeps each alive until it is closed, and
# the one which last took the port viewers use in turn.
_open: set[Viewer] = set()
_shared: Viewer | None = None


@functools.cache
def _scratch() -> tempfile.TemporaryDirectory:
    """
    Return this process's temporary annotation directory, made when first asked.

    Every viewer of a spool or patch shares it, so what was drawn outlives
    the viewer it was drawn in (a cell run again); it goes with the process.
    """
    return tempfile.TemporaryDirectory(prefix="dastor-annotations-")


def _listen(host: str, port: int) -> socket.socket:
    """Return a socket listening on a port (0 for any free one)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.create_server((host, port), family=family)


class Viewer:
    """
    The dastor viewer on a spool, served from this process.

    Displayed in a notebook it is the viewer's page in a frame; `url` opens
    the same page in a browser tab. By default the server listens on this
    machine only. As Jupyter's does, its API answers only requests carrying
    its `token`, a random one which `url` includes, so other users of the
    machine and other websites read nothing. A script reads it with
    `dastor.Client(viewer.address, key=viewer.token)`. It is served until
    `close` is called or the process ends.

    Parameters
    ----------
    source
        A spool, a patch, or the path of a directory of DAS files. A spool
        or patch is served as it is, from memory or wherever it loads from
        (a `dastor.Client` spool shows another server's data).
    annotations
        The directory annotation sets are kept in. By default a directory's
        are kept beside it, as `dastor browse` does, and a spool's or
        patch's in a temporary directory which lasts as long as the process
        and which every such viewer shares.
    height
        The height of the frame in a notebook, in pixels.
    limits
        Values for the `limits` of a server's configuration, such as
        `max_view_bytes`. Left alone, a tile and a screen may read any
        amount when every patch is held in memory, and otherwise what a
        server allows.
    port
        The port to serve on. By default viewers take one port in turn, so
        the page keeps its settings: a new viewer closes the one before it.
        Give a port (0 for any free one) to keep several open.
    host
        The address to listen on.
    base_url
        The address a browser reaches the server at, when not
        `http://<host>:<port>`: behind jupyter-server-proxy,
        ``"/proxy/{port}"`` (`{port}` is filled in).
    """

    def __init__(
        self,
        source: dc.BaseSpool | dc.Patch | str | Path,
        annotations: str | Path | None = None,
        *,
        height: int = 600,
        limits: dict | None = None,
        port: int | None = None,
        host: str = "127.0.0.1",
        base_url: str | None = None,
    ):
        global _shared  # noqa: PLW0603
        self.height = height
        if isinstance(source, str | Path):
            if not Path(source).expanduser().is_dir():
                raise NotADirectoryError(f"{source} is not a directory")
            config, spool = browse_settings(str(source), annotations), None
        else:
            spool = dc.spool(source)
            paths = spool.get_contents()["source_path"].astype(str)
            if paths.str.startswith("memorypatch://").all():
                limits = _IN_MEMORY | (limits or {})
            store = _scratch().name if annotations is None else annotations
            owner = {"path": str(store), "anonymous_owner": local_owner()}
            config = {"spool": {"rules": [OPEN_RULE]}, "annotations": owner}
        settings = Settings.model_validate(config | {"limits": limits or {}})
        self._store = AnnotationStore(settings.annotations.path)
        if port is not None:
            sock = _listen(host, port)
        else:
            # The last viewer's port, even one fallen back to, keeps the address.
            last = _shared
            if last is not None:
                last.close()
            try:
                sock = _listen(host, last.port if last else DEFAULT_PORT)
            except OSError:
                sock = _listen(host, 0)
        self.port = sock.getsockname()[1]
        address = f"[{host}]" if ":" in host else host
        base = base_url or f"http://{address}:{self.port}"
        self.token = secrets.token_urlsafe(32)
        self.address = base.format(port=self.port).rstrip("/")
        self.url = f"{self.address}/viewer/?token={self.token}"
        app = create_app(settings, spool=spool, token=self.token)
        # The notebook's logging is left as it is, and requests (whose
        # addresses may hold the token) are not logged.
        config = uvicorn.Config(app, log_config=None, access_log=False)
        self._server = uvicorn.Server(config)
        # A daemon thread with its own event loop, so the notebook's is free.
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        self._thread.start()
        # The server answers only once the spool is indexed.
        while not self._server.started:
            if not self._thread.is_alive():
                sock.close()
                msg = "the viewer's server did not start; its error is logged above"
                raise RuntimeError(msg)
            time.sleep(0.02)
        _open.add(self)
        if port is None:
            _shared = self

    def annotations(self) -> dict[str, dc.AnnotationSet]:
        """Return the stored annotation sets, by `owner/name`."""
        names = [(x["owner"], x["name"]) for x in self._store.list()]
        return {f"{o}/{n}": self._store.load(o, n) for o, n in names}

    def close(self) -> None:
        """Stop serving; the annotation sets stay where they are kept."""
        self._server.should_exit = True
        self._thread.join(timeout=10)
        _open.discard(self)

    def _repr_html_(self) -> str:
        """Return the viewer's page in a frame."""
        if self not in _open:
            return "<i>this dastor viewer is closed</i>"
        return (
            f'<iframe src="{html.escape(self.url)}" width="100%" '
            f'height="{int(self.height)}" style="border:0" '
            'allow="clipboard-write" title="dastor viewer"></iframe>'
        )

    def __repr__(self) -> str:
        """Return the viewer's address, or that it is closed."""
        return f"<dastor viewer: {self.url if self in _open else 'closed'}>"


def view(source, annotations=None, **kwargs) -> Viewer:
    """
    Show a spool, a patch or a directory of DAS files in the viewer.

    The last line of a notebook cell shows it; see `Viewer` for the
    parameters.

    Examples
    --------
    >>> import dascore as dc
    >>> import dastor
    >>> viewer = dastor.view(dc.get_example_patch())
    >>> sets = viewer.annotations()  # what was drawn, as DASCore sets
    >>> viewer.close()
    """
    return Viewer(source, annotations, **kwargs)
