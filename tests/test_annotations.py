"""Tests for stored annotation sets and their API."""

import io
import zipfile
from pathlib import Path

import dascore as dc
import pandas as pd
import pytest
from conftest import ALICE_KEY, BOB_KEY, VIEW_KEY, auth
from fastapi.testclient import TestClient

from dastor.annotations import (
    AnnotationError,
    AnnotationStore,
    MissingSetError,
    RevisionConflictError,
)
from dastor.app import create_app
from dastor.config import AnnotationConfig

DIMS = ["distance", "time"]


def _document(base_revision=0, **more) -> dict:
    """Return a set of one path and one box, as a client sends it."""
    rows = [
        {"feature_id": "track", "seq": 0, "time": "2024-05-01T00:00:01", "distance": 2},
        {
            "feature_id": "track",
            "seq": 1,
            "time": "2024-05-01T00:00:09",
            "distance": 90,
        },
        {
            "feature_id": "area",
            "time_min": "2024-05-01T00:00:02",
            "time_max": "2024-05-01T00:00:04",
            "distance_min": 10.0,
            "distance_max": 40.0,
        },
    ]
    features = [
        {"id": "track", "geometry": "path", "label": "car"},
        {"id": "area", "label": "noise"},
    ]
    document = {"dims": DIMS, "annotations": rows, "features": features}
    return {**document, "base_revision": base_revision, **more}


@pytest.fixture
def store(tmp_path):
    """An empty annotation store."""
    return AnnotationStore(tmp_path / "annotations")


class TestStore:
    """Tests for saving sets to a directory."""

    def test_saved_set_is_a_dascore_set(self, store):
        """A saved set is a directory DASCore reads, with Parquet tables."""
        assert store.save("me", "picks", _document(), "me")["revision"] == 1
        path = store.root / "me" / "picks"
        assert (path / "annotations.parquet").is_file()
        saved = dc.annotations(path)
        assert saved.dims == tuple(DIMS)
        assert saved["track"].kind == "path"
        assert saved.attrs.creation_info.author == "me"
        assert saved.annotations["time"].dtype.kind == "M"
        assert store.load("me", "picks") == saved

    def test_document_round_trips(self, store):
        """What is read back is what was saved, with stamps and a revision."""
        store.save("me", "picks", _document(acquisition_key=VIEW_KEY), "me")
        document = store.document("me", "picks")
        assert document["revision"] == 1
        assert document["acquisition_key"] == VIEW_KEY
        labels = {x["id"]: x["label"] for x in document["features"]}
        assert labels == {"track": "car", "area": "noise"}
        track = [x for x in document["annotations"] if x["feature_id"] == "track"]
        assert [x["distance"] for x in track] == [2, 90]
        assert track[0]["time"].startswith("2024-05-01T00:00:01")
        assert all(x["modified_by"] == "me" for x in document["features"])

    def test_only_changed_features_are_restamped(self, store):
        """A save keeps the stamps of features whose content is as stored."""
        store.save("me", "picks", _document(), "me")
        before = {x["id"]: x for x in store.document("me", "picks")["features"]}
        changed = _document(base_revision=1)
        changed["annotations"][1]["distance"] = 95
        changed["features"][0]["modified_by"] = "someone else"  # ignored
        assert store.save("me", "picks", changed, "editor")["revision"] == 2
        after = {x["id"]: x for x in store.document("me", "picks")["features"]}
        assert after["area"] == before["area"]
        assert after["track"]["modified_by"] == "editor"
        assert after["track"]["created_time"] == before["track"]["created_time"]
        assert after["track"]["modified_time"] > before["track"]["modified_time"]

    def test_stale_revision_conflicts(self, store):
        """A save based on a revision no longer current is refused."""
        store.save("me", "picks", _document(), "me")
        with pytest.raises(RevisionConflictError) as error:
            store.save("me", "picks", _document(), "me")
        assert error.value.current == 1
        assert store.document("me", "picks")["revision"] == 1

    def test_set_written_by_dascore_can_be_saved(self, store):
        """A stored set without stamps, as DASCore writes one, takes a save."""
        store.save("me", "picks", _document(), "me")
        path = store.root / "me" / "picks"
        stored = dc.annotations(path)
        bare = dc.AnnotationSet(
            stored.annotations,
            stored.features[["id", "geometry", "label"]],
            dims=stored.dims,
            creation_info={"version": "1"},
        )
        bare.io.save(path, format="parquet")
        changed = _document(base_revision=1)
        changed["features"][0]["label"] = "lorry"
        assert store.save("me", "picks", changed, "me")["revision"] == 2
        after = {x["id"]: x for x in store.document("me", "picks")["features"]}
        assert after["track"]["modified_by"] == "me"
        assert after["track"]["created_time"] is not None
        assert after["area"]["modified_time"] is None  # untouched, so unstamped

    def test_failed_write_leaves_the_stored_set(self, store, monkeypatch):
        """A save which fails on disk keeps the last revision and no leftovers."""
        store.save("me", "picks", _document(), "me")

        def fail(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "rename", fail)  # moving the new set into place
        with pytest.raises(OSError, match="disk full"):
            store.save("me", "picks", _document(base_revision=1), "me")
        assert store.document("me", "picks")["revision"] == 1
        assert [x.name for x in (store.root / "me").iterdir()] == ["picks"]

    def test_empty_set(self, store):
        """A set with nothing in it is saved and read."""
        document = {"dims": DIMS, "annotations": [], "features": []}
        store.save("me", "empty", document, "me")
        assert store.document("me", "empty")["annotations"] == []

    def test_list_and_delete(self, store):
        """Sets are listed from their attributes, and deleted whole."""
        assert store.list() == []
        store.save("me", "picks", _document(acquisition_key=VIEW_KEY), "me")
        (listed,) = store.list()
        assert (listed["owner"], listed["name"]) == ("me", "picks")
        assert (listed["revision"], listed["acquisition_key"]) == (1, VIEW_KEY)
        store.delete("me", "picks")
        assert store.list() == []
        with pytest.raises(MissingSetError):
            store.delete("me", "picks")
        with pytest.raises(MissingSetError):
            store.load("me", "picks")

    @pytest.mark.parametrize(
        "change, message",
        [
            ({"dims": None}, "bad annotation document"),
            ({"annotations": "rows"}, "lists of records"),
            ({"annotations": [{"time": "2024-05-01"}]}, "needs a feature_id"),
            (
                {"annotations": [{"feature_id": "a", "time": "yesterday"}]},
                "holds a bad time",
            ),
            (
                {"features": [{"id": "track", "geometry": "circle"}]},
                "bad annotation set",
            ),
        ],
    )
    def test_bad_documents(self, store, change, message):
        """Documents which are no annotation set are refused, and nothing saved."""
        with pytest.raises(AnnotationError, match=message):
            store.save("me", "picks", {**_document(), **change}, "me")
        assert store.list() == []

    @pytest.mark.parametrize("name", ["..", "a/b", "", ".hidden", "x" * 65, "a\n"])
    def test_bad_names(self, store, name):
        """Owners and set names are plain words, never paths."""
        with pytest.raises(AnnotationError, match="not a valid"):
            store.save("me", name, _document(), "me")
        with pytest.raises(AnnotationError, match="not a valid"):
            store.load(name, "picks")


