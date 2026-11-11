"""Named DASCore annotation sets, stored one directory each and revisioned."""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from pathlib import Path

import dascore as dc
import numpy as np
import pandas as pd

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Feature columns the store writes itself; a client's values are ignored.
STAMPS = ("created_time", "modified_time", "modified_by")

_FEATURE_COLUMNS = {
    "created_time": {
        "description": "When the feature was first saved (UTC).",
        "dtype": "datetime64[ns]",
    },
    "modified_time": {
        "description": "When the feature last changed (UTC).",
        "dtype": "datetime64[ns]",
    },
    "modified_by": {"description": "Who last changed the feature."},
    "derived_from": {
        "description": "The feature this one was copied from, as owner/set/id.",
    },
    "derived_revision": {
        "description": "The revision of the source set when it was copied.",
    },
}


class AnnotationError(ValueError):
    """Raised for an annotation set or name the store can't accept."""


class RevisionConflictError(Exception):
    """Raised when a save is based on a revision that is no longer current."""

    def __init__(self, current: int):
        super().__init__(f"the set is at revision {current}")
        self.current = current


def _records(frame: pd.DataFrame) -> list[dict]:
    """Return a frame as JSON records: times in ISO 8601, missing as null."""
    text = frame.to_json(orient="records", date_format="iso", date_unit="ns")
    return json.loads(text)


def _frame(records, time_columns=()) -> pd.DataFrame:
    """Return JSON records as a frame, reading the named columns as times."""
    if not isinstance(records, list) or not all(isinstance(x, dict) for x in records):
        raise AnnotationError("tables are lists of records")
    frame = pd.DataFrame(records)
    for name in time_columns:
        if name in frame.columns:
            try:
                times = pd.to_datetime(frame[name], utc=True, format="ISO8601")
            except (TypeError, ValueError) as exc:
                raise AnnotationError(f"column {name!r} holds a bad time") from exc
            frame[name] = times.dt.tz_convert(None).astype("datetime64[ns]")
    return frame


def _time_columns(records, dims) -> list[str]:
    """Return the coordinate columns a client wrote as times (text)."""
    names = [f"{dim}{end}" for dim in dims for end in ("", "_min", "_max")]
    return [
        name for name in names if any(isinstance(row.get(name), str) for row in records)
    ]


def _by_feature(annotations: dc.AnnotationSet) -> dict[str, str]:
    """Return each feature's content, apart from its stamps, as text."""
    features = annotations.features.drop(columns=list(STAMPS), errors="ignore")
    rows = annotations.annotations
    members = {
        key: _records(part.drop(columns="feature_id"))
        for key, part in rows.groupby(rows["feature_id"].astype(str), sort=False)
    }
    out = {}
    for record in _records(features):
        key = str(record["id"])
        content = {"feature": record, "rows": members.get(key, [])}
        out[key] = json.dumps(content, sort_keys=True)
    return out


def _revision(attrs) -> int:
    """Return the revision an annotation set's attributes state."""
    version = attrs.creation_info.version
    return int(version) if str(version).isdigit() else 0


