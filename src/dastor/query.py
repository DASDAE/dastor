"""Turn request query parameters into DASCore spool selections."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd


class QueryError(ValueError):
    """Raised for query parameters that can't become a selection."""


def _number_or_text(value: str):
    """Return a number for numeric text, else the text (e.g. a time)."""
    for kind in (int, float):
        try:
            return kind(value)
        except ValueError:
            pass
    return value


def selection(params: Iterable[tuple[str, str]], reserved=frozenset()) -> dict:
    """
    Return `Spool.select` keyword arguments for query parameters.

    `<coord>_min` and `<coord>_max` bound a coordinate (either may be left
    out, e.g. `time_min=2020-01-03T00:00:04&distance_max=20`); any other
    parameter selects an attribute, where DASCore's `*` and `?` wildcards
    apply (e.g. `tag=public*`). Bounds that look numeric are numbers;
    attribute values stay text. Names in `reserved` are skipped.
    """
    out: dict = {}
    bounds: dict[str, list] = {}
    seen = set()
    for key, value in params:
        if key in reserved:
            continue
        if key in seen:
            raise QueryError(f"parameter {key!r} is given more than once")
        seen.add(key)
        name, _, end = key.rpartition("_")
        if name and end in ("min", "max"):
            pair = bounds.setdefault(name, [None, None])
            pair[end == "max"] = _number_or_text(value)
        else:
            out[key] = value
    for name, pair in bounds.items():
        if name in out:
            raise QueryError(f"{name!r} is given both as a value and as bounds")
        out[name] = tuple(pair)
    return out


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
