"""The viewer served from inside a Python session, as in a notebook."""

import json
import socket
import threading
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
from dastor.cli import local_owner
from dastor.config import Limits, Settings
from dastor.notebook import Viewer

BOUNDS = {
    "time_min": "2024-05-01T00:00:00",
    "time_max": "2024-05-01T00:01:00",
    "distance_min": 0,
    "distance_max": 126,
}
DOCUMENT = {"dims": ["distance", "time"], "base_revision": 0, "annotations": []}


@pytest.fixture(scope="module")
def spool():
    """A recording at two resolutions, held in memory."""
    return dc.spool(list(_viewer_patches()))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """Start each test with no viewer and end it with none left serving."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
    monkeypatch.setattr(notebook, "DEFAULT_PORT", free)
    monkeypatch.setattr(notebook, "_shared", None)
    notebook._scratch.cache_clear()
    yield
    for viewer in list(notebook._open):
        viewer.close()


@pytest.fixture
def viewer(spool, tmp_path):
    """A viewer on the in-memory spool, on a port of its own."""
    return Viewer(spool, tmp_path / "sets", port=0)


def _get(viewer, path: str, token: bool = True, headers=(), **kwargs):
    """Return the response to a request of a viewer, by default with its token."""
    headers = dict(headers)
    if token:
        headers["Authorization"] = f"token {viewer.token}"
    request = urllib.request.Request(viewer.address + path, headers=headers, **kwargs)
    return urllib.request.urlopen(request)


def _status(viewer, path: str, **kwargs) -> int:
    """Return the status of a request made without the viewer's token."""
    try:
        return _get(viewer, path, token=False, **kwargs).status
    except urllib.error.HTTPError as error:
        return error.code


def _save(viewer, document: dict = DOCUMENT) -> str:
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


def _view_limit(viewer) -> int:
    """Return the bytes a viewer lets one screen read."""
    found = json.load(_get(viewer, "/v1/viewer/products"))["limits"]
    return found["max_view_bytes"]


