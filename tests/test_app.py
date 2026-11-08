import io

import dascore as dc
import pyarrow as pa
import pytest
from conftest import ALICE_KEY, BOB_KEY, auth
from fastapi.testclient import TestClient

from dastor.app import create_app
from dastor.config import Settings
from dastor.wire import decode_patch, read_framed

WINDOW = {"time_min": "2020-01-03T00:00:02", "time_max": "2020-01-03T00:00:05"}


def tags(client, headers=None, **params):
    response = client.get("/v1/contents", headers=headers, params=params)
    assert response.status_code == 200
    return sorted(x["tag"] for x in response.json())


class TestAccess:
    def test_anonymous_sees_whole_public_files_only(self, client):
        # The mixed file's public patch stays hidden with its secret one.
        assert tags(client) == ["public", "public"]
        assert tags(client, auth(BOB_KEY)) == ["public", "public"]

    def test_group_sees_everything(self, client):
        assert tags(client, auth(ALICE_KEY)) == ["public"] * 3 + ["secret"] * 2

    def test_rules_can_match_paths(self, tmp_path):
        # Datasets in subdirectories of the one spool, granted by path.
        for folder in ("open", "closed"):
            (tmp_path / folder).mkdir()
            dc.write(dc.get_example_patch(), tmp_path / folder / "a.h5", "DASDAE")
        rule = {"column": "source_path", "pattern": "^open/", "allow": ["*"]}
        config = {"spool": {"path": str(tmp_path), "rules": [rule]}}
        with TestClient(create_app(Settings.model_validate(config))) as client:
            rows = client.get("/v1/contents").json()
        assert [x["source_path"] for x in rows] == ["open/a.h5"]

    def test_bad_key_is_unauthorized(self, client):
        response = client.get("/v1/contents", headers=auth("wrong"))
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_data_never_includes_hidden_patches(self, client):
        response = client.get("/v1/data", params={"tag": "secret"})
        assert response.status_code == 204
        patches = read(client.get("/v1/data"))
        assert {x.attrs.tag for x in patches} == {"public"}

    def test_coverage_respects_access(self, client):
        def covered(headers=None):
            got = client.get("/v1/coverage", headers=headers).json()
            return {x["tag"] for x in got}

        assert covered() == {"public"}
        assert covered(auth(ALICE_KEY)) == {"public", "secret"}


def read(response):
    assert response.status_code == 200
    return list(read_framed(io.BytesIO(response.content)))


class TestContents:
    def test_query_narrows(self, client):
        assert tags(client, auth(ALICE_KEY), tag="sec*") == ["secret", "secret"]

    def test_arrow_format(self, client):
        response = client.get("/v1/contents", params={"format": "arrow"})
        table = pa.ipc.open_stream(response.content).read_all()
        assert table.num_rows == 2
        assert pa.types.is_timestamp(table.schema.field("time_min").type)

    def test_json_times(self, client):
        row = client.get("/v1/contents").json()[0]
        assert row["time_min"].startswith("2020-01-03T00:00:00")
        assert row["time_step"] == pytest.approx(0.004)

    @pytest.mark.parametrize(
        "params",
        [
            [("tag", "a"), ("tag", "b")],
            {"time_min": "not a time"},
            {"distance_min": "abc"},
            {"no_such_attr": "x"},
        ],
    )
    def test_bad_query_is_400(self, client, params):
        response = client.get("/v1/contents", params=params)
        assert response.status_code == 400

    def test_gaps(self, client):
        response = client.get("/v1/gaps", headers=auth(ALICE_KEY))
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestData:
    def test_arrow_matches_local_selection(self, client, data_dir):
        # Only file_0 overlaps the window once the mixed file is hidden.
        local = dc.spool(data_dir / "file_0.h5").select(time=tuple(WINDOW.values()))
        got = read(client.get("/v1/data", params=WINDOW))
        assert got == list(local)

    def test_group_gets_more_data(self, client):
        got = read(client.get("/v1/data", params=WINDOW, headers=auth(ALICE_KEY)))
        assert sorted(x.attrs.tag for x in got) == ["public", "public"]

    def test_dasdae_format(self, client, tmp_path):
        params = {**WINDOW, "format": "dasdae"}
        response = client.get("/v1/data", params=params)
        assert response.status_code == 200
        path = tmp_path / "out.h5"
        path.write_bytes(response.content)
        assert len(dc.spool(path)) == 1

    def test_size_limit(self, settings):
        limits = settings.limits.model_copy(update={"max_request_bytes": 1000})
        small = settings.model_copy(update={"limits": limits})
        with TestClient(create_app(small)) as client:
            response = client.get("/v1/data")
        assert response.status_code == 413
        assert "limit is 1000" in response.json()["detail"]


