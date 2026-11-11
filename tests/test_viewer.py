"""Tests for the viewer's products and tiles."""

import dascore as dc
import numpy as np
import pytest
from conftest import ALICE_KEY, VIEW_KEY, auth

from dastor.auth import ANONYMOUS
from dastor.cli import browse_settings
from dastor.query import QueryError
from dastor.tiles import (
    decode_product,
    encode_product,
    list_products,
    product_rows,
    render_tile,
)

FIRST_BLOCK = {
    "time_min": "2024-05-01T00:00:00",
    "time_max": "2024-05-01T00:01:00",
    "distance_min": 0,
    "distance_max": 128,
}


def _products(client, **kwargs) -> dict:
    """Return the products a caller sees, by tag."""
    listing = client.get("/v1/viewer/products", **kwargs).json()
    return {x["attrs"]["tag"]: x for x in listing["products"]}


def _tile(reply, shape) -> np.ndarray:
    """Return a tile response as an array of (y, x)."""
    return np.frombuffer(reply.content, "<f4").reshape(shape)


@pytest.fixture(scope="module")
def patch():
    """A patch of 300 distances by 2000 times, 1 m by 4 ms."""
    return dc.get_example_patch("random_das")


@pytest.fixture(scope="module")
def ranges(patch):
    """The x and y ranges covering the whole patch, as numbers."""
    time = patch.get_array("time").astype("int64")
    return (int(time[0]), int(time[0]) + 2000 * 4_000_000), (0.0, 300.0)


class TestProducts:
    """Tests for grouping contents into products."""

    def test_one_product_per_kind_and_resolution(self, viewer):
        """The recording's two resolutions are two products of one acquisition."""
        products = _products(viewer)
        assert set(products) == {"raw", "max_100ms"}
        raw = products["raw"]
        assert raw["attrs"]["acquisition_key"] == VIEW_KEY
        assert (raw["x"], raw["y"], raw["viewable"]) == ("time", "distance", True)
        assert raw["coords"]["time"]["step"] == pytest.approx(0.01)
        assert raw["coords"]["distance"] == {
            "timelike": False,
            "step": 2.0,
            "units": "m",
            "min": 0.0,
            "max": 126.0,
            "spans": [[0.0, 126.0]],
        }

    def test_spans_show_the_gap(self, viewer):
        """A product's time spans are its recorded blocks."""
        spans = _products(viewer)["raw"]["coords"]["time"]["spans"]
        assert [x[0][:19] for x in spans] == [
            "2024-05-01T00:00:00",
            "2024-05-01T00:03:00",
        ]

    def test_limits_are_listed(self, viewer, viewer_settings):
        """The listing states the limits the viewer keeps to."""
        limits = viewer.get("/v1/viewer/products").json()["limits"]
        assert limits["max_tile_bytes"] == viewer_settings.limits.max_tile_bytes
        assert limits["max_view_bytes"] == viewer_settings.limits.max_view_bytes

    def test_id_round_trips(self, viewer):
        """A product id decodes to what selects the product's rows."""
        raw = _products(viewer)["raw"]
        decoded = decode_product(raw["id"])
        assert encode_product(decoded) == raw["id"]
        contents = viewer.app.state.catalog.view(ANONYMOUS).spool.get_contents()
        rows = product_rows(contents, decoded)
        assert set(contents["tag"][rows]) == {"raw"} and rows.sum() == 2

    def test_bad_id(self):
        """Text which is no product id is refused."""
        with pytest.raises(QueryError, match="not a product id"):
            decode_product("nonsense")

    def test_empty_contents(self, viewer):
        """No contents hold no products."""
        contents = viewer.app.state.catalog.view(ANONYMOUS).spool.get_contents()
        assert list_products(contents.iloc[:0]) == []

    def test_hidden_products_are_not_listed(self, client):
        """A caller's products come only from the files they may read."""
        assert set(_products(client)) == {"public"}
        assert set(_products(client, headers=auth(ALICE_KEY))) == {"public", "secret"}


