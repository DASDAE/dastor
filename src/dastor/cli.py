"""The `dastor` command."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from dastor.auth import hash_key, new_key

# The rule under which everything in the spool is readable.
OPEN_RULE = {"column": "source_path", "pattern": "", "allow": ["*"]}


def local_owner() -> str:
    """Return the system user's name as the owner of annotation sets."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", getpass.getuser()).lstrip("_.-")
    return name[:64] or "me"


def browse_settings(path: str, annotations: str | None = None) -> dict:
    """
    Return the configuration `dastor browse` serves a directory with.

    Everything in the directory is readable, and annotation sets are kept
    beside it (in `<directory>_annotations` unless a place is given) under
    the system user's name.
    """
    data = Path(path).expanduser().resolve()
    if not data.is_dir():
        raise SystemExit(f"{data} is not a directory")
    # A drive's root has no name to put a folder beside.
    store = annotations or data.parent / f"{data.name or 'das'}_annotations"
    return {
        "spool": {
            "path": str(data),
            "rules": [OPEN_RULE],
            "refresh_seconds": 300,
        },
        "annotations": {"path": str(store), "anonymous_owner": local_owner()},
    }


def remote_key() -> str | None:
    """Return the API key for a remote server, from `DASTOR_KEY`, if any."""
    return os.environ.get("DASTOR_KEY") or None


def sync_directory(data_dir: str | None, source: Path | None = None) -> Path | None:
    """
    Return the local data directory selected data is synced into, if any.

    It may not be the directory being browsed, nor inside it: synced files
    would then be served as more of the same data.
    """
    if data_dir is None:
        return None
    out = Path(data_dir).expanduser().resolve()
    if source is not None and (out == source or source in out.parents):
        raise SystemExit(f"--data-dir {out} is inside the directory being browsed")
    return out


def _open_when_up(host: str, port: int, url: str) -> None:
    """
    Open a browser at `url` once the server answers on its port.

    The server listens only after it has indexed the directory, which can
    take minutes the first time, so a fixed delay would open a dead page.
    """
    while True:
        try:
            socket.create_connection((host, port), timeout=1).close()
        except OSError:
            time.sleep(0.3)
        else:
            webbrowser.open(url)
            return


def main(argv: list[str] | None = None) -> int:
    """Run the dastor command line."""
    parser = argparse.ArgumentParser(prog="dastor", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve the spool in a config file")
    serve.add_argument("config", help="path to the YAML config")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    browse = commands.add_parser(
        "browse", help="open the viewer on a directory of DAS files, or on a server"
    )
    browse.add_argument(
        "path",
        help="the directory of DAS files, or the URL of a dastor server "
        "(its API key is read from the DASTOR_KEY environment variable)",
    )
    browse.add_argument(
        "--annotations", help="where annotation sets are kept (a directory's only)"
    )
    browse.add_argument(
        "--data-dir",
        help="a local data directory which the viewer's Sync copies selected data into",
    )
    browse.add_argument("--host", default="127.0.0.1")
    browse.add_argument("--port", type=int, default=8000)
    browse.add_argument("--no-open", action="store_true", help="don't open a browser")
    commands.add_parser("new-key", help="print a new API key and its digest")
    commands.add_parser("hash-key", help="print the digest of a key read from stdin")
    args = parser.parse_args(argv)

    if args.command == "new-key":
        key, digest = new_key()
        print(f"key:        {key}\nkey_sha256: {digest}")
    elif args.command == "hash-key":
        print(hash_key(sys.stdin.readline().strip()))
    else:
        import uvicorn  # noqa: PLC0415

        from dastor.app import create_app  # noqa: PLC0415
        from dastor.config import Settings  # noqa: PLC0415

        if args.command == "serve":
            app = create_app(Settings.from_yaml(args.config))
        elif re.match(r"https?://", args.path):
            from dastor.proxy import create_proxy  # noqa: PLC0415

            if args.annotations:
                raise SystemExit("--annotations: a server keeps its own annotations")
            sync_dir = sync_directory(args.data_dir)
            app = create_proxy(args.path, remote_key(), sync_dir)
        else:
            config = browse_settings(args.path, args.annotations)
            source = Path(config["spool"]["path"])
            sync_dir = sync_directory(args.data_dir, source)
            app = create_app(Settings.model_validate(config), sync_dir=sync_dir)
        if args.command == "browse":
            url = f"http://{args.host}:{args.port}/viewer/"
            print(f"viewer: {url}")
            if not args.no_open:
                # A daemon: it ends with the command if the server never starts.
                waiter = (args.host, args.port, url)
                threading.Thread(target=_open_when_up, args=waiter, daemon=True).start()
        # One process: each would otherwise index and refresh the spool itself.
        uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
