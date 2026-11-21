"""
The fibre's layout for the viewer's map, read from the spool's inventory.

Everything about the fibre (where it lies, its labels, coupling,
measured columns, and the notes saying how well any of it is known) comes
from the DASCore inventory found beside the data (`.inventory`); dastor
keeps none of it. The optional layers directory holds only background
drawings (GeoJSON) which describe the ground, not the fibre.
"""

from __future__ import annotations

from pathlib import Path

import dascore as dc
import numpy as np
from dascore.core.inventory import axis_columns
from dascore.core.inventory_loader import find_inventory

# The most points of one path sent to the page.
MAX_POINTS = 6000
# The largest background layer served, in bytes: the page draws it whole.
MAX_LAYER_BYTES = 30_000_000


def load_inventory(directory: Path | None):
    """Return the inventory DASCore finds beside a data directory, or None."""
    if directory is None:
        return None
    found = find_inventory(directory)
    return dc.inventory(found) if found is not None else None


def _clean(values, digits: int = 7) -> list:
    """Return floats for JSON, with None where a value is not stated."""
    return [None if v != v else round(float(v), digits) for v in values]


def _span(thing) -> list:
    """Return an object's time span as two ISO strings, None for an open end."""
    ends = (thing.time_min, thing.time_max)
    return [None if t is None or str(t) == "NaT" else str(t) for t in ends]


def _distance_map(acquisition) -> dict:
    """
    Return the acquisition's distance map as two equal lists of control points.

    A map the page cannot follow by straight lines between control points
    (one point, or channels only) is left out and said so; the page then
    draws the fibre but links no distance to it.
    """
    dmap = acquisition.distance_map
    if dmap is None:  # the recording's distance is the optical distance
        return {"from": [], "to": []}
    src = list(dmap.acquisition_distance or ())
    dst = list(dmap.optical_distance or ())
    if len(src) < 2 or len(src) != len(dst):
        return {"unmapped": True}
    return {"from": _clean(src, 3), "to": _clean(dst, 3)}


def frame(crs) -> dict:
    """
    Return how the inventory's coordinates are laid on the map.

    Longitude and latitude make a `geographic` frame, which web maps and
    GeoJSON layers share. Anything else (a projected grid, a local x, y, z)
    is a `plane`: its first two axes are drawn as they are, to scale, and
    nothing which is placed by longitude and latitude can be drawn with it.
    `axes` are the indices of the two axes drawn, across then up.
    """
    labels = tuple(crs.coordinate_labels)
    if "longitude" in labels and "latitude" in labels:
        axes = (labels.index("longitude"), labels.index("latitude"))
        return {"kind": "geographic", "axes": axes, "units": "degree", "name": crs.name}
    if len(labels) < 2:
        return {"kind": "none", "axes": (), "units": "", "name": crs.name}
    return {
        "kind": "plane",
        "axes": (0, 1),
        "labels": [labels[0], labels[1]],
        "units": str(crs.units[0]) if crs.units else "",
        "name": crs.name,
    }


def _start(geom) -> float:
    """Return the optical distance a geometry segment starts at."""
    return min(geom.optical_distance, default=np.inf)


def _column(geom, name: str | None, keep) -> np.ndarray:
    """Return the kept control points of a segment's column, NaN if it has none."""
    values = geom.columns.get(name)
    if values is None:
        return np.full(len(keep), np.nan)
    return np.asarray(values, dtype=float)[keep]