class AnnotationStore:
    """
    Annotation sets under a directory, as `<owner>/<name>/`.

    Each set is a DASCore annotation set saved in its directory form with
    Parquet tables, so `dascore.annotations(path)` reads it. Its revision
    is the `version` of its `creation_info`, raised by one on every save.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()

    def _path(self, owner: str, name: str) -> Path:
        """Return the directory of a set, refusing names which aren't plain."""
        for value in (owner, name):
            if not _NAME.match(value):
                raise AnnotationError(f"not a valid owner or set name: {value!r}")
        return self.root / owner / name

    def list(self) -> list[dict]:
        """Return a summary of every stored set, read from its attributes."""
        out = []
        for path in sorted(self.root.glob("*/*/attrs.json")):
            if path.parent.name.startswith("."):
                continue
            attrs = json.loads(path.read_text())
            info = attrs.get("creation_info", {})
            version = str(info.get("version", ""))
            out.append(
                {
                    "owner": path.parent.parent.name,
                    "name": path.parent.name,
                    "dims": attrs.get("dims", []),
                    "acquisition_key": attrs.get("acquisition_key", ""),
                    "revision": int(version) if version.isdigit() else 0,
                    "updated": info.get("update_time"),
                }
            )
        return out

    def load(self, owner: str, name: str) -> dc.AnnotationSet:
        """Return a stored set; FileNotFoundError if there is none."""
        path = self._path(owner, name)
        if not (path / "attrs.json").is_file():
            raise FileNotFoundError(f"no annotation set {owner}/{name}")
        return dc.annotations(path)

    def document(self, owner: str, name: str) -> dict:
        """Return a stored set as a JSON document of its two tables."""
        stored = self.load(owner, name)
        return {
            "owner": owner,
            "name": name,
            "dims": list(stored.dims),
            "acquisition_key": stored.attrs.acquisition_key,
            "revision": _revision(stored.attrs),
            "annotations": _records(stored.annotations),
            "features": _records(stored.features),
        }

    def save(self, owner: str, name: str, document: dict, user: str) -> dict:
        """
        Save a document as the next revision of a set, and return it.

        The document's `base_revision` must be the stored revision (0 for a
        new set), else RevisionConflictError. Every row names its feature.
        A feature's `created_time`, `modified_time` and `modified_by` are
        set here: a feature whose content is as stored keeps its stamps.
        """
        path = self._path(owner, name)
        try:
            dims = tuple(document["dims"])
            rows = document.get("annotations", [])
            times = _time_columns(rows, dims) if isinstance(rows, list) else []
            annotations = _frame(rows, times)
            features = _frame(document.get("features", []))
            base = int(document.get("base_revision", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise AnnotationError(f"bad annotation document: {exc}") from exc
        if len(annotations):
            ids = annotations.get("feature_id")
            if ids is None or ids.isna().any() or (ids.astype(str) == "").any():
                raise AnnotationError("every annotation row needs a feature_id")
        features = features.drop(columns=list(STAMPS), errors="ignore")
        now = pd.Timestamp.now("UTC").tz_localize(None).as_unit("ns")
        with self._lock:
            old = self.load(owner, name) if (path / "attrs.json").is_file() else None
            current = _revision(old.attrs) if old is not None else 0
            if base != current:
                raise RevisionConflictError(current)
            info = {"author": owner, "version": str(current + 1), "update_time": now}
            created = old.attrs.creation_info.creation_time if old is not None else now
            info["creation_time"] = now if pd.isna(created) else created
            options = dict(
                dims=dims,
                acquisition_key=str(document.get("acquisition_key") or "") or None,
                creation_info=info,
                feature_columns=_FEATURE_COLUMNS,
            )
            try:
                new = dc.AnnotationSet(
                    annotations if len(annotations) else None,
                    features if len(features) else None,
                    **options,
                )
                stamped = self._stamped(new, old, now, user)
                new = dc.AnnotationSet(new.annotations, stamped, **options)
                self._write(new, path)
            except AnnotationError:
                raise
            except Exception as exc:  # DASCore refuses what it can't model
                raise AnnotationError(f"bad annotation set: {exc}") from exc
        return {"revision": current + 1, "updated": now.isoformat()}

    @staticmethod
    def _stamped(new, old, now, user: str) -> pd.DataFrame | None:
        """Return the new set's features with their stamps filled in."""
        features = new.features
        if not len(features):
            return None
        before = _by_feature(old) if old is not None else {}
        stamps = {}
        if old is not None and len(old.features):
            kept = old.features.reindex(columns=["id", *STAMPS])
            stamps = {str(x["id"]): x for x in kept.to_dict("records")}
        rows = []
        for key, content in _by_feature(new).items():
            was = stamps.get(key)
            if was is None:
                rows.append((now, now, user))
            elif before.get(key) == content:
                rows.append(tuple(was[x] for x in STAMPS))
            else:
                rows.append((was["created_time"], now, user))
        out = features.drop(columns=list(STAMPS), errors="ignore").copy()
        for number, column in enumerate(STAMPS):
            values = [x[number] for x in rows]
            if column != "modified_by":
                values = np.array(values, dtype="datetime64[ns]")
            out[column] = values
        return out

    @staticmethod
    def _write(annotations: dc.AnnotationSet, path: Path) -> None:
        """Write a set beside its directory, then move it into place."""
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.parent / f".{path.name}.{uuid.uuid4().hex}"
        retired = path.parent / f".{path.name}.{uuid.uuid4().hex}"
        try:
            annotations.io.save(staging, format="parquet")
            if path.exists():
                path.rename(retired)
            staging.rename(path)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(retired, ignore_errors=True)

    def delete(self, owner: str, name: str) -> None:
        """Remove a stored set; FileNotFoundError if there is none."""
        path = self._path(owner, name)
        with self._lock:
            if not (path / "attrs.json").is_file():
                raise FileNotFoundError(f"no annotation set {owner}/{name}")
            shutil.rmtree(path)
