"""Shared fixtures: a small served directory with public and secret files."""

import warnings

import dascore as dc
import pytest
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
            "spools": {
                "das": {
                    "path": str(data_dir),
                    "rules": [
                        {"column": "tag", "pattern": "^public$", "allow": ["*"]},
                        {"column": "tag", "pattern": ".*", "allow": ["group:lab"]},
                    ],
                },
                "lab_only": {
                    "path": str(data_dir),
                    "rules": [
                        {"column": "tag", "pattern": ".*", "allow": ["group:lab"]}
                    ],
                },
            },
            "users": [
                {"name": "alice", "key_sha256": hash_key(ALICE_KEY), "groups": ["lab"]},
                {"name": "bob", "key_sha256": hash_key(BOB_KEY)},
            ],
        }
    )


@pytest.fixture(scope="session")
def client(settings):
    """A test client with the spools opened."""
    with TestClient(create_app(settings)) as out:
        yield out


def auth(key):
    """Return the headers presenting an API key."""
    return {"Authorization": f"Bearer {key}"}
