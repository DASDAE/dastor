import io

import dascore as dc
import pyarrow as pa
import pytest
from conftest import ALICE_KEY, BOB_KEY, auth
from fastapi.testclient import TestClient

from dastor.app import create_app
from dastor.wire import read_framed

WINDOW = {"time_min": "2020-01-03T00:00:02", "time_max": "2020-01-03T00:00:05"}


def tags(client, headers=None, **params):
    response = client.get("/v1/spools/das/contents", headers=headers, params=params)
    assert response.status_code == 200
    return sorted(x["tag"] for x in response.json())


class TestAccess:
    def test_spool_listing(self, client):
        assert client.get("/v1/spools").json() == ["das"]
        assert client.get("/v1/spools", headers=auth(ALICE_KEY)).json() == [
            "das",
            "lab_only",
        ]

    def test_anonymous_sees_whole_public_files_only(self, client):
        # The mixed file's public patch stays hidden with its secret one.
        assert tags(client) == ["public", "public"]
        assert tags(client, auth(BOB_KEY)) == ["public", "public"]

    def test_group_sees_everything(self, client):
        assert tags(client, auth(ALICE_KEY)) == ["public"] * 3 + ["secret"] * 2

    def test_hidden_spool_is_not_found(self, client):
        assert client.get("/v1/spools/lab_only/contents").status_code == 404
        assert client.get("/v1/spools/nope/contents").status_code == 404
        ok = client.get("/v1/spools/lab_only/contents", headers=auth(ALICE_KEY))
        assert ok.status_code == 200

    def test_bad_key_is_unauthorized(self, client):
        response = client.get("/v1/spools", headers=auth("wrong"))
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_data_never_includes_hidden_patches(self, client):
        response = client.get("/v1/spools/das/data", params={"tag": "secret"})
        assert response.status_code == 204
        patches = read(client.get("/v1/spools/das/data"))
        assert {x.attrs.tag for x in patches} == {"public"}

    def test_coverage_respects_access(self, client):
        def covered(headers=None):
            got = client.get("/v1/spools/das/coverage", headers=headers).json()
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
        response = client.get("/v1/spools/das/contents", params={"format": "arrow"})
        table = pa.ipc.open_stream(response.content).read_all()
        assert table.num_rows == 2
        assert pa.types.is_timestamp(table.schema.field("time_min").type)

    def test_json_times(self, client):
        row = client.get("/v1/spools/das/contents").json()[0]
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
        response = client.get("/v1/spools/das/contents", params=params)
        assert response.status_code == 400

    def test_gaps(self, client):
        response = client.get("/v1/spools/das/gaps", headers=auth(ALICE_KEY))
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestData:
    def test_arrow_matches_local_selection(self, client, data_dir):
        # Only file_0 overlaps the window once the mixed file is hidden.
        local = dc.spool(data_dir / "file_0.h5").select(time=tuple(WINDOW.values()))
        got = read(client.get("/v1/spools/das/data", params=WINDOW))
        assert got == list(local)

    def test_group_gets_more_data(self, client):
        got = read(
            client.get("/v1/spools/das/data", params=WINDOW, headers=auth(ALICE_KEY))
        )
        assert sorted(x.attrs.tag for x in got) == ["public", "public"]

    def test_dasdae_format(self, client, tmp_path):
        params = {**WINDOW, "format": "dasdae"}
        response = client.get("/v1/spools/das/data", params=params)
        assert response.status_code == 200
        path = tmp_path / "out.h5"
        path.write_bytes(response.content)
        assert len(dc.spool(path)) == 1

    def test_size_limit(self, settings):
        limits = settings.limits.model_copy(update={"max_request_bytes": 1000})
        small = settings.model_copy(update={"limits": limits})
        with TestClient(create_app(small)) as client:
            response = client.get("/v1/spools/das/data")
        assert response.status_code == 413
        assert "limit is 1000" in response.json()["detail"]


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"
