"""The viewer served from inside a Python session, as in a notebook."""

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

import dascore as dc
import pytest
from conftest import _viewer_patches
from fastapi.testclient import TestClient

import dastor
from dastor import notebook
from dastor.app import create_app
from dastor.catalog import Catalog
from dastor.cli import OPEN_RULE, local_owner
from dastor.config import Limits, Settings

BOUNDS = {
    "time_min": "2024-05-01T00:00:00",
    "time_max": "2024-05-01T00:01:00",
    "distance_min": 0,
    "distance_max": 126,
}


@pytest.fixture(scope="module")
def spool():
    """A recording at two resolutions, held in memory."""
    return dc.spool(list(_viewer_patches()))


@pytest.fixture
def viewer(spool, tmp_path):
    """A viewer on the in-memory spool, on a port of its own."""
    out = notebook.Viewer(spool, tmp_path / "sets", port=0)
    yield out
    out.close()


def _get(viewer, path: str, token: bool = True, headers=(), **kwargs):
    """Return the response to a request of a viewer's API, with its token."""
    headers = dict(headers)
    if token:
        headers["Authorization"] = f"token {viewer.token}"
    request = urllib.request.Request(viewer.address + path, headers=headers, **kwargs)
    return urllib.request.urlopen(request)


def _save(viewer, document: dict) -> str:
    """Save a document as the owner's set `picks`; return the set's name."""
    name = f"{local_owner()}/picks"
    headers = {"Content-Type": "application/json"}
    body = json.dumps(document).encode()
    _get(viewer, f"/v1/annotations/{name}", data=body, headers=headers, method="PUT")
    return name


def _products(viewer) -> dict:
    """Return a viewer's products by tag."""
    products = json.load(_get(viewer, "/v1/viewer/products"))["products"]
    return {x["attrs"]["tag"]: x for x in products}


class TestSpoolApp:
    """The app serving a spool it is handed rather than a directory."""

    def test_serves_a_spool_in_memory(self, spool):
        settings = Settings.model_validate({"spool": {"rules": [OPEN_RULE]}})
        with TestClient(create_app(settings, spool)) as client:
            products = client.get("/v1/viewer/products").json()["products"]
            assert {x["attrs"]["tag"] for x in products} == {"raw", "max_100ms"}
            params = {**BOUNDS, "product": products[0]["id"]}
            assert client.get("/v1/viewer/tile", params=params).status_code == 200

    def test_rules_still_apply(self, spool):
        """A handed-in spool is read through the same rules as a directory."""
        rule = {"column": "tag", "pattern": "^raw$", "allow": ["*"]}
        settings = Settings.model_validate({"spool": {"rules": [rule]}})
        with TestClient(create_app(settings, spool)) as client:
            tags = {x["tag"] for x in client.get("/v1/contents").json()}
            assert tags == {"raw"}

    def test_needs_a_path_or_a_spool(self):
        with pytest.raises(ValueError, match="no spool path"):
            Catalog(Settings.model_validate({"spool": {}}))


