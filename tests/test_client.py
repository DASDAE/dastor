import pickle
import socket
import threading
import time
import urllib.error

import dascore as dc
import numpy as np
import pytest
import uvicorn
from conftest import ALICE_KEY

from dastor import Client
from dastor.app import create_app
from dastor.client import _bound_params


@pytest.fixture(scope="module")
def url(settings):
    """The test spool served over real HTTP on a free local port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(create_app(settings), port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join()


def test_spool_matches_server_contents(url):
    spool = Client(url).spool()
    assert sorted(spool.get_contents()["tag"]) == ["public", "public"]
    everything = Client(url, key=ALICE_KEY).spool().get_contents()
    assert sorted(everything["tag"]) == ["public"] * 3 + ["secret"] * 2


def test_patches_match_local(url, data_dir):
    local = dc.spool(data_dir / "file_0.h5")[0]
    spool = Client(url).spool().select(data_id=local.attrs.data_id)
    assert list(spool) == [local]


def test_selection_trims_locally(url, data_dir):
    local = dc.spool(data_dir / "file_0.h5")[0]
    t1 = local.get_coord("time").min() + dc.to_timedelta64(2)
    window = {"time": (t1, t1 + dc.to_timedelta64(3))}
    (remote,) = Client(url).spool().select(origin_id=local.attrs.origin_id, **window)
    assert remote == local.select(**window)


def test_chunk_matches_local(url, data_dir):
    local = dc.spool([dc.spool(data_dir / f"file_{x}.h5")[0] for x in (0, 1)])
    remote = Client(url).spool().chunk(time=2)
    assert len(remote) > 1
    assert list(remote) == list(local.chunk(time=2))


def test_spool_pickles(url):
    spool = Client(url).spool()
    assert pickle.loads(pickle.dumps(spool))[0] == spool[0]


def test_hidden_patch_is_not_found(url):
    secret = Client(url, key=ALICE_KEY).spool().select(tag="secret")
    data_id = secret.get_contents()["data_id"].iloc[0]
    with pytest.raises(urllib.error.HTTPError, match="404"):
        Client(url).get_patch(data_id)


def test_get_patch_trims_on_server(url, data_dir):
    local = dc.spool(data_dir / "file_0.h5")[0]
    t1 = local.get_coord("time").min() + dc.to_timedelta64(2)
    bounds = {"time": (t1, None), "distance": (10, 20)}
    got = Client(url).get_patch(local.attrs.data_id, **bounds)
    assert got == local.select(**bounds)


def test_selection_downloads_only_its_window(url, data_dir, monkeypatch):
    sizes = []
    get = Client._get

    def counting(self, path):
        body = get(self, path)
        sizes.append((path, len(body)))
        return body

    monkeypatch.setattr(Client, "_get", counting)
    local = dc.spool(data_dir / "file_0.h5")[0]
    spool = Client(url).spool().select(data_id=local.attrs.data_id)
    whole = spool[0]
    part = spool.select(distance=(0, 9))[0]
    assert part == whole.select(distance=(0, 9))
    (whole_size, part_size) = [n for path, n in sizes if "/v1/patch/" in path]
    assert part_size < whole_size / 10


@pytest.mark.parametrize(
    ("bounds", "params"),
    [
        ({"distance": (10, None)}, [("distance_min", "10")]),
        ({"distance": [0.5, np.inf]}, [("distance_min", "0.5")]),
        ({"time": (np.datetime64("NaT"), True)}, []),
        ({"time": "x", "other": (1, 2, 3)}, []),
    ],
)
def test_bound_params(bounds, params):
    assert _bound_params(bounds) == params