class TestFiles:
    """Tests for saving a set to a file and loading one from a file."""

    def test_export_unpacks_to_a_dascore_set(self, store, tmp_path):
        """The archive of a set is its folder, which DASCore reads."""
        store.save("me", "picks", _document(), "me")
        with zipfile.ZipFile(io.BytesIO(store.export_zip("me", "picks"))) as archive:
            names = sorted(archive.namelist())
            archive.extractall(tmp_path)
        assert names == [
            "picks/annotations.parquet",
            "picks/attrs.json",
            "picks/features.parquet",
        ]
        assert dc.annotations(tmp_path / "picks") == store.load("me", "picks")
        with pytest.raises(MissingSetError):
            store.export_zip("me", "nothing")

    def test_exported_set_loads_as_a_new_set(self, store):
        """What `export_zip` gives, `import_file` stores under another name."""
        store.save("me", "picks", _document(acquisition_key=VIEW_KEY), "me")
        payload = store.export_zip("me", "picks")
        out = store.import_file("you", "copy", "picks.zip", payload, "you")
        assert out["revision"] == 1
        copy, source = store.document("you", "copy"), store.document("me", "picks")
        assert copy["acquisition_key"] == VIEW_KEY
        assert copy["annotations"] == source["annotations"]
        labels = {x["id"]: x["label"] for x in copy["features"]}
        assert labels == {"track": "car", "area": "noise"}
        assert {x["modified_by"] for x in copy["features"]} == {"you"}
        with pytest.raises(RevisionConflictError):
            store.import_file("you", "copy", "picks.zip", payload, "you")

    @pytest.mark.parametrize("suffix", [".csv", ".parquet"])
    def test_bare_table_loads(self, store, tmp_path, suffix):
        """A table of picks alone becomes a set, each row its own feature."""
        frame = pd.DataFrame(
            {
                "time": pd.to_datetime(["2024-05-01T00:00:01", "2024-05-01T00:00:02"]),
                "distance": [10.0, 20.0],
                "phase": ["P", "S"],
            }
        )
        picks = dc.AnnotationSet(frame, dims=DIMS)
        path = tmp_path / f"picks{suffix}"
        getattr(picks.io, "to_csv" if suffix == ".csv" else "to_parquet")(path)
        store.import_file(
            "me", "picks", path.name, path.read_bytes(), "me", tuple(DIMS), VIEW_KEY
        )
        document = store.document("me", "picks")
        assert document["acquisition_key"] == VIEW_KEY
        assert [x["phase"] for x in document["annotations"]] == ["P", "S"]
        assert len({x["feature_id"] for x in document["annotations"]}) == 2
        assert len(document["features"]) == 2

    def test_archive_paths_never_choose_where_files_land(self, store, tmp_path):
        """A member named to climb out of the folder is written inside it."""
        store.save("me", "picks", _document(), "me")
        good = zipfile.ZipFile(io.BytesIO(store.export_zip("me", "picks")))
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as bad:
            for member in good.namelist():
                name = Path(member).name
                bad.writestr(f"../../escape/{name}", good.read(member))
        before = sorted(x.name for x in tmp_path.iterdir())
        store.import_file("me", "again", "x.zip", payload.getvalue(), "me")
        assert sorted(x.name for x in tmp_path.iterdir()) == before
        assert not (store.root.parent / "escape").exists()
        assert len(store.document("me", "again")["features"]) == 2

    @pytest.mark.parametrize(
        "filename, payload, message",
        [
            ("set.zip", b"not a zip", "not a zip archive"),
            ("set.txt", b"", "uploaded as .zip, .parquet or .csv"),
            ("set.parquet", b"junk", "not an annotation set"),
        ],
    )
    def test_bad_uploads(self, store, filename, payload, message):
        """Files which are no annotation set are refused, and nothing saved."""
        with pytest.raises(AnnotationError, match=message):
            store.import_file("me", "picks", filename, payload, "me", tuple(DIMS))
        assert store.list() == []

    def test_archive_without_a_set(self, store):
        """An archive of something else is refused."""
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("notes.txt", "hello")
        with pytest.raises(AnnotationError, match="holds no attrs.json"):
            store.import_file("me", "picks", "x.zip", payload.getvalue(), "me")
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("a/attrs.json", "{}")
            archive.writestr("b/attrs.json", "{}")
        with pytest.raises(AnnotationError, match="not one annotation set"):
            store.import_file("me", "picks", "x.zip", payload.getvalue(), "me")

    def test_api_round_trip(self, viewer):
        """A set saved to a file through the API loads back under a new name."""
        viewer.put("/v1/annotations/me/picks", json=_document())
        exported = viewer.get("/v1/annotations/me/picks/export")
        assert exported.headers["content-type"] == "application/zip"
        assert 'filename="picks.zip"' in exported.headers["content-disposition"]
        headers = {"Content-Type": "application/octet-stream"}
        params = {"filename": "picks.zip"}
        url = "/v1/annotations/me/copy/import"
        loaded = viewer.post(
            url, params=params, content=exported.content, headers=headers
        )
        assert loaded.json()["revision"] == 1
        again = viewer.post(
            url, params=params, content=exported.content, headers=headers
        )
        assert again.status_code == 409
        other = "/v1/annotations/you/copy/import"
        refused = viewer.post(
            other, params=params, content=exported.content, headers=headers
        )
        assert refused.status_code == 403
        junk = viewer.post(url, params={"filename": "x.zip"}, content=b"junk")
        assert junk.status_code == 400
        assert viewer.get("/v1/annotations/me/nothing/export").status_code == 404


