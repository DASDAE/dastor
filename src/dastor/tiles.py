"""Products of the spool and their reduction to image tiles for the viewer."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from typing import Literal

import dascore as dc
import numpy as np
import pandas as pd

from dastor.query import QueryError

Stat = Literal["max", "mean", "absmax"]

# How far a row's step may differ from its product's and still belong to it.
_STEP_TOLERANCE = 1e-9


def _number(value) -> float | int | None:
    """Return a coordinate value as a number: times and durations in ns."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp | pd.Timedelta):
        return int(value.value)
    if isinstance(value, np.datetime64 | np.timedelta64):
        return int(value.astype("datetime64[ns]").astype("int64"))
    try:
        out = float(value)
    except TypeError, ValueError:
        return None
    return None if np.isnan(out) else out


def _numbers(values: np.ndarray) -> np.ndarray:
    """Return coordinate values as numbers: times and durations in ns."""
    if values.dtype.kind == "M":
        return values.astype("datetime64[ns]").astype("int64")
    if values.dtype.kind == "m":
        return values.astype("timedelta64[ns]").astype("int64")
    return values.astype("float64")


def _text(column: pd.Series) -> pd.Series:
    """Return a contents column as text, with missing values blank."""
    return column.astype("string").fillna("").astype(str)


def _kind_columns(contents: pd.DataFrame) -> list[str]:
    """Return the contents columns which tell kinds of patches apart."""
    return [x for x in dc.get_config().patch_kind_attrs if x in contents.columns]


def _step_numbers(contents: pd.DataFrame, dim: str) -> np.ndarray:
    """Return each row's step along a dim as a number, NaN where it has none."""
    name = f"{dim}_step"
    if name not in contents.columns:
        return np.full(len(contents), np.nan)
    out = [_number(x) for x in contents[name]]
    return np.array([np.nan if x is None else x for x in out], dtype="float64")


