import io

import dascore as dc
import numpy as np
import pytest

from dastor.wire import decode_patch, encode_patch, frame, read_framed

EXAMPLES = [
    "random_das",
    "random_patch_with_lat_lon",
    "random_patch_with_xyz",
    "wacky_dim_coords_patch",
    "nd_patch",
    "sin_wav",
]


@pytest.mark.parametrize("name", EXAMPLES)
def test_round_trip(name):
    patch = dc.get_example_patch(name)
    assert decode_patch(encode_patch(patch)) == patch


def test_round_trip_keeps_nan_and_identity():
    # NaN makes patch equality false even for the same patch, so compare parts.
    patch = dc.get_example_patch("patch_with_null")
    out = decode_patch(encode_patch(patch))
    assert np.array_equal(out.data, patch.data, equal_nan=True)
    assert out.coords == patch.coords
    assert out.attrs.data_id == patch.attrs.data_id
    assert out.attrs.origin_id == patch.attrs.origin_id


def test_framed_patches():
    patches = list(dc.get_example_spool())
    body = b"".join(bytes(x) for p in patches for x in frame(encode_patch(p)))
    assert list(read_framed(io.BytesIO(body))) == patches


@pytest.mark.parametrize("cut", [4, 10], ids=["header", "body"])
def test_truncated_frame_raises(cut):
    body = b"".join(bytes(x) for x in frame(encode_patch(dc.get_example_patch())))
    body = body[:cut] if cut < 8 else body[:-cut]
    with pytest.raises(ValueError, match="truncated"):
        list(read_framed(io.BytesIO(body)))


def test_complex_data_refused():
    patch = dc.get_example_patch()
    with pytest.raises(ValueError, match="complex"):
        encode_patch(patch.new(data=patch.data.astype(np.complex64)))
