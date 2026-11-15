"""
One viewer tile, from selection to values: in the server, or in a worker.

A tile's work holds Python's lock, so tiles drawn in the server run one at a
time; `limits.tile_workers` draws them in worker processes instead. A worker
opens its own `Catalog` from the server's settings and reads the same index,
never indexing itself, and reads it again when the server has refreshed.
Access is decided by `Catalog.view`, as for every request.
"""

from __future__ import annotations

import time

import dascore as dc
import numpy as np

from dastor.auth import ANONYMOUS, Principal
from dastor.catalog import Catalog, TooLargeError, View
from dastor.config import Settings
from dastor.query import estimated_bytes
from dastor.tiles import render_tile, tile_range

# A worker's catalog, the server's refresh it was last read at, and the
# server's current refresh, which the server sets and every worker reads.
_catalog: Catalog | None = None
_generation: int | None = None
_current = None


def draw(
    view: View,
    product: dict,
    kwargs: dict,
    x: str,
    y: str,
    shape: tuple[int, int],
    stat: str,
    limit: int,
) -> tuple[np.ndarray | None, dict]:
    """
    Return a tile's values (None for no data) and how long its parts took.

    The product is selected through the view, the read estimated against
    `limit` (TooLargeError above it), each patch checked as it is opened,
    then loaded and reduced to `shape`.
    """
    clock = time.perf_counter
    start = clock()
    spool = view.products([product], **kwargs)
    contents = spool.get_contents()
    picked = clock()
    if not len(contents):
        return None, {"select": picked - start, "load": 0.0, "patches": 0}
    ranges = [
        tile_range(kwargs[dim], contents[f"{dim}_min"].dtype.kind in "Mm")
        for dim in (x, y)
    ]
    if (size := estimated_bytes(contents)) > limit:
        msg = f"tile would read about {size} bytes; the limit is {limit}"
        raise TooLargeError(msg)
    patches = list(view.checked(spool, limit))
    values = render_tile(patches, x, y, *ranges, shape, stat)
    info = {"select": picked - start, "load": clock() - picked}
    return values, info | {"patches": len(patches)}


def start(settings: dict, config, current) -> None:
    """
    Open a worker's catalog from the server's settings (a pool's initializer).

    The worker takes the server's DASCore config, so the index it reads is
    read as the server wrote it, never rebuilt. It reads the index now, as
    of the server's refresh `current` (a shared integer the server sets),
    and works out what visitors may see, so no tile waits for either.
    """
    global _catalog, _generation, _current  # noqa: PLW0603
    dc.set_config(config)
    _catalog, _current = Catalog(Settings.model_validate(settings)), current
    _catalog.refresh(update=False)
    _catalog.view(ANONYMOUS)
    _generation = current.value


def in_worker(principal: Principal, *args):
    """
    Draw a tile in a worker, as of the server's latest refresh.

    The refresh is read as the tile is drawn, not when it was asked for: a
    tile queued before a refresh and drawn after it sees the new index with
    the visibility worked out from it, never the old visibility with new rows.
    """
    global _generation  # noqa: PLW0603
    assert _catalog is not None, "start() runs first, as the pool's initializer"
    if (generation := _current.value) != _generation:
        _catalog.refresh(update=False)
        _generation = generation
    return draw(_catalog.view(principal), *args)
