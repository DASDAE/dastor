"""Copy selected data into a local data directory (`dastor browse --data-dir`)."""

from __future__ import annotations

import contextlib
import os
import re
import urllib.parse
import uuid
from collections.abc import Iterator
from pathlib import Path

from fastapi.responses import JSONResponse

# A synced file's name, less its suffix: no separators, and not hidden.
SYNC_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,150}$")
_DEFAULT_NAME = "dastor"


class SyncNameError(ValueError):
    """Raised for a name a synced file can't have."""


def check_name(name: str | None) -> str:
    """Return a synced file's name (less its suffix), or raise for a bad one."""
    name = name or _DEFAULT_NAME
    if not SYNC_NAME.match(name):
        msg = f"{name!r} is not a file name: use letters, digits and _ . -"
        raise SyncNameError(msg)
    return name


def from_elsewhere(method: str, headers) -> bool:
    """
    Return True for a change another website made the browser ask for.

    A page anywhere can make a browser post to a local port, and under
    `dastor browse` that port acts for its user without a key. A request
    which changes something and says it came from a page (`Origin`) other
    than the viewer's own is one to refuse; programs send no `Origin`.
    """
    origin = headers.get("origin")
    if method in ("GET", "HEAD", "OPTIONS") or origin is None:
        return False
    return urllib.parse.urlsplit(origin).netloc != headers.get("host")


async def refuse_elsewhere(request, call_next):
    """Refuse, as middleware, a change asked for by another website's page."""
    if from_elsewhere(request.method, request.headers):
        detail = {"detail": "this request came from another website's page"}
        return JSONResponse(detail, status_code=403)
    return await call_next(request)


@contextlib.contextmanager
def synced_file(directory: Path, name: str, suffix: str = ".h5") -> Iterator[dict]:
    """
    Yield where to write a file which is to appear in a data directory.

    The file is written under a hidden temporary name and takes its own
    name only once it is whole, so a spool indexing the directory never
    reads half of one. A name already taken gets a number: nothing in the
    directory is replaced. The yielded dict has the temporary `path`; on
    leaving it also has `name` and `bytes` of the file as it now stands.
    """
    directory.mkdir(parents=True, exist_ok=True)
    # Made as any file is (not as a private temporary one), so it has the
    # permissions the directory's other files have.
    temporary = directory / f".dastor-{uuid.uuid4().hex}.part"
    temporary.touch(exist_ok=False)
    out = {"path": temporary}
    try:
        yield out
        out["bytes"] = os.path.getsize(temporary)
        out["name"] = _claim(directory, temporary, name, suffix)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _claim(directory: Path, temporary: Path, name: str, suffix: str) -> str:
    """Give the temporary file the first free name, and return that name."""
    number = 0
    while True:
        final = f"{name}_{number}{suffix}" if number else f"{name}{suffix}"
        try:
            # A hard link fails if the name exists, where a rename would replace.
            os.link(temporary, directory / final)
        except FileExistsError:
            number += 1
        else:
            return final