class TestApi:
    """Tests for `/v1/annotations` as `dastor browse` serves it."""

    def test_save_read_list_delete(self, viewer):
        """A set is saved under the caller's name, read back, listed, deleted."""
        assert viewer.get("/v1/annotations").json() == {"owner": "me", "sets": []}
        saved = viewer.put("/v1/annotations/me/picks", json=_document())
        assert saved.json()["revision"] == 1
        document = viewer.get("/v1/annotations/me/picks").json()
        assert len(document["annotations"]) == 3 and document["revision"] == 1
        (listed,) = viewer.get("/v1/annotations").json()["sets"]
        assert listed["editable"] is True
        assert viewer.delete("/v1/annotations/me/picks").status_code == 204
        assert viewer.get("/v1/annotations/me/picks").status_code == 404

    def test_conflict_is_409(self, viewer):
        """A stale save answers with the current revision."""
        viewer.put("/v1/annotations/me/picks", json=_document())
        reply = viewer.put("/v1/annotations/me/picks", json=_document())
        assert reply.status_code == 409 and reply.json()["revision"] == 1

    def test_bad_document_is_400(self, viewer):
        """A document or a name the store refuses is a bad request."""
        bad_rows = {"dims": DIMS, "annotations": [1]}
        reply = viewer.put("/v1/annotations/me/picks", json=bad_rows)
        assert reply.status_code == 400
        reply = viewer.put("/v1/annotations/me/bad name", json=_document())
        assert reply.status_code == 400
        assert viewer.get("/v1/annotations").json()["sets"] == []

    def test_only_the_owner_writes(self, viewer, viewer_settings):
        """Another owner's set can be read but not changed or deleted."""
        store = AnnotationStore(viewer_settings.annotations.path)
        document = _document(acquisition_key=VIEW_KEY)
        store.save("detector", "auto", document, "detector")
        (listed,) = viewer.get("/v1/annotations").json()["sets"]
        assert listed["editable"] is False
        assert viewer.get("/v1/annotations/detector/auto").status_code == 200
        reply = viewer.put("/v1/annotations/detector/auto", json=_document(1))
        assert reply.status_code == 403
        assert viewer.delete("/v1/annotations/detector/auto").status_code == 403


