"""Syncing selected data into a local data directory, and browsing a server."""

import dascore as dc
import numpy as np
import pytest
from conftest import ALICE_KEY, free_port, live
from fastapi.testclient import TestClient

from dastor.app import create_app
from dastor.cli import main, sync_directory
from dastor.proxy import create_proxy
from dastor.sync import SyncNameError, check_name, synced_file

FIRST_BLOCK = {
    "time_min": "2024-05-01T00:00:00",
    "time_max": "2024-05-01T00:00:30",
    "distance_min": 0,
    "distance_max": 20,
}


def _products(client) -> dict:
    """Return the products a caller sees, by tag."""
    listing = client.get("/v1/viewer/products").json()
    return {x["attrs"]["tag"]: x for x in listing["products"]}


@pytest.fixture
def local(viewer_settings, tmp_path):
    """A client on the viewer's directory which syncs into a data directory."""
    with TestClient(create_app(viewer_settings, tmp_path / "mine")) as out:
        yield out


@pytest.fixture
def remote(viewer_settings):
    """The address of a live server on the viewer's directory."""
    with live(create_app(viewer_settings)) as address:
        yield address


@pytest.fixture
def proxied(remote, tmp_path):
    """A client of `dastor browse <url> --data-dir`."""
    with TestClient(create_proxy(remote, sync_dir=tmp_path / "mine")) as out:
        yield out


class TestSyncedFile:
    """A synced file appears whole, and replaces nothing."""

    def test_names_are_not_reused(self, tmp_path):
        for expected in ("area.h5", "area_1.h5", "area_2.h5"):
            with synced_file(tmp_path, "area") as out:
                out["path"].write_bytes(b"abc")
            assert out["name"] == expected and out["bytes"] == 3
        plain = tmp_path.parent / f"{tmp_path.name}-plain"
        plain.touch()  # an ordinary file: a synced one has its permissions
        assert (tmp_path / "area.h5").stat().st_mode == plain.stat().st_mode
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "area.h5",
            "area_1.h5",
            "area_2.h5",
        ]

    def test_a_failed_write_leaves_nothing(self, tmp_path):
        with pytest.raises(RuntimeError), synced_file(tmp_path, "area") as out:
            out["path"].write_bytes(b"half")
            raise RuntimeError("the connection dropped")
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("name", ["../up", "a/b", ".hidden", "", "x" * 200])
    def test_bad_names(self, name):
        if name == "":
            assert check_name(name) == "dastor"
        else:
            with pytest.raises(SyncNameError):
                check_name(name)


class TestLocalSync:
    """`dastor browse <directory> --data-dir` copies an area into the directory."""

    def test_listing_says_whether_sync_is_on(self, local, viewer):
        assert local.get("/v1/viewer/products").json()["sync"] is True
        assert viewer.get("/v1/viewer/products").json()["sync"] is False
        assert viewer.post("/v1/sync").status_code in (404, 405)

    def test_area_is_written_trimmed(self, local, tmp_path):
        raw = _products(local)["raw"]["id"]
        params = {**FIRST_BLOCK, "product": raw, "name": "first"}
        out = local.post("/v1/sync", params=params).json()
        assert out["name"] == "first.h5"
        path = tmp_path / "mine" / "first.h5"
        assert path.stat().st_size == out["bytes"] > 0
        (patch,) = dc.spool(path)
        assert patch.attrs.tag == "raw"
        time = patch.get_coord("time")
        assert time.min() == np.datetime64("2024-05-01T00:00:00")
        assert time.max() <= np.datetime64("2024-05-01T00:00:30")
        assert patch.get_coord("distance").max() <= 20
        again = local.post("/v1/sync", params=params).json()
        assert again["name"] == "first_1.h5"

    def test_several_products_in_one_file(self, local, tmp_path):
        ids = [p["id"] for p in _products(local).values()]
        params = {**FIRST_BLOCK, "product": ids}
        out = local.post("/v1/sync", params=params).json()
        tags = {p.attrs.tag for p in dc.spool(tmp_path / "mine" / out["name"])}
        assert tags == set(_products(local))

    def test_nothing_matching_and_bad_names(self, local, tmp_path):
        params = {"time_min": "2030-01-01T00:00:00"}
        assert local.post("/v1/sync", params=params).json()["name"] is None
        reply = local.post("/v1/sync", params={"name": "../escape"})
        assert reply.status_code == 400
        assert not (tmp_path / "mine").exists() or not list(
            (tmp_path / "mine").iterdir()
        )

    def test_another_website_cannot_sync(self, local, proxied, tmp_path):
        """A post a page elsewhere made the browser send is refused."""
        for client in (local, proxied):
            elsewhere = {"Origin": "https://evil.example"}
            assert client.post("/v1/sync", headers=elsewhere).status_code == 403
            own = {"Origin": "http://testserver"}
            params = {"time_min": "2030-01-01T00:00:00"}
            assert (
                client.post("/v1/sync", headers=own, params=params).status_code == 200
            )
        assert not (tmp_path / "mine").exists()

    def test_data_dir_may_not_be_inside_the_data(self, tmp_path):
        with pytest.raises(SystemExit, match="inside"):
            sync_directory(str(tmp_path / "sub"), tmp_path)
        with pytest.raises(SystemExit, match="inside"):
            sync_directory(str(tmp_path), tmp_path)
        assert sync_directory(None, tmp_path) is None
        assert sync_directory(str(tmp_path / "x"), tmp_path / "y") == tmp_path / "x"


