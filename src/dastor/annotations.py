"""Named DASCore annotation sets, stored one directory each and revisioned."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

import dascore as dc
import pandas as pd
from dascore.exceptions import DASCoreError

from dastor.config import SET_NAME

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


# The most a set read from an upload may unpack to, and the files it may hold.
_MAX_UNPACKED = 256 * 1024**2
_MAX_MEMBERS = 16
_TABLE_SUFFIXES = (".csv", ".parquet")


class AnnotationError(ValueError):
    """Raised for an annotation set or name the store can't accept."""


class MissingSetError(LookupError):
    """Raised when there is no annotation set of the owner and name asked for."""


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
    """
    Return the coordinate columns a client wrote as times (text).

    Anything but a list of records has none; `_frame` refuses it.
    """
    if not isinstance(records, list):
        return []
    rows = [x for x in records if isinstance(x, dict)]
    names = [f"{dim}{end}" for dim in dims for end in ("", "_min", "_max")]
    return [x for x in names if any(isinstance(row.get(x), str) for row in rows)]


def _by_feature(annotations: dc.AnnotationSet) -> dict[str, str]:
    """Return each feature's content, apart from its stamps, as text."""
    features = annotations.features.drop(columns=list(STAMPS), errors="ignore")
    members: dict[str, list] = {}
    for row in _records(annotations.annotations):  # one pass, whatever the size
        members.setdefault(str(row.pop("feature_id")), []).append(row)
    out = {}
    for record in _records(features):
        key = str(record["id"])
        content = {"feature": record, "rows": members.get(key, [])}
        out[key] = json.dumps(content, sort_keys=True)
    return out


def _unpack(payload: bytes, folder: Path) -> Path:
    """
    Unpack a zipped annotation set into a folder, and return its directory.

    Only plain files are written, each under its own name, from the
    archive's root or from one folder inside it: a member's path never
    chooses where it lands.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise AnnotationError("the upload is not a zip archive") from exc
    with archive:
        members = [x for x in archive.infolist() if not x.is_dir()]
        parents = {str(Path(x.filename).parent) for x in members}
        if not members or len(members) > _MAX_MEMBERS or len(parents) != 1:
            raise AnnotationError("the archive is not one annotation set's folder")
        if sum(x.file_size for x in members) > _MAX_UNPACKED:
            raise AnnotationError("the archive unpacks to more than is accepted")
        for member in members:
            (folder / Path(member.filename).name).write_bytes(archive.read(member))
    if not (folder / "attrs.json").is_file():
        raise AnnotationError("the archive holds no attrs.json")
    return folder


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
            if not SET_NAME.fullmatch(value):
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
        """Return a stored set; MissingSetError if there is none."""
        path = self._path(owner, name)
        if not (path / "attrs.json").is_file():
            raise MissingSetError(f"no annotation set {owner}/{name}")
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
            annotations = _frame(rows, _time_columns(rows, dims))
            features = _frame(document.get("features", []))
            base = int(document.get("base_revision", 0))
        except AnnotationError:
            raise
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
            except (DASCoreError, KeyError, TypeError, ValueError) as exc:
                # DASCore refuses what it can't model
                raise AnnotationError(f"bad annotation set: {exc}") from exc
            self._write(new, path)
        return {"revision": current + 1, "updated": now.isoformat()}

    @staticmethod
    def _stamped(new, old, now, user: str) -> pd.DataFrame | None:
        """
        Return the new set's features with their stamps filled in.

        A stored feature without stamps (a set written by DASCore alone)
        keeps none until it changes, and is then created when it changes.
        """
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
                by = was["modified_by"]
                rows.append((*(was[x] for x in STAMPS[:2]), "" if pd.isna(by) else by))
            else:
                created = was["created_time"]
                rows.append((now if pd.isna(created) else created, now, user))
        out = features.drop(columns=list(STAMPS), errors="ignore").copy()
        created, modified, who = zip(*rows, strict=True)
        out["created_time"] = pd.to_datetime(list(created)).astype("datetime64[ns]")
        out["modified_time"] = pd.to_datetime(list(modified)).astype("datetime64[ns]")
        out["modified_by"] = list(who)
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

    def export_zip(self, owner: str, name: str) -> bytes:
        """
        Return a stored set as a zip archive of its directory, `<name>/`.

        Unpacked, the folder is a DASCore annotation set:
        `dascore.annotations("<name>")` reads it.
        """
        path = self._path(owner, name)
        with self._lock:
            if not (path / "attrs.json").is_file():
                raise MissingSetError(f"no annotation set {owner}/{name}")
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
                for member in sorted(x for x in path.iterdir() if x.is_file()):
                    archive.write(member, f"{name}/{member.name}")
        return out.getvalue()

    def import_file(
        self,
        owner: str,
        name: str,
        filename: str,
        payload: bytes,
        user: str,
        dims: tuple[str, ...] = (),
        acquisition_key: str = "",
    ) -> dict:
        """
        Save an uploaded annotation set as a new stored set, and return it.

        The upload is a zip of a set's directory (as `export_zip` gives, or
        any DASCore set zipped) or one bare annotations table, CSV or
        Parquet. `dims` are for a table which doesn't state its own, and
        `acquisition_key` for a set which names none. Rows naming no
        feature each become a feature, since the store needs every row to
        name one. A set of the name already stored is a revision conflict.
        """
        self._path(owner, name)  # refuse a bad name before reading anything
        suffix = Path(filename).suffix.lower()
        with tempfile.TemporaryDirectory(prefix="dastor-import-") as folder:
            try:
                if suffix == ".zip":
                    source = _unpack(payload, Path(folder))
                    loaded = dc.annotations(source)
                elif suffix in _TABLE_SUFFIXES:
                    source = Path(folder) / f"annotations{suffix}"
                    source.write_bytes(payload)
                    loaded = dc.annotations(source, dims=dims or None)
                else:
                    msg = "an annotation set is uploaded as .zip, .parquet or .csv"
                    raise AnnotationError(msg)
            except AnnotationError:
                raise
            except (DASCoreError, KeyError, TypeError, ValueError, OSError) as exc:
                raise AnnotationError(f"not an annotation set: {exc}") from exc
            rows = loaded.annotations
            if len(rows):
                ids = rows["feature_id"].astype("string").fillna("")
                blank = (ids == "").to_numpy()
                ids = ids.astype(object)
                ids[blank] = [f"row_{number}" for number in range(int(blank.sum()))]
                rows = rows.assign(feature_id=ids.to_numpy())
            document = {
                "dims": list(loaded.dims),
                "acquisition_key": loaded.attrs.acquisition_key or acquisition_key,
                "base_revision": 0,
                "annotations": _records(rows),
                "features": _records(loaded.features),
            }
        return self.save(owner, name, document, user)

    def delete(self, owner: str, name: str) -> None:
        """Remove a stored set; MissingSetError if there is none."""
        path = self._path(owner, name)
        with self._lock:
            if not (path / "attrs.json").is_file():
                raise MissingSetError(f"no annotation set {owner}/{name}")
            shutil.rmtree(path)
