"""Shared fixtures: a small served directory with public and secret files."""

import contextlib
import socket
import threading
import time
import warnings

import dascore as dc
import numpy as np
import pytest
import uvicorn
from fastapi.testclient import TestClient

from dastor.app import create_app
from dastor.auth import hash_key
from dastor.config import Settings

ALICE_KEY = "alice-key"  # in group "lab": sees everything
BOB_KEY = "bob-key"  # authenticated, no groups: sees public files


@pytest.fixture(autouse=True)
def _quiet_dascore():
    """Silence DASCore's informational warnings in test output."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        yield


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    """
    A directory of one-patch files tagged public, public and secret, plus
    one file holding a public and a secret patch.
    """
    path = tmp_path_factory.mktemp("data")
    patches = list(dc.get_example_spool("random_das"))
    tags = ["public", "public", "secret"]
    for number, (patch, tag) in enumerate(zip(patches, tags, strict=True)):
        dc.write(patch.update_attrs(tag=tag), path / f"file_{number}.h5", "DASDAE")
    mixed = [
        patches[0].update_attrs(tag="public", station="mixed"),
        patches[1].update_attrs(tag="secret", station="mixed"),
    ]
    dc.write(dc.spool(mixed), path / "mixed.h5", "DASDAE")
    return path


@pytest.fixture(scope="session")
def settings(data_dir):
    """Settings granting public files to everyone and all files to group lab."""
    return Settings.model_validate(
        {
            "spool": {
                "path": str(data_dir),
                "rules": [
                    {"column": "tag", "pattern": "^public$", "allow": ["*"]},
                    {"column": "tag", "pattern": ".*", "allow": ["group:lab"]},
                ],
            },
            "users": [
                {"name": "alice", "key_sha256": hash_key(ALICE_KEY), "groups": ["lab"]},
                {"name": "bob", "key_sha256": hash_key(BOB_KEY)},
            ],
        }
    )


@pytest.fixture(scope="session")
def client(settings):
    """A test client with the spool opened."""
    with TestClient(create_app(settings)) as out:
        yield out


VIEW_KEY = "DEMO.CABLE..A1"  # the acquisition the viewer's products share


def _viewer_patches():
    """
    Yield a recording as viewer products: raw data in two blocks with a
    gap between them, and its maximum over each 0.1 s and four channels.
    """
    rng = np.random.default_rng(0)
    start = np.datetime64("2024-05-01T00:00:00")
    distance = np.arange(64) * 2.0
    for offset in (0, 180):  # seconds; the second block starts after a gap
        time = start + np.timedelta64(offset, "s")
        time = time + np.arange(6000) * np.timedelta64(10, "ms")
        data = rng.normal(0, 1, (64, 6000)).astype("float32")
        data[np.arange(6000) * 64 // 6000, np.arange(6000)] += 8  # a moving source
        coarse = np.abs(data).reshape(16, 4, 600, 10).max((1, 3))
        for tag, values, dist, times in (
            ("raw", data, distance, time),
            ("max_100ms", coarse, distance[::4], time[::10]),
        ):
            yield dc.Patch(
                data=values,
                coords={"distance": dist, "time": times},
                dims=("distance", "time"),
                attrs={"tag": tag, "acquisition_key": VIEW_KEY},
            ).update_coords(distance=dc.get_coord(data=dist, units="m"))


@pytest.fixture(scope="session")
def viewer_dir(tmp_path_factory):
    """A directory holding one recording at two resolutions."""
    path = tmp_path_factory.mktemp("viewer_data")
    for number, patch in enumerate(_viewer_patches()):
        dc.write(patch, path / f"{patch.attrs.tag}_{number}.h5", "DASDAE")
    return path


@pytest.fixture
def viewer_settings(viewer_dir, tmp_path):
    """Settings as `dastor browse` makes them, with an empty annotation store."""
    from dastor.cli import browse_settings  # noqa: PLC0415

    config = browse_settings(str(viewer_dir), str(tmp_path / "annotations"))
    config["annotations"]["anonymous_owner"] = "me"
    return Settings.model_validate(config)


@pytest.fixture
def viewer(viewer_settings):
    """A test client on the viewer's directory."""
    with TestClient(create_app(viewer_settings)) as out:
        yield out


def free_port() -> int:
    """Return a port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def live(app):
    """Serve an app on a real port for a while, yielding its address."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    end = time.time() + 60
    while not server.started:
        assert thread.is_alive() and time.time() < end, "the server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def auth(key):
    """Return the headers presenting an API key."""
    return {"Authorization": f"Bearer {key}"}
