"""Tests for raw data served as level 0 of the recordings it is linked to."""

import dascore as dc
import numpy as np
import pandas as pd
import pytest
from conftest import VIEW_KEY
from fastapi.testclient import TestClient

from dastor.app import create_app
from dastor.auth import ANONYMOUS
from dastor.catalog import RAW_TAG, read_links
from dastor.config import RawConfig

FIRST_BLOCK = {
    "time_min": "2024-05-01T00:00:00",
    "time_max": "2024-05-01T00:00:10",
    "distance_min": 0,
    "distance_max": 128,
}


def _raw_patch(value: float) -> dc.Patch:
    """Ten seconds at 1 ms of 64 channels, holding one value, with no recording."""
    time = np.datetime64("2024-05-01T00:00:00") + np.arange(10_000) * np.timedelta64(
        1, "ms"
    )
    return dc.Patch(
        data=np.full((64, 10_000), value, dtype="float32"),
        coords={"distance": np.arange(64) * 2.0, "time": time},
        dims=("distance", "time"),
    )


@pytest.fixture
def raw_settings(viewer_settings, tmp_path):
    """Settings with a raw directory: one file linked to the recording, one not."""
    raw = tmp_path / "raw"
    (raw / "linked").mkdir(parents=True)
    (raw / "other").mkdir()
    dc.write(_raw_patch(3.0), raw / "linked" / "a.h5", "DASDAE")
    dc.write(_raw_patch(7.0), raw / "other" / "b.h5", "DASDAE")
    links = tmp_path / "links.csv"
    pd.DataFrame(
        {
            "source_path": ["linked/a.h5"],
            "acquisition_key": [VIEW_KEY],
            "network": [""],
            "station": [""],
        }
    ).to_csv(links, index=False)
    dc.spool(raw, index=tmp_path / "raw.index").update()  # built beforehand
    config = viewer_settings.model_dump(mode="json")
    config["spool"]["raw"] = {
        "path": str(raw),
        "index": str(tmp_path / "raw.index"),
        "links": str(links),
    }
    return type(viewer_settings).model_validate(config)


def _raw_products(client) -> list[dict]:
    listing = client.get("/v1/viewer/products").json()["products"]
    return [p for p in listing if p.get("raw")]


def test_linked_raw_is_level_zero_of_its_recording(raw_settings):
    """Only the linked file is offered, as the recording's raw product."""
    with TestClient(create_app(raw_settings)) as client:
        found = _raw_products(client)
        assert len(found) == 1
        product = found[0]
        assert product["attrs"]["acquisition_key"] == VIEW_KEY
        assert product["attrs"]["tag"] == RAW_TAG
        assert product["steps"]["time"] == 1_000_000
        params = {"product": product["id"], "width": 20, "height": 8, **FIRST_BLOCK}
        reply = client.get("/v1/viewer/tile", params=params)
        assert reply.status_code == 200
        tile = np.frombuffer(reply.content, "<f4")
        # the linked file's values, never the unlinked one's
        assert np.nanmax(tile) == 3.0 and np.isfinite(tile).any()


def _with_rules(settings, rules):
    config = settings.model_dump(mode="json")
    config["spool"]["rules"] = rules
    return type(settings).model_validate(config)


def test_raw_follows_the_access_rules(raw_settings):
    """Raw files the rules don't grant are neither listed nor drawn."""
    with TestClient(create_app(raw_settings)) as client:
        product = _raw_products(client)[0]["id"]  # known from a reader who may
    denied = _with_rules(
        raw_settings,
        [{"column": "source_path", "pattern": "^(raw|max)_", "allow": ["*"]}],
    )
    with TestClient(create_app(denied)) as client:
        assert _raw_products(client) == []
        listing = client.get("/v1/viewer/products").json()["products"]
        assert listing  # the pyramid's own products are still served
        params = {"product": product, "width": 20, "height": 8, **FIRST_BLOCK}
        assert client.get("/v1/viewer/tile", params=params).status_code == 204
        assert (
            client.get("/v1/summary", params={"product": product}).json()["patches"]
            == 0
        )


def test_raw_access_is_judged_on_the_files_own_attributes(raw_settings):
    """A file holding a secret stays hidden, whatever its link makes it look like."""
    raw = raw_settings.spool.raw
    mixed = dc.spool(
        [
            _raw_patch(1.0).update_attrs(tag="PUBLIC"),
            _raw_patch(9.0).update_attrs(tag="SECRET"),
        ]
    )
    dc.write(mixed, raw.path / "linked" / "mixed.h5", "DASDAE")
    dc.spool(raw.path, index=raw.index).update()
    links = pd.read_csv(raw.links, dtype=str, keep_default_na=False)
    row = {"source_path": "linked/mixed.h5", "acquisition_key": VIEW_KEY}
    pd.concat([links, pd.DataFrame([{**row, "network": "", "station": ""}])]).to_csv(
        raw.links, index=False
    )
    # granted: the recording's own products, and raw rows tagged PUBLIC; a link
    # rewrites the tag to "raw DAS", which this rule would have let through
    rule = {"column": "tag", "pattern": "^(raw|max_100ms|PUBLIC)", "allow": ["*"]}
    with TestClient(create_app(_with_rules(raw_settings, [rule]))) as client:
        view = client.app.state.catalog.view(ANONYMOUS)
        assert "linked/mixed.h5" not in view.raw_files  # not every row is granted
        assert "linked/a.h5" not in view.raw_files  # untagged: not granted either
        assert _raw_products(client) == []


def test_annotations_need_the_raw_files_too(raw_settings):
    """An acquisition is readable about only when its linked raw files are."""
    with TestClient(create_app(raw_settings)) as client:
        assert VIEW_KEY in client.app.state.catalog.view(ANONYMOUS).acquisitions
    denied = _with_rules(
        raw_settings,
        [{"column": "source_path", "pattern": "^(raw|max)_", "allow": ["*"]}],
    )
    with TestClient(create_app(denied)) as client:
        assert VIEW_KEY not in client.app.state.catalog.view(ANONYMOUS).acquisitions


def test_raw_and_other_products_together(raw_settings):
    """Asked for with another product, the raw data is not left out."""
    with TestClient(create_app(raw_settings)) as client:
        raw = _raw_products(client)[0]["id"]
        listing = client.get("/v1/viewer/products").json()["products"]
        coarse = next(p for p in listing if p["attrs"]["tag"] == "max_100ms")["id"]

        def count(*products):
            params = [("product", x) for x in products]
            return client.get("/v1/summary", params=params).json()["patches"]

        assert count(raw, coarse) == count(raw) + count(coarse) > count(coarse)


def test_links_read_as_written(tmp_path):
    """A CSV's identifiers stay text: no number parsing, blanks stay blank."""
    path = tmp_path / "links.csv"
    path.write_text(
        "source_path,acquisition_key,network,station\na.h5,NET.ARR..0012,,0012\n"
    )
    config = RawConfig(path=tmp_path, links=path)
    table = read_links(config)
    assert table.loc["a.h5", "station"] == "0012"
    assert table.loc["a.h5", "network"] == ""
    path.write_text("source_path,acquisition_key,network,station\na.h5,,,x\n")
    with pytest.raises(ValueError, match="no acquisition_key"):
        read_links(config)


def test_no_raw_configured_lists_none(viewer):
    """Without a raw directory no product is raw."""
    listing = viewer.get("/v1/viewer/products").json()["products"]
    assert listing and not any(p.get("raw") for p in listing)
