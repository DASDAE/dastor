"""Tests for stored annotation sets and their API."""

import dascore as dc
import pytest
from conftest import ALICE_KEY, BOB_KEY, VIEW_KEY, auth
from fastapi.testclient import TestClient

from dastor.annotations import (
    AnnotationError,
    AnnotationStore,
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
        with pytest.raises(FileNotFoundError):
            store.delete("me", "picks")
        with pytest.raises(FileNotFoundError):
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

    @pytest.mark.parametrize("name", ["..", "a/b", "", ".hidden", "x" * 65])
    def test_bad_names(self, store, name):
        """Owners and set names are plain words, never paths."""
        with pytest.raises(AnnotationError, match="not a valid"):
            store.save("me", name, _document(), "me")
        with pytest.raises(AnnotationError, match="not a valid"):
            store.load(name, "picks")


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
        """A document which is no annotation set is a bad request."""
        reply = viewer.put("/v1/annotations/me/picks", json={"dims": DIMS, "x": 1})
        assert reply.status_code == 200  # an empty set
        reply = viewer.put("/v1/annotations/me/bad name", json=_document())
        assert reply.status_code == 400

    def test_only_the_owner_writes(self, viewer, viewer_settings):
        """Another owner's set can be read but not changed or deleted."""
        store = AnnotationStore(viewer_settings.annotations.path)
        store.save("detector", "auto", _document(), "detector")
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
        """A user owns the sets under their name, and others read them."""
        saved = client.put(
            "/v1/annotations/alice/picks", json=_document(), headers=auth(ALICE_KEY)
        )
        assert saved.status_code == 200
        reply = client.put(
            "/v1/annotations/alice/picks", json=_document(1), headers=auth(BOB_KEY)
        )
        assert reply.status_code == 403
        listing = client.get("/v1/annotations", headers=auth(BOB_KEY)).json()
        assert listing["owner"] == "bob"
        assert [x["editable"] for x in listing["sets"]] == [False]

    def test_sets_on_unreadable_data_are_hidden(self, client):
        """A set made on an acquisition the caller can't read isn't shown."""
        document = _document(acquisition_key="NET.HIDDEN..A1")
        client.put(
            "/v1/annotations/alice/picks", json=document, headers=auth(ALICE_KEY)
        )
        assert client.get("/v1/annotations", headers=auth(BOB_KEY)).json()["sets"] == []
        reply = client.get("/v1/annotations/alice/picks", headers=auth(BOB_KEY))
        assert reply.status_code == 404
        mine = client.get("/v1/annotations", headers=auth(ALICE_KEY)).json()["sets"]
        assert [x["name"] for x in mine] == ["picks"]

    def test_no_store_is_404(self, settings):
        """A server configured without annotations has no annotation API."""
        with TestClient(create_app(settings)) as plain:
            assert plain.get("/v1/annotations").status_code == 404