class TestViewer:
    """A viewer served from a thread of this process."""

    def test_serves_the_page_and_the_data(self, viewer):
        assert _get(viewer, "/viewer/").status == 200
        products = _products(viewer)
        assert set(products) == {"raw", "max_100ms"}
        query = urllib.parse.urlencode({**BOUNDS, "product": products["raw"]["id"]})
        assert _get(viewer, f"/v1/viewer/tile?{query}").status == 200

    def test_the_api_needs_the_token(self, viewer):
        """Without the token the API is refused; the page itself is not."""
        for path in ("/v1/summary", "/v1/summary?token=wrong", "/v1/annotations"):
            with pytest.raises(urllib.error.HTTPError) as error:
                _get(viewer, path, token=False)
            assert error.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as error:
            _get(viewer, "/v1/summary", token=False, headers={"Authorization": "x"})
        assert error.value.code == 403
        assert _get(viewer, "/viewer/app.js", token=False).status == 200

    def test_a_token_in_the_address_is_not_a_selection(self, viewer):
        """A link's `?token=` lets it in and is not read as an attribute."""
        path = f"/v1/summary?tag=raw&token={viewer.token}"
        assert json.load(_get(viewer, path, token=False))["patches"] == 2

    def test_shows_as_a_frame_until_closed(self, viewer):
        assert viewer.url == f"{viewer.address}/viewer/?token={viewer.token}"
        assert f'<iframe src="{viewer.url}"' in viewer._repr_html_()
        assert viewer.url in repr(viewer)
        viewer.close()
        assert "closed" in viewer._repr_html_() and "closed" in repr(viewer)
        with pytest.raises(OSError):
            _get(viewer, "/health")

    def test_annotations_come_back(self, viewer):
        """A set saved through the page's API is read back as a DASCore set."""
        row = {"feature_id": "a", "time": "2024-05-01T00:00:05", "distance": 30.0}
        document = {
            "dims": ["distance", "time"],
            "acquisition_key": "DEMO.CABLE..A1",
            "base_revision": 0,
            "annotations": [row],
            "features": [{"id": "a", "label": "car"}],
        }
        name = _save(viewer, document)
        sets = viewer.annotations()
        assert list(sets) == [name]
        assert isinstance(sets[name], dc.AnnotationSet)
        assert len(sets[name].annotations) == 1

    def test_a_selection_and_a_patch(self, spool):
        """A spool which is a selection is served as it is, as is one patch."""
        sub = notebook.Viewer(spool.select(tag="raw"), port=0)
        one = notebook.Viewer(spool[0], port=0)
        try:
            assert set(_products(sub)) == {"raw"}
            assert len(_products(one)) == 1
        finally:
            sub.close()
            one.close()

    def test_a_directory(self, viewer_dir, tmp_path):
        out = notebook.Viewer(viewer_dir, tmp_path / "sets", port=0)
        try:
            assert set(_products(out)) == {"raw", "max_100ms"}
        finally:
            out.close()
        with pytest.raises(NotADirectoryError):
            notebook.Viewer(tmp_path / "missing")

    def test_another_servers_spool(self, viewer):
        """A client's spool of one viewer is shown by another."""
        remote = dastor.Client(viewer.address, key=viewer.token).spool()
        local = notebook.Viewer(remote, port=0)
        try:
            products = _products(local)
            params = {**BOUNDS, "product": products["max_100ms"]["id"]}
            query = urllib.parse.urlencode(params)
            assert _get(local, f"/v1/viewer/tile?{query}").status == 200
        finally:
            local.close()

    def test_memory_has_no_view_limit(self, viewer, spool, viewer_dir, tmp_path):
        """Patches in memory lift the screen's limits; files keep the server's."""
        usual = Limits().max_view_bytes
        found = json.load(_get(viewer, "/v1/viewer/products"))["limits"]
        assert found["max_view_bytes"] > usual
        for source, more in ((viewer_dir, {}), (spool, {"max_view_bytes": 10})):
            out = notebook.Viewer(source, tmp_path / "sets", port=0, limits=more)
            try:
                found = json.load(_get(out, "/v1/viewer/products"))["limits"]
                assert found["max_view_bytes"] == more.get("max_view_bytes", usual)
            finally:
                out.close()

    def test_base_url(self, spool):
        out = notebook.Viewer(spool, port=0, base_url="/proxy/{port}/")
        try:
            assert out.address == f"/proxy/{out.port}"
        finally:
            out.close()

    def test_a_failed_start_raises(self, spool, monkeypatch):
        def refresh(self):
            raise ValueError("no index")

        monkeypatch.setattr(Catalog, "refresh", refresh)
        with pytest.raises(RuntimeError, match="did not start"):
            notebook.Viewer(spool, port=0)


class TestView:
    """`dastor.view`, which keeps one viewer on the default port."""

    def test_the_next_view_replaces_the_last(self, spool):
        first = dastor.view(spool)
        second = dastor.view(spool[0])
        try:
            assert "closed" in repr(first)
            assert len(_products(second)) == 1
        finally:
            second.close()

    def test_drawn_sets_outlive_their_viewer(self, spool, monkeypatch):
        """What is drawn on a spool is still there when the cell runs again."""
        monkeypatch.setattr(notebook, "_folder", None)
        document = {"dims": ["distance", "time"], "base_revision": 0}
        name = _save(dastor.view(spool), {**document, "annotations": []})
        second = dastor.view(spool)
        try:
            assert list(second.annotations()) == [name]
            with pytest.raises(NotADirectoryError):
                dastor.view("/no/such/directory")
            assert list(second.annotations()) == [name]
        finally:
            second.close()

    def test_a_port_keeps_both(self, spool):
        first = dastor.view(spool)
        second = dastor.view(spool, port=0)
        try:
            assert first.port != second.port
            assert _get(first, "/health").status == 200
        finally:
            first.close()
            second.close()

    def test_a_taken_port_falls_back(self, spool, monkeypatch):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            monkeypatch.setattr(notebook, "DEFAULT_PORT", taken.getsockname()[1])
            out = dastor.view(spool)
            try:
                assert out.port != taken.getsockname()[1]
                assert _get(out, "/health").status == 200
            finally:
                out.close()
            with pytest.raises(OSError):
                dastor.view(spool, port=taken.getsockname()[1])
