"""The viewer inside a notebook, on a spool, a patch or a directory."""

from __future__ import annotations

import html
import secrets
import socket
import tempfile
import threading
import time
import weakref
from pathlib import Path

import dascore as dc
import uvicorn

from dastor.annotations import AnnotationStore
from dastor.app import create_app
from dastor.cli import OPEN_RULE, browse_settings, local_owner
from dastor.config import Settings

# The port `view` uses unless told another. The page keeps its settings
# (theme, levels, display) by address, so they last while the port does.
DEFAULT_PORT = 8765

# Limits for patches held in memory, where a read is a slice: none in effect.
_IN_MEMORY = {"max_tile_bytes": 2**50, "max_view_bytes": 2**50}

# The viewer `view` last put on the default port, which the next replaces.
_current: Viewer | None = None

# The temporary directory of annotation sets drawn on spools and patches.
_folder: tempfile.TemporaryDirectory | None = None


def _bind(host: str, port: int) -> socket.socket:
    """Return a socket bound to a port (0 for any free one)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def _listen(host: str, port: int | None) -> socket.socket:
    """Return a socket on `port`; with none asked for, a free one if need be."""
    if port is not None:
        return _bind(host, port)
    try:
        return _bind(host, DEFAULT_PORT)
    except OSError:
        return _bind(host, 0)


def _scratch() -> str:
    """
    Return this process's temporary annotation directory, made when first asked.

    Every viewer of a spool or patch shares it, so what was drawn outlives
    the viewer it was drawn in (a cell run again); it goes with the process.
    """
    global _folder  # noqa: PLW0603
    if _folder is None:
        _folder = tempfile.TemporaryDirectory(prefix="dastor-annotations-")
    return _folder.name


def _stop(server: uvicorn.Server, thread: threading.Thread) -> None:
    """Stop a viewer's server."""
    server.should_exit = True
    if thread is not threading.current_thread():
        thread.join(timeout=10)


class Viewer:
    """
    The dastor viewer on a spool, served from this process.

    Displayed in a notebook it is the viewer's page in a frame; `url` opens
    the same page in a browser tab. The server listens on this machine only
    and, as Jupyter's does, answers only requests carrying its `token`, a
    random one which `url` includes: other users of the machine and other
    websites are refused. `dastor.Client(viewer.address, key=viewer.token)`
    reads it from a script.

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
        Values for the server's `limits` (see the README), such as
        `max_view_bytes`. By default a directory has the server's, and
        patches held in memory have no limit on what a screen reads.
    port
        The port to serve on. By default one port is used by every viewer in
        turn, so the page keeps its settings: a new viewer closes the one
        before it. Give a port to keep several open.
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
            annotations = _scratch() if annotations is None else annotations
            config = {
                "spool": {"rules": [OPEN_RULE]},
                "annotations": {
                    "path": str(annotations),
                    "anonymous_owner": local_owner(),
                },
            }
        settings = Settings.model_validate(config | {"limits": limits or {}})
        self._store = AnnotationStore(settings.annotations.path)
        sock = _listen(host, port)
        self.port = sock.getsockname()[1]
        address = f"[{host}]" if ":" in host else host
        base = base_url or f"http://{address}:{self.port}"
        self.token = secrets.token_urlsafe(32)
        self.address = base.format(port=self.port).rstrip("/")
        self.url = f"{self.address}/viewer/?token={self.token}"
        # log_config=None: the notebook's logging is left as it is.
        app = create_app(settings, spool, self.token)
        server = uvicorn.Server(uvicorn.Config(app, log_config=None))
        # A daemon thread with its own event loop, so the notebook's is free.
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        self._close = weakref.finalize(self, _stop, server, thread)
        thread.start()
        # The server answers only once the spool is indexed.
        while not server.started:
            if not thread.is_alive():
                self.close()
                sock.close()
                msg = "the viewer's server did not start; its error is logged above"
                raise RuntimeError(msg)
            time.sleep(0.02)

    def annotations(self) -> dict[str, dc.AnnotationSet]:
        """Return the annotation sets saved in the viewer, by `owner/name`."""
        sets = self._store.list()
        names = [(x["owner"], x["name"]) for x in sets]
        return {
            f"{owner}/{name}": self._store.load(owner, name) for owner, name in names
        }

    def close(self) -> None:
        """Stop serving; the annotation sets stay where they are kept."""
        self._close()

    def _repr_html_(self) -> str:
        """Return the viewer's page in a frame."""
        if not self._close.alive:
            return "<i>this dastor viewer is closed</i>"
        return (
            f'<iframe src="{html.escape(self.url)}" width="100%" '
            f'height="{int(self.height)}" style="border:0" '
            'allow="clipboard-write" title="dastor viewer"></iframe>'
        )

    def __repr__(self) -> str:
        """Return the viewer's address, or that it is closed."""
        state = self.url if self._close.alive else "closed"
        return f"<dastor viewer: {state}>"


def view(source, annotations=None, *, port: int | None = None, **kwargs) -> Viewer:
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
    global _current  # noqa: PLW0603
    if port is not None:
        return Viewer(source, annotations, port=port, **kwargs)
    if _current is not None:
        _current.close()
    _current = Viewer(source, annotations, **kwargs)
    return _current