class TestApiWithUsers:
    """Tests for annotations on a server with users and hidden data."""

    @pytest.fixture
    def client(self, settings, tmp_path):
        """The public and secret server, keeping annotations."""
        store = AnnotationConfig(path=tmp_path / "annotations")
        with_store = settings.model_copy(update={"annotations": store})
        with TestClient(create_app(with_store)) as out:
            yield out

    def test_anonymous_cannot_save(self, client):
        """Without a name to save under, a caller only reads."""
        assert client.get("/v1/annotations").json()["owner"] is None
        reply = client.put("/v1/annotations/anonymous/picks", json=_document())
        assert reply.status_code == 403

    def test_users_save_under_their_own_name(self, client):
        """A user owns the sets under their name; no one else changes them."""
        saved = client.put(
            "/v1/annotations/alice/picks", json=_document(), headers=auth(ALICE_KEY)
        )
        assert saved.status_code == 200
        reply = client.put(
            "/v1/annotations/alice/picks", json=_document(1), headers=auth(BOB_KEY)
        )
        assert reply.status_code == 403
        mine = client.get("/v1/annotations", headers=auth(ALICE_KEY)).json()
        assert mine["owner"] == "alice"
        assert [x["editable"] for x in mine["sets"]] == [True]

    def test_sets_need_every_file_of_their_data(self, client):
        """
        A set is shown only to those who may read all the data it is on.

        The served files name no acquisition, so a set naming none is on
        all of them: alice reads them all, bob only the public ones.
        """
        for key, owner in ((ALICE_KEY, "alice"), (BOB_KEY, "bob")):
            saved = client.put(
                f"/v1/annotations/{owner}/picks", json=_document(), headers=auth(key)
            )
            assert saved.status_code == 200
        seen = {
            who: sorted(
                x["owner"]
                for x in client.get("/v1/annotations", headers=headers).json()["sets"]
            )
            for who, headers in (
                ("alice", auth(ALICE_KEY)),
                ("bob", auth(BOB_KEY)),
                ("anonymous", {}),
            )
        }
        assert seen == {"alice": ["alice", "bob"], "bob": ["bob"], "anonymous": []}
        theirs = client.get("/v1/annotations/bob/picks", headers=auth(ALICE_KEY))
        assert theirs.status_code == 200
        hidden = client.get("/v1/annotations/alice/picks", headers=auth(BOB_KEY))
        assert hidden.status_code == 404
        assert client.get("/v1/annotations/bob/picks").status_code == 404

    def test_sets_on_missing_data_are_only_their_owners(self, client):
        """A set naming an acquisition no file has is read by its owner alone."""
        document = _document(acquisition_key="NET.GONE..A1")
        client.put("/v1/annotations/bob/picks", json=document, headers=auth(BOB_KEY))
        listing = client.get("/v1/annotations", headers=auth(ALICE_KEY)).json()
        assert listing["sets"] == []
        mine = client.get("/v1/annotations", headers=auth(BOB_KEY)).json()["sets"]
        assert [x["name"] for x in mine] == ["picks"]

    def test_no_store_is_404(self, settings):
        """A server configured without annotations has no annotation API."""
        with TestClient(create_app(settings)) as plain:
            assert plain.get("/v1/annotations").status_code == 404