class TestProxy:
    """`dastor browse <url>` shows the viewer on another server's data."""

    def test_requests_pass_to_the_server(self, proxied, viewer):
        assert proxied.get("/health").json() == viewer.get("/health").json()
        there = viewer.get("/v1/summary", params=FIRST_BLOCK).json()
        assert proxied.get("/v1/summary", params=FIRST_BLOCK).json() == there
        assert "<canvas" in proxied.get("/viewer/").text
        listing = proxied.get("/v1/viewer/products").json()
        assert listing["sync"] is True
        assert {p["attrs"]["tag"] for p in listing["products"]} == set(
            _products(viewer)
        )

    def test_errors_and_empty_answers_pass_back(self, proxied):
        reply = proxied.get("/v1/summary", params={"product": "nonsense"})
        assert reply.status_code == 400 and "detail" in reply.json()
        assert proxied.get("/v1/nothing-here").status_code == 404
        assert proxied.get("/elsewhere").status_code == 404
        empty = proxied.get("/v1/data", params={"time_min": "2030-01-01T00:00:00"})
        assert empty.status_code == 204

    def test_tiles_arrive_as_sent(self, proxied, viewer):
        raw = _products(viewer)["raw"]["id"]
        params = {**FIRST_BLOCK, "product": raw, "width": 32, "height": 8}
        direct = viewer.get("/v1/viewer/tile", params=params)
        passed = proxied.get("/v1/viewer/tile", params=params)
        assert passed.status_code == 200 and passed.content == direct.content
        squeezed = proxied.get(
            "/v1/viewer/tile", params=params, headers={"Accept-Encoding": "gzip"}
        )
        assert squeezed.headers["content-encoding"] == "gzip"
        assert squeezed.content == direct.content  # the client unpacks it

    def test_annotations_are_saved_on_the_server(self, proxied, viewer):
        document = {
            "dims": ["distance", "time"],
            "base_revision": 0,
            "annotations": [
                {"feature_id": "a", "time": "2024-05-01T00:00:05", "distance": 3.0}
            ],
            "features": [{"id": "a", "label": "car"}],
        }
        owner = viewer.get("/v1/annotations").json()["owner"]
        reply = proxied.put(f"/v1/annotations/{owner}/picks", json=document)
        assert reply.status_code == 200, reply.text
        stored = viewer.get(f"/v1/annotations/{owner}/picks").json()
        assert [f["label"] for f in stored["features"]] == ["car"]
        assert proxied.delete(f"/v1/annotations/{owner}/picks").status_code == 204

    def test_sync_downloads_into_the_data_directory(self, proxied, tmp_path):
        raw = _products(proxied)["raw"]["id"]
        params = {**FIRST_BLOCK, "product": raw, "name": "from_server"}
        out = proxied.post("/v1/sync", params=params).json()
        assert out["name"] == "from_server.h5"
        (patch,) = dc.spool(tmp_path / "mine" / "from_server.h5")
        assert patch.attrs.tag == "raw"
        assert patch.get_coord("distance").max() <= 20
        nothing = proxied.post("/v1/sync", params={"time_min": "2030-01-01T00:00:00"})
        assert nothing.json()["name"] is None
        assert proxied.post("/v1/sync", params={"name": "a/b"}).status_code == 400
        assert [p.name for p in (tmp_path / "mine").iterdir()] == ["from_server.h5"]

    def test_the_key_is_added_here(self, settings, tmp_path):
        """The proxy presents the key; the page never holds it."""
        with live(create_app(settings)) as address:
            with TestClient(create_proxy(address)) as anonymous:
                few = anonymous.get("/v1/summary").json()["patches"]
            with TestClient(create_proxy(address, ALICE_KEY)) as alice:
                every = alice.get("/v1/summary").json()["patches"]
            with TestClient(create_proxy(address, "wrong")) as wrong:
                assert wrong.get("/v1/summary").status_code == 401
        assert every > few > 0

    def test_a_server_which_is_down(self, tmp_path):
        address = f"http://127.0.0.1:{free_port()}"
        with TestClient(create_proxy(address, sync_dir=tmp_path)) as client:
            assert client.get("/v1/summary").status_code == 502
            assert client.post("/v1/sync").status_code == 502
        assert list(tmp_path.iterdir()) == []


class TestCommand:
    """`dastor browse` takes a URL and a data directory."""

    def test_browse_a_url(self, monkeypatch, tmp_path):
        import uvicorn  # noqa: PLC0415

        seen = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app))
        monkeypatch.setenv("DASTOR_KEY", "secret")
        argv = ["browse", "http://das.example.org", "--no-open"]
        assert main([*argv, "--data-dir", str(tmp_path)]) == 0
        assert seen["app"].title == "dastor browse"
        paths = {route.path for route in seen["app"].routes}
        assert "/v1/sync" in paths
        with pytest.raises(SystemExit, match="annotations"):
            main([*argv, "--annotations", str(tmp_path)])

    def test_browse_a_directory_with_a_data_dir(
        self, monkeypatch, viewer_dir, tmp_path
    ):
        import uvicorn  # noqa: PLC0415

        seen = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app))
        argv = ["browse", str(viewer_dir), "--no-open"]
        store = ["--annotations", str(tmp_path / "sets")]
        assert main([*argv, *store, "--data-dir", str(tmp_path / "mine")]) == 0
        assert "/v1/sync" in {route.path for route in seen["app"].routes}
        assert main([*argv, *store]) == 0
        assert "/v1/sync" not in {route.path for route in seen["app"].routes}
        with pytest.raises(SystemExit, match="inside"):
            main([*argv, *store, "--data-dir", str(viewer_dir / "sub")])