def _geometry(path, crs, where: dict) -> dict:
    """
    Return the path's control points, segment by segment.

    The segments are joined end to end in one list, and `breaks` holds the
    index at which each starts: values are never to be interpolated from one
    segment into the next, since a geometry may jump there. `x` and `y` are
    the two axes the frame draws; any other axis of the CRS (an elevation,
    say) is given with the measured columns, since it can colour the fibre.
    """
    labels = tuple(crs.coordinate_labels)
    drawn = set(where["axes"])
    spare = {i: labels[i] for i in range(len(labels)) if i not in drawn}
    total = sum(len(g.optical_distance) for g in path.geometry)
    step = max(1, -(-total // MAX_POINTS))
    axis_names = {n for g in path.geometry for n in axis_columns(g, crs)}
    names = [n for n in path.geometry_columns() if n not in axis_names]
    columns = {n: {"units": "", "v": []} for n in names}
    for i, label in spare.items():
        units = str(crs.units[i]) if i < len(crs.units) else ""
        columns[label] = {"units": units, "v": []}
    out = {"d": [], "breaks": [], "columns": columns}
    xs, ys = [], []
    for geom in sorted(path.geometry, key=_start):
        dist = np.asarray(geom.optical_distance, dtype=float)
        if not len(dist):
            continue
        keep = np.unique(np.r_[np.arange(0, len(dist), step), len(dist) - 1])
        out["breaks"].append(len(out["d"]))
        out["d"] += _clean(dist[keep], 3)
        by_axis = {i: n for n, i in axis_columns(geom, crs).items()}

        def take(name, geom=geom, keep=keep):
            return _column(geom, name, keep)

        if len(drawn) == 2:
            xs += _clean(take(by_axis.get(where["axes"][0])))
            ys += _clean(take(by_axis.get(where["axes"][1])))
        for i, label in spare.items():
            columns[label]["v"] += _clean(take(by_axis.get(i)), 3)
        for name in names:
            columns[name]["v"] += _clean(take(name), 3)
            columns[name]["units"] = columns[name]["units"] or str(
                geom.units.get(name, "")
            )
    for label in spare.values():  # an axis no segment states is no column
        if all(v is None for v in columns[label]["v"]):
            del columns[label]
    if any(v is not None for v in xs) and any(v is not None for v in ys):
        out["x"], out["y"] = xs, ys
    return out


def describe(inventory) -> list[dict]:
    """
    Return every optical path of the inventory, for the page.

    Each has its position, measured columns, labels and coupling along
    optical distance, its notes, and the acquisitions which recorded it. An
    acquisition is put on the path the inventory resolves it to at its own
    start, so a fibre re-laid between two epochs is not confused with itself.
    This does not depend on the caller; `visible` narrows it.
    """
    crs = inventory.coordinate_reference_system
    where = frame(crs)
    out = []
    for network in inventory.networks:
        for array in network.fiber_arrays:
            entries = {}
            for path in array.optical_paths:
                about = (path.description, array.description)
                entry = {
                    "id": f"{network.code}.{array.code}.{path.location_code}",
                    "name": array.name or array.code,
                    "about": " ".join(t.strip() for t in about if t),
                    "epoch": _span(path),
                    "length": path.optical_length,
                    "recordings": [],
                    "tracks": {},
                    **_geometry(path, crs, where),
                }
                for label in path.labels:
                    value = "yes" if label.value is None else label.value
                    entry["tracks"].setdefault(label.group, []).append(
                        [label.distance_min, label.distance_max, value]
                    )
                for item in path.coupling:
                    entry["tracks"].setdefault("coupling", []).append(
                        [item.distance_min, item.distance_max, item.coupling_type]
                    )
                entries[id(path)] = entry
                out.append(entry)
            for acq in array.acquisitions:
                key = f"{network.code}.{array.code}.{acq.location_code}.{acq.code}"
                try:
                    path = inventory.resolve(key, acq.time_min).optical_path
                except Exception:  # one the inventory cannot place is not drawn
                    continue
                if id(path) not in entries:
                    continue
                entries[id(path)]["recordings"].append(
                    {
                        "key": key,
                        "about": (acq.description or "").strip(),
                        "time": _span(acq),
                        **_distance_map(acq),
                    }
                )
    return out


def visible(paths: list[dict], keys: set[str], whole: frozenset[str]) -> list[dict]:
    """
    Return the paths a caller may see, each with the recordings they may read.

    `keys` are the acquisition keys of the rows the caller may read and
    `whole` those all of whose files they may read. The join is the
    acquisition key and nothing else: data whose key the inventory does not
    hold is not on the map. A fibre is shown only through an acquisition seen
    whole, the rule annotation sets follow: its layout says where every file
    was recorded.
    """
    seen = keys & whole
    out = []
    for path in paths:
        recordings = [r for r in path["recordings"] if r["key"] in seen]
        if recordings:
            out.append({**path, "recordings": recordings})
    return out


def layers(directory: Path | None) -> dict[str, Path]:
    """
    Return the background layers of a directory: its GeoJSON files, by name.

    Only regular files which lie in the directory itself and are small enough
    to draw are layers; a link out of it, or a dangling one, is not.
    """
    if directory is None or not directory.is_dir():
        return {}
    root = directory.resolve()
    out = {}
    for path in sorted(directory.glob("*.geojson")):
        try:
            real = path.resolve(strict=True)
            small = real.stat().st_size <= MAX_LAYER_BYTES
            if real.parent == root and real.is_file() and small:
                out[path.stem] = real
        except OSError:
            continue
    return out
