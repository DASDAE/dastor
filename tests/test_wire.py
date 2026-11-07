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
    assert out.attrs == patch.attrs


def test_decode_does_not_copy_data():
    out = decode_patch(encode_patch(dc.get_example_patch()))
    assert not out.data.flags.owndata


def test_framed_patches():
    patches = list(dc.get_example_spool())
    body = b"".join(bytes(x) for p in patches for x in frame(encode_patch(p)))
    assert list(read_framed(io.BytesIO(body))) == patches


def test_truncated_frame_raises():
    body = b"".join(bytes(x) for x in frame(encode_patch(dc.get_example_patch())))
    with pytest.raises(ValueError, match="truncated"):
        list(read_framed(io.BytesIO(body[:-10])))


def test_object_arrays_refused():
    patch = dc.get_example_patch()
    patch = patch.new(data=patch.data.astype(object))
    with pytest.raises(TypeError, match="object"):
        encode_patch(patch)


def test_one_sample_coordinate_keeps_step():
    patch = dc.get_example_patch().select(distance=(50, 50))
    out = decode_patch(encode_patch(patch))
    assert out == patch
    assert out.get_coord("distance").step == patch.get_coord("distance").step


def test_attrs_keep_types():
    extra = {
        "when": np.datetime64("2020-01-01T00:00:00.123456789"),
        "span": np.timedelta64(5, "ms"),
        "gain": float("nan"),
        "big": float("inf"),
        "z": 1 + 2j,
    }
    patch = dc.get_example_patch().update_attrs(**extra)
    attrs = decode_patch(encode_patch(patch)).attrs.model_dump()
    assert attrs["when"] == extra["when"]
    assert attrs["span"] == extra["span"]
    assert np.isnan(attrs["gain"])
    assert attrs["big"] == float("inf")
    assert attrs["z"] == extra["z"]


def test_structured_arrays_refused():
    patch = dc.get_example_patch()
    dtype = [("real", "<f4"), ("imag", "<f4")]
    with pytest.raises(TypeError, match="dtype"):
        encode_patch(patch.new(data=np.zeros(patch.shape, dtype=dtype)))