class TestRenderTile:
    """Tests for reducing patches to an image."""

    @pytest.mark.parametrize("stat", ["max", "mean", "absmax"])
    def test_statistics(self, patch, ranges, stat):
        """Each bin holds the statistic of the samples inside it."""
        out = render_tile([patch], "time", "distance", *ranges, (30, 20), stat)
        blocks = patch.transpose("distance", "time").data.reshape(30, 10, 20, 100)
        expected = {
            "max": blocks.max((1, 3)),
            "mean": blocks.mean((1, 3)),
            "absmax": np.abs(blocks).max((1, 3)),
        }[stat]
        assert np.allclose(out, expected, rtol=1e-5)

    def test_bins_finer_than_the_data_repeat_samples(self, patch):
        """Where the data is coarser than the bins, each sample fills its cell."""
        part = patch.select(time=(0, 3), distance=(0, 2), samples=True)
        data = part.transpose("distance", "time").data
        rows, columns = data.shape
        start = int(part.get_array("time").astype("int64")[0])
        x_range = (start, start + columns * 4_000_000)
        shape = (rows * 4, columns * 4)
        out = render_tile([part], "time", "distance", x_range, (0.0, rows), shape)
        assert np.allclose(out, np.repeat(np.repeat(data, 4, 0), 4, 1))

    def test_bins_outside_the_data_are_nan(self, patch, ranges):
        """Bins the patch doesn't reach hold no value."""
        (low, high), _ = ranges
        wide = (low - (high - low), high)
        out = render_tile([patch], "time", "distance", wide, (-300.0, 300.0), (4, 4))
        assert np.isnan(out[:2]).all() and np.isnan(out[:, :2]).all()
        assert np.isfinite(out[2:, 2:]).all()

    def test_no_data_in_range(self, patch):
        """A range the patches miss gives no image."""
        out = render_tile([patch], "time", "distance", (0, 10), (0.0, 300.0), (4, 4))
        assert out is None

    def test_patches_are_combined(self, patch, ranges):
        """Two patches side by side fill one image."""
        (low, high), y_range = ranges
        parts = [
            patch.select(time=(0, 1000), samples=True),
            patch.select(time=(1000, None), samples=True),
        ]
        whole = render_tile([patch], "time", "distance", (low, high), y_range, (8, 8))
        for stat in ("max", "mean"):
            args = ("time", "distance", (low, high), y_range, (8, 8), stat)
            assert np.allclose(render_tile(parts, *args), render_tile([patch], *args))
        assert np.isfinite(whole).all()

    def test_other_dims_are_skipped(self, patch, ranges):
        """A patch lacking the tile's dims adds nothing."""
        other = patch.rename_coords(distance="depth")
        assert render_tile([other], "time", "distance", *ranges, (4, 4)) is None