class TestSpoolApp:
    """The app serving a spool it is handed rather than a directory."""

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
        assert _status(viewer, "/v1/summary") == 403
        assert _status(viewer, "/v1/summary?token=wrong") == 403
        for value in ("x", "token wrong", "Bearer wrong", f"basic {viewer.token}"):
            headers = {"Authorization": value}
            assert _status(viewer, "/v1/summary", headers=headers) == 403
        path = f"/v1/annotations/{local_owner()}/picks"
        assert _status(viewer, path, data=b"{}", method="PUT") == 403
        assert viewer.annotations() == {}
        assert _status(viewer, "/viewer/app.js") == 200

    def test_a_token_in_the_address_is_not_a_selection(self, viewer):
        """A link's `?token=` lets it in and is not read as an attribute."""
        path = f"/v1/summary?tag=raw&token={viewer.token}"
        assert json.load(_get(viewer, path, token=False))["patches"] == 2
        # nor is a header beside it read as an API key
        assert _status(viewer, path, headers={"Authorization": "Bearer old"}) == 200

    def test_shows_as_a_frame_until_closed(self, viewer):
        assert viewer.url == f"{viewer.address}/viewer/?token={viewer.token}"
        assert f'<iframe src="{viewer.url}"' in viewer._repr_html_()
        assert viewer.url in repr(viewer)
        viewer.close()
        assert "closed" in viewer._repr_html_() and "closed" in repr(viewer)
        with pytest.raises(OSError):
            _get(viewer, "/health")

    def test_serves_until_closed_though_nothing_holds_it(self, spool):
        """A viewer which is only displayed keeps answering."""
        address = Viewer(spool, port=0).address
        assert urllib.request.urlopen(f"{address}/health").status == 200

    def test_annotations_come_back(self, viewer):
        """A set saved through the page's API is read back as a DASCore set."""
        row = {"feature_id": "a", "time": "2024-05-01T00:00:05", "distance": 30.0}
        features = [{"id": "a", "label": "car"}]
        name = _save(viewer, {**DOCUMENT, "annotations": [row], "features": features})
        sets = viewer.annotations()
        assert list(sets) == [name]
        assert isinstance(sets[name], dc.AnnotationSet)
        assert len(sets[name].annotations) == 1

    def test_viewers_on_one_directory_see_each_others_saves(self, spool):
        """Of two saves based on one revision, made at once, one is refused."""
        viewers = [Viewer(spool, port=0) for _ in range(2)]
        codes = []

        def save(viewer):
            try:
                _save(viewer)
                codes.append(200)
            except urllib.error.HTTPError as error:
                codes.append(error.code)

        for _ in range(5):
            name = f"{local_owner()}/picks"
            threads = [threading.Thread(target=save, args=(x,)) for x in viewers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert sorted(codes) == [200, 409]
            codes.clear()
            _get(viewers[0], f"/v1/annotations/{name}", method="DELETE")

    def test_a_selection_is_served_as_it_is(self, spool):
        assert set(_products(Viewer(spool.select(tag="raw"), port=0))) == {"raw"}

    def test_a_directory(self, viewer_dir, tmp_path):
        out = Viewer(viewer_dir, tmp_path / "sets", port=0)
        assert set(_products(out)) == {"raw", "max_100ms"}
        with pytest.raises(NotADirectoryError):
            Viewer(tmp_path / "missing")

    def test_another_servers_spool(self, viewer):
        """A client's spool of one viewer is shown by another."""
        remote = dastor.Client(viewer.address, key=viewer.token).spool()
        local = Viewer(remote, port=0)
        params = {**BOUNDS, "product": _products(local)["max_100ms"]["id"]}
        query = urllib.parse.urlencode(params)
        assert _get(local, f"/v1/viewer/tile?{query}").status == 200

    def test_only_memory_has_no_view_limit(self, viewer, spool, viewer_dir):
        """Patches in memory lift the screen's limit; those in files keep it."""
        usual = Limits().max_view_bytes
        assert _view_limit(viewer) > usual
        assert _view_limit(Viewer(dc.spool(viewer_dir).update(), port=0)) == usual
        assert _view_limit(Viewer(spool, port=0, limits={"max_view_bytes": 9})) == 9

    def test_base_url(self, spool):
        out = Viewer(spool, port=0, base_url="/proxy/{port}/")
        assert out.address == f"/proxy/{out.port}"

    def test_a_failed_start_raises(self, spool, monkeypatch):
        def refresh(self):
            raise ValueError("no index")

        monkeypatch.setattr(Catalog, "refresh", refresh)
        with pytest.raises(RuntimeError, match="did not start"):
            Viewer(spool, port=0)
        assert not notebook._open


class TestView:
    """`dastor.view`, whose viewers take one port in turn."""

    def test_the_next_view_replaces_the_last(self, spool):
        first = dastor.view(spool)
        second = dastor.view(spool[0])
        assert "closed" in repr(first)
        assert second.port == first.port == notebook.DEFAULT_PORT
        assert len(_products(second)) == 1

    def test_a_bad_source_leaves_the_last_serving(self, spool):
        first = dastor.view(spool)
        with pytest.raises(NotADirectoryError):
            dastor.view("/no/such/directory")
        assert _get(first, "/health").status == 200

    def test_drawn_sets_outlive_their_viewer(self, spool, tmp_path):
        """What is drawn on a spool is still there when the cell runs again."""
        name = _save(dastor.view(spool))
        assert list(dastor.view(spool).annotations()) == [name]
        assert dastor.view(spool, tmp_path / "sets").annotations() == {}

    def test_a_port_keeps_both(self, spool):
        first = dastor.view(spool)
        second = dastor.view(spool, port=0)
        assert first.port != second.port
        assert _get(first, "/health").status == 200

    def test_a_taken_port_falls_back_and_is_kept(self, spool):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", notebook.DEFAULT_PORT))
            taken.listen()
            first = dastor.view(spool)
            assert first.port != notebook.DEFAULT_PORT
            assert dastor.view(spool).port == first.port
            with pytest.raises(OSError):
                dastor.view(spool, port=notebook.DEFAULT_PORT)