def contents_ids(client, headers=None, **params):
    rows = client.get("/v1/contents", headers=headers, params=params).json()
    return [x["data_id"] for x in rows]


class TestPatch:
    def test_whole_patch_by_id(self, client, data_dir):
        local = dc.spool(data_dir / "file_0.h5")[0]
        response = client.get(f"/v1/patch/{local.attrs.data_id}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/vnd.apache.arrow.stream"
        assert decode_patch(response.content) == local

    def test_hidden_patch_is_404(self, client):
        (secret, *_) = contents_ids(client, auth(ALICE_KEY), tag="secret")
        assert client.get(f"/v1/patch/{secret}").status_code == 404
        assert client.get(f"/v1/patch/{secret}", headers=auth(ALICE_KEY)).is_success

    @pytest.mark.parametrize(("data_id", "status"), [("0" * 32, 404), ("*", 422)])
    def test_bad_ids(self, client, data_id, status):
        assert client.get(f"/v1/patch/{data_id}").status_code == status

    def test_size_limit(self, settings):
        limits = settings.limits.model_copy(update={"max_request_bytes": 1000})
        small = settings.model_copy(update={"limits": limits})
        with TestClient(create_app(small)) as client:
            (data_id, *_) = contents_ids(client)
            assert client.get(f"/v1/patch/{data_id}").status_code == 413


class TestSummary:
    def test_counts_only_visible_patches(self, client):
        assert client.get("/v1/summary").json()["patches"] == 2
        everything = client.get("/v1/summary", headers=auth(ALICE_KEY)).json()
        assert (everything["patches"], everything["files"]) == (5, 4)

    def test_dims_and_extents(self, client):
        got = client.get("/v1/summary").json()
        assert got["dims"] == [["distance", "time"]]
        assert "tag" in got["columns"]
        (time,) = got["extents"]["time"]
        assert time["min"].startswith("2020-01-03T00:00:00")
        assert time["max"] > time["min"]
        assert got["extents"]["distance"] == [{"units": "m", "min": 0, "max": 299}]

    def test_extents_split_by_units(self, tmp_path):
        patch = dc.get_example_patch().update_attrs(tag="x")
        dc.write(patch, tmp_path / "m.h5", "DASDAE")
        km = patch.convert_units(distance="km").update_coords(distance_min=100)
        dc.write(km, tmp_path / "km.h5", "DASDAE")
        rule = {"column": "tag", "pattern": "", "allow": ["*"]}
        config = {"spool": {"path": str(tmp_path), "rules": [rule]}}
        with TestClient(create_app(Settings.model_validate(config))) as client:
            got = client.get("/v1/summary").json()["extents"]["distance"]
        assert [(x["units"], x["min"]) for x in got] == [("km", 100), ("m", 0)]

    def test_query_narrows(self, client):
        got = client.get("/v1/summary", params={"tag": "nope"}).json()
        assert (got["patches"], got["files"], got["extents"]) == (0, 0, {})


def test_hidden_files_columns_are_not_shown(tmp_path):
    patch = dc.get_example_patch()
    dc.write(patch.update_attrs(tag="public"), tmp_path / "a.h5", "DASDAE")
    secret = patch.update_attrs(tag="secret", private_project="x")
    dc.write(secret, tmp_path / "b.h5", "DASDAE")
    rule = {"column": "tag", "pattern": "^public$", "allow": ["*"]}
    config = {"spool": {"path": str(tmp_path), "rules": [rule]}}
    with TestClient(create_app(Settings.model_validate(config))) as client:
        assert "private_project" not in client.get("/v1/summary").json()["columns"]
        (row,) = client.get("/v1/contents").json()
        assert "private_project" not in row
        arrow = client.get("/v1/contents", params={"format": "arrow"}).content
        assert "private_project" not in pa.ipc.open_stream(arrow).schema.names
        nothing = client.get("/v1/summary", params={"tag": "nope"}).json()
        assert nothing["columns"] == []


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"
