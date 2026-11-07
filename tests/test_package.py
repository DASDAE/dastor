from importlib.metadata import version

import dastor


def test_version_matches_metadata():
    assert dastor.__version__ == version("dastor")