def encode_product(product: dict) -> str:
    """Return the id of a product: its attributes, dims and steps, encoded."""
    key = {x: product[x] for x in ("attrs", "dims", "steps")}
    text = json.dumps(key, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def decode_product(product_id: str) -> dict:
    """Return the attributes, dims and steps a product id names."""
    try:
        padded = product_id + "=" * (-len(product_id) % 4)
        out = json.loads(base64.urlsafe_b64decode(padded.encode()))
        attrs, dims, steps = out["attrs"], out["dims"], out["steps"]
        assert isinstance(attrs, dict) and isinstance(steps, dict)
        assert isinstance(dims, list) and all(isinstance(x, str) for x in dims)
    except Exception as exc:
        raise QueryError(f"not a product id: {product_id!r}") from exc
    return {"attrs": attrs, "dims": dims, "steps": steps}


def product_rows(contents: pd.DataFrame, product: dict) -> np.ndarray:
    """Return a boolean mask of the contents rows belonging to a product."""
    keep = np.array(_text(contents["dims"]) == ",".join(product["dims"]))
    for name in _kind_columns(contents):
        keep &= (_text(contents[name]) == product["attrs"].get(name, "")).to_numpy()
    for name in set(product["attrs"]) - set(contents.columns):
        keep &= product["attrs"][name] == ""
    for dim in product["dims"]:
        want = product["steps"].get(dim)
        steps = _step_numbers(contents, dim)
        if want is None:
            keep &= np.isnan(steps)
        else:
            keep &= np.abs(steps - want) <= abs(want) * _STEP_TOLERANCE
    return keep


def product_axes(dims: list[str]) -> tuple[str, str]:
    """Return the dims a product is drawn along: x is time when it has it."""
    x = "time" if "time" in dims else (dims[-1] if dims else "")
    return x, next((d for d in dims if d != x), "")


def _spans(low: np.ndarray, high: np.ndarray, step: float | None) -> list[list]:
    """Merge intervals which touch (within one and a half steps) into spans."""
    order = np.argsort(low, kind="stable")
    slack = 1.5 * step if step else 0
    out: list[list] = []
    for start, stop in zip(low[order], high[order], strict=True):
        if out and start <= out[-1][1] + slack:
            out[-1][1] = max(out[-1][1], stop)
        else:
            out.append([start, stop])
    return out


def _json_coord(value, timelike: bool):
    """Return a coordinate number as JSON: times as ISO 8601 text."""
    if timelike:
        return str(np.datetime64(int(value), "ns"))
    return float(value)


def list_products(contents: pd.DataFrame) -> list[dict]:
    """
    Return the products in spool contents.

    A product is the patches of one kind (the values of DASCore's kind
    attributes, such as `acquisition_key` and `tag`) which share dims and
    steps, so each is one layer at one resolution. A product of two dims is
    viewable: `x` is `time` when it has it, and `y` the other dim. Steps and
    spans of times are in seconds and ISO 8601.
    """
    if contents.empty:
        return []
    kinds = _kind_columns(contents)
    frame = pd.DataFrame({x: _text(contents[x]) for x in kinds})
    frame["dims"] = _text(contents["dims"]).to_numpy()
    all_dims = sorted({y for x in frame["dims"].unique() for y in x.split(",") if y})
    for dim in all_dims:
        frame[f"_step_{dim}"] = _step_numbers(contents, dim)
    keys = [*kinds, "dims", *(f"_step_{x}" for x in all_dims)]
    out = []
    groups = frame.groupby(keys, dropna=False, sort=True).indices
    for key, rows in groups.items():
        values = dict(zip(keys, key, strict=True))
        dims = [x for x in values["dims"].split(",") if x]
        part = contents.iloc[rows]
        steps, info = {}, {}
        for dim in dims:
            low, high = part[f"{dim}_min"], part[f"{dim}_max"]
            timelike = low.dtype.kind in "Mm"
            step = values[f"_step_{dim}"]
            step = None if np.isnan(step) else (int(step) if timelike else step)
            steps[dim] = step
            # Times stay whole nanoseconds, which floats would lose.
            kind = "int64" if timelike else "float64"
            low, high = low.astype(kind).to_numpy(), high.astype(kind).to_numpy()
            units = part.get(f"{dim}_units")
            info[dim] = {
                "timelike": timelike,
                "step": step / 1e9 if timelike and step is not None else step,
                "units": "" if units is None else str(_text(units).iloc[0]),
                "min": _json_coord(low.min(), timelike),
                "max": _json_coord(high.max(), timelike),
                "spans": [
                    [_json_coord(a, timelike), _json_coord(b, timelike)]
                    for a, b in _spans(low, high, step)
                ],
            }
        product = {
            "attrs": {x: values[x] for x in kinds if values[x]},
            "dims": dims,
            "steps": steps,
        }
        x, y = product_axes(dims)
        viewable = len(dims) == 2 and all(steps[d] is not None for d in dims)
        out.append(
            {
                "id": encode_product(product),
                **product,
                "label": " ".join(product["attrs"].values()) or "(untagged)",
                "coords": info,
                "x": x,
                "y": y,
                "viewable": viewable,
                "patches": len(rows),
                "dtype": str(part["dtype"].iloc[0]) if "dtype" in part else "",
            }
        )
    return out


def _bins(coord: np.ndarray, low, high, count: int, step: float):
    """
    Return, for each of `count` bins over [low, high), the samples it holds.

    Gives the first sample of each bin, one past its last, and the sample
    shown by a bin holding none: the one just before it when the bin lies
    within that sample's cell (the data is coarser than the bins), else -1.
    """
    rel = (coord - low).astype("float64")
    edges = np.linspace(0.0, float(high - low), count + 1)
    first = np.searchsorted(rel, edges[:-1], side="left")
    last = np.searchsorted(rel, edges[1:], side="left")
    before = first - 1
    near = (before >= 0) & (rel[np.clip(before, 0, None)] + step > edges[:-1])
    return first, last, np.where((last == first) & near, before, -1)


def _reduce_axis(data: np.ndarray, bins, ufunc, empty) -> np.ndarray:
    """Reduce the first axis of `data` into bins, filling bins of no data."""
    first, last, nearest = bins
    full = last > first
    out = np.full((len(first), *data.shape[1:]), empty, dtype=data.dtype)
    if full.any():
        # The full bins partition the samples from the first to the last.
        out[full] = ufunc.reduceat(data[: last[full][-1]], first[full], axis=0)
    shown = nearest >= 0
    out[shown] = data[nearest[shown]]
    return out


def _rebin(data: np.ndarray, y_bins, x_bins, ufunc, empty) -> np.ndarray:
    """Reduce an array of dims (y, x) into the bins of both axes."""
    out = _reduce_axis(data, y_bins, ufunc, empty)
    return _reduce_axis(out.T, x_bins, ufunc, empty).T


def _step(coord: np.ndarray, patch: dc.Patch, dim: str) -> float:
    """Return the width of one sample's cell along a dim."""
    step = _number(patch.get_coord(dim).step)
    if step is None and len(coord) > 1:
        step = float(np.median(np.diff(coord)))
    return abs(step) if step else 0.0


def render_tile(
    patches: Iterable[dc.Patch],
    x: str,
    y: str,
    x_range: tuple,
    y_range: tuple,
    shape: tuple[int, int],
    stat: Stat = "max",
) -> np.ndarray | None:
    """
    Reduce patches to one image of `shape` (y, x) bins over the ranges.

    A bin shows the statistic of the samples in it, or its nearest sample
    where the data is coarser than the bins; bins the patches don't cover
    are NaN. Returns None when no bin has data. Ranges are numbers, with
    times in nanoseconds.
    """
    total = np.full(shape, np.nan, dtype="float32")
    weight = np.zeros(shape, dtype="float32")
    for patch in patches:
        if set(patch.dims) != {x, y}:
            continue
        data = np.asarray(patch.transpose(y, x).data)
        if not data.size:
            continue
        if data.dtype.kind == "c" or stat == "absmax":
            data = np.abs(data)
        data = data.astype("float32", copy=False)
        coords = {d: _numbers(np.asarray(patch.coords.get_array(d))) for d in (x, y)}
        if any(len(c) > 1 and c[0] > c[-1] for c in coords.values()):
            continue  # descending coordinates are not drawn
        x_bins = _bins(coords[x], *x_range, shape[1], _step(coords[x], patch, x))
        y_bins = _bins(coords[y], *y_range, shape[0], _step(coords[y], patch, y))
        if stat == "mean":
            finite = np.isfinite(data)
            part = _rebin(np.where(finite, data, 0), y_bins, x_bins, np.add, 0)
            count = _rebin(finite.astype("float32"), y_bins, x_bins, np.add, 0)
            total = np.where(count > 0, np.nan_to_num(total) + part, total)
            weight += count
        else:
            part = _rebin(data, y_bins, x_bins, np.fmax, np.nan)
            total = np.fmax(total, part)
    if stat == "mean":
        with np.errstate(invalid="ignore", divide="ignore"):
            total = np.where(weight > 0, total / weight, np.nan).astype("float32")
    return total if np.isfinite(total).any() else None


def tile_range(value: tuple, timelike: bool) -> tuple:
    """Return a selection's bounds along a dim as numbers, for binning."""
    low, high = value
    if low is None or high is None:
        raise QueryError("a tile needs both bounds of its x and y dims")
    try:
        if timelike:
            low, high = (int(dc.to_datetime64(v).astype("int64")) for v in (low, high))
        else:
            low, high = float(low), float(high)
    except (TypeError, ValueError) as exc:
        raise QueryError(f"bad tile bounds: {value!r}") from exc
    if not high > low:
        raise QueryError("a tile's bounds must increase")
    return low, high


def encode_tile(values: np.ndarray) -> bytes:
    """Return a tile as little-endian float32 rows, the lowest y first."""
    return np.ascontiguousarray(values, dtype="<f4").tobytes()
