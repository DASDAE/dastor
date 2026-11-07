"""Turn request query parameters into DASCore spool selections."""

from __future__ import annotations

from collections.abc import Iterable


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
