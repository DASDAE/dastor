"""Files and indexes that change while the server runs."""

import glob
import io
import tempfile

import dascore as dc
import pytest
from fastapi.testclient import TestClient

from dastor.app import create_app
from dastor.auth import ANONYMOUS
from dastor.catalog import Catalog, ChangedFileError
from dastor.config import Settings
from dastor.wire import read_framed

PUBLIC = [{"column": "access", "pattern": "^public$", "allow": ["*"]}]


def settings_for(path, limit=2 * 1024**3):
    spool = {"path": str(path), "rules": PUBLIC}
    limits = {"max_request_bytes": limit}
    return Settings.model_validate({"spool": spool, "limits": limits})


# One patch, so the overwritten file holds the same data and ids as before.
PATCH = dc.get_example_patch()


@pytest.fixture
def public_file(tmp_path):
    """A directory holding one public file."""
    dc.write(PATCH.update_attrs(access="public"), tmp_path / "a.h5", "DASDAE")
    return tmp_path / "a.h5"


def overwrite_as_secret(path, new_patch=False):
    """
    Replace a file with secret data under the same patch key.

    DASCore still loads the file when only its attrs and data change, and
    refuses it when it holds a different patch (`new_patch`).
    """
    patch = dc.get_example_patch() if new_patch else PATCH
    patch = patch.update_attrs(access="secret").new(data=PATCH.data * 2)
    path.unlink()
    dc.write(patch, path, "DASDAE")


@pytest.mark.parametrize("new_patch", [False, True])
def test_overwritten_file_is_not_served(public_file, new_patch):
    with TestClient(create_app(settings_for(public_file.parent))) as client:
        assert len(client.get("/v1/contents").json()) == 1
        overwrite_as_secret(public_file, new_patch)
        response = client.get("/v1/data", params={"format": "dasdae"})
        assert response.status_code == 409
        with pytest.raises(ChangedFileError):
            client.get("/v1/data")


def test_view_rechecks_rows_after_reindex(public_file):
    catalog = Catalog(settings_for(public_file.parent))
    catalog.refresh()
    view = catalog.view(ANONYMOUS)
    assert len(view.select(access="*")) == 1
    # Re-index the same index under the view, as a refresh in progress does.
    overwrite_as_secret(public_file)
    dc.spool(public_file.parent).update()
    assert len(view.select(access="*")) == 0


def test_size_limit_holds_without_steps(tmp_path):
    # Irregular coordinates give no step, so the estimate can't count samples.
    patch = dc.get_example_patch("wacky_dim_coords_patch")
    patch = patch.update_attrs(access="public")
    dc.write(patch, tmp_path / "a.h5", "DASDAE")
    params = {"distance_min": 1, "distance_max": 3, "format": "dasdae"}
    with TestClient(create_app(settings_for(tmp_path, limit=1000))) as client:
        response = client.get("/v1/data", params=params)
        assert response.status_code == 413
        # The Arrow stream ends rather than exceed the limit.
        with pytest.raises(Exception, match="limit"):
            client.get("/v1/data", params={**params, "format": "arrow"})


@pytest.mark.parametrize("range_header", ["bogus", "bytes=999999999-"])
def test_dasdae_leaves_no_temp_files(public_file, tmp_path, monkeypatch, range_header):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    with TestClient(create_app(settings_for(public_file.parent))) as client:
        response = client.get(
            "/v1/data",
            params={"format": "dasdae"},
            headers={"Range": range_header},
        )
    assert response.status_code == 200
    out = tmp_path / "out.h5"
    out.write_bytes(response.content)
    assert len(dc.spool(out)) == 1
    assert glob.glob(str(scratch / "dastor-*")) == []


def test_arrow_unchanged_file_is_served(public_file):
    with TestClient(create_app(settings_for(public_file.parent))) as client:
        body = client.get("/v1/data").content
    assert len(list(read_framed(io.BytesIO(body)))) == 1