class TestTileEndpoint:
    """Tests for `/v1/viewer/tile`."""

    def test_tile_of_each_level(self, viewer):
        """A tile is the product's values reduced to the asked shape."""
        products = _products(viewer)
        tiles = {}
        for tag, product in products.items():
            params = {"product": product["id"], "width": 60, "height": 16}
            reply = viewer.get("/v1/viewer/tile", params={**params, **FIRST_BLOCK})
            assert reply.status_code == 200
            tiles[tag] = _tile(reply, (16, 60))
        # the coarse product is the raw one's largest magnitude in each cell
        assert np.isfinite(tiles["raw"]).all()
        assert (tiles["max_100ms"] >= tiles["raw"] - 1e-5).all()

    def test_statistic(self, viewer):
        """`stat` picks what a bin shows."""
        product = _products(viewer)["raw"]["id"]
        params = {"product": product, "width": 8, "height": 8, **FIRST_BLOCK}
        mean = viewer.get("/v1/viewer/tile", params={**params, "stat": "mean"})
        peak = viewer.get("/v1/viewer/tile", params={**params, "stat": "max"})
        assert (_tile(mean, (8, 8)) < _tile(peak, (8, 8))).all()

    def test_no_data_is_204(self, viewer):
        """A tile in the gap between the blocks is empty."""
        params = {
            **FIRST_BLOCK,
            "product": _products(viewer)["raw"]["id"],
            "time_min": "2024-05-01T00:01:30",
            "time_max": "2024-05-01T00:02:30",
        }
        assert viewer.get("/v1/viewer/tile", params=params).status_code == 204

    def test_bounds_are_required(self, viewer):
        """A tile needs both dims bounded on both sides."""
        product = _products(viewer)["raw"]["id"]
        params = {"product": product, "time_min": "2024-05-01"}
        reply = viewer.get("/v1/viewer/tile", params=params)
        assert reply.status_code == 400
        params = {**FIRST_BLOCK, "product": product}
        del params["distance_max"]
        params["distance_min"] = 0
        assert viewer.get("/v1/viewer/tile", params=params).status_code == 400

    def test_bad_product_is_400(self, viewer):
        """An id which is no product is a bad request."""
        params = {**FIRST_BLOCK, "product": "nonsense"}
        assert viewer.get("/v1/viewer/tile", params=params).status_code == 400

    def test_too_many_samples_is_413(self, viewer_settings, viewer_dir):
        """A tile over the limit is refused before anything loads."""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from dastor.app import create_app  # noqa: PLC0415
        from dastor.config import Limits  # noqa: PLC0415

        limits = Limits(max_tile_bytes=1000)
        settings = viewer_settings.model_copy(update={"limits": limits})
        with TestClient(create_app(settings)) as client:
            params = {**FIRST_BLOCK, "product": _products(client)["raw"]["id"]}
            assert client.get("/v1/viewer/tile", params=params).status_code == 413

    def test_hidden_product_has_no_tiles(self, client):
        """A product the caller may not read gives an empty tile."""
        secret = _products(client, headers=auth(ALICE_KEY))["secret"]
        coords = secret["coords"]
        params = {
            "product": secret["id"],
            "time_min": coords["time"]["min"],
            "time_max": coords["time"]["max"],
            "distance_min": 0,
            "distance_max": 300,
        }
        allowed = client.get("/v1/viewer/tile", params=params, headers=auth(ALICE_KEY))
        assert allowed.status_code == 200
        assert client.get("/v1/viewer/tile", params=params).status_code == 204


class TestViewerPage:
    """Tests for serving the page and for `dastor browse`."""

    def test_page_is_served(self, viewer):
        """The viewer's page and script are static files under /viewer/."""
        assert "dastor viewer" in viewer.get("/viewer/").text
        assert viewer.get("/viewer/app.js").status_code == 200

    def test_summary_estimates_bytes(self, viewer):
        """The summary sizes a selection, as the viewer's download panel shows."""
        whole = viewer.get("/v1/summary", params={"tag": "raw"}).json()
        assert whole["bytes"] == 2 * 64 * 6000 * 4
        part = viewer.get("/v1/summary", params={"tag": "raw", **FIRST_BLOCK}).json()
        assert 0 < part["bytes"] < whole["bytes"]

    def test_browse_settings(self, tmp_path):
        """`dastor browse` reads everything and keeps annotations beside the data."""
        data = tmp_path / "data"
        data.mkdir()
        config = browse_settings(str(data))
        assert config["spool"]["rules"][0]["allow"] == ["*"]
        assert config["annotations"]["path"] == str(tmp_path / "data_annotations")
        assert config["annotations"]["anonymous_owner"]
        assert browse_settings(str(data), "x")["annotations"]["path"] == "x"

    def test_browse_needs_a_directory(self, tmp_path):
        """A path which is no directory ends the command."""
        with pytest.raises(SystemExit, match="not a directory"):
            browse_settings(str(tmp_path / "missing"))
