"""
The Arrow wire format for patches.

One patch is one Arrow IPC stream holding a single record batch. Each column
is a one-row `large_binary` holding a C-ordered array's raw bytes: `data`
for the patch data and `coord:<name>` for each coordinate. The schema's
`dastor.patch` metadata is JSON giving the dims, dtypes, shapes, units and
attributes needed to rebuild the patch. Arrays are passed as their buffers,
so encoding copies them once and decoding doesn't copy them at all.

A response with several patches frames them: each stream is preceded by its
length as an unsigned 64-bit little-endian integer. Any Arrow library can
read the streams.
"""

from __future__ import annotations

import datetime
import json
import math
import struct
from collections.abc import Iterator
from typing import Any, BinaryIO

import dascore as dc
import numpy as np
import pyarrow as pa
from dascore.utils.identity import operation_context

PATCH_MEDIA_TYPE = "application/vnd.apache.arrow.stream"
FRAMED_MEDIA_TYPE = "application/vnd.dastor.patches+arrow"
FORMAT_VERSION = 1
_META_KEY = b"dastor.patch"
_LENGTH = struct.Struct("<Q")


def to_json(value: Any) -> Any:
    """
    Return a value as JSON-ready data that `from_json` turns back into it.

    Plain JSON can't hold datetimes, durations, complex numbers or
    non-finite floats, so those become one-key objects such as
    `{"$datetime64": "2020-01-03T00:00:00.000000000"}`.
    """
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_json(x) for x in value]
    if isinstance(value, np.datetime64 | datetime.datetime):
        return {"$datetime64": str(np.datetime64(value))}
    if isinstance(value, np.timedelta64 | datetime.timedelta):
        value = np.timedelta64(value)
        unit, _ = np.datetime_data(value.dtype)
        return {"$timedelta64": [int(value.astype(np.int64)), unit]}
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if isinstance(value, int | np.integer):
        return int(value)
    if isinstance(value, float | np.floating):
        value = float(value)
        return value if math.isfinite(value) else {"$float": str(value)}
    if isinstance(value, complex | np.complexfloating):
        return {"$complex": [to_json(value.real), to_json(value.imag)]}
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"cannot encode {type(value).__name__} value {value!r}")


def from_json(value: Any) -> Any:
    """Return the value `to_json` encoded."""
    if isinstance(value, list):
        return [from_json(x) for x in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        ((key, item),) = value.items()
        if key == "$datetime64":
            return np.datetime64(item)
        if key == "$timedelta64":
            return np.timedelta64(item[0], item[1])
        if key == "$float":
            return float(item)
        if key == "$complex":
            return complex(from_json(item[0]), from_json(item[1]))
    return {k: from_json(v) for k, v in value.items()}


def _array_column(array: np.ndarray) -> tuple[pa.Array, dict]:
    """Return a one-row binary column over an array's bytes and its description."""
    array = np.ascontiguousarray(array)
    # The dtype travels as `dtype.str`, which can't describe these.
    if array.dtype.hasobject or array.dtype.names is not None:
        raise TypeError(f"cannot encode an array of dtype {array.dtype}")
    buffer = pa.py_buffer(array.reshape(-1).view(np.uint8))
    offsets = pa.py_buffer(np.array([0, buffer.size], dtype=np.int64))
    column = pa.Array.from_buffers(pa.large_binary(), 1, [None, offsets, buffer])
    return column, {"dtype": array.dtype.str, "shape": list(array.shape)}


def _column_array(column: pa.Array, info: dict) -> np.ndarray:
    """Return the array a binary column holds, without copying it."""
    buffer = column.buffers()[2]
    dtype = np.dtype(info["dtype"])
    return np.frombuffer(buffer, dtype=dtype).reshape(info["shape"])


def encode_patch(patch: dc.Patch) -> pa.Buffer:
    """Return one patch as an Arrow IPC stream."""
    data, data_info = _array_column(patch.data)
    columns, fields = [data], [pa.field("data", pa.large_binary())]
    coords = []
    for name, coord in patch.coords.coord_map.items():
        column, info = _array_column(coord.values)
        units = None if coord.units is None else str(coord.units)
        dims = list(patch.coords.dim_map[name])
        step = to_json(coord.step)
        coords.append(
            {"name": name, "dims": dims, "units": units, "step": step, **info}
        )
        columns.append(column)
        fields.append(pa.field(f"coord:{name}", pa.large_binary()))
    meta = {
        "version": FORMAT_VERSION,
        "dims": list(patch.dims),
        "data": data_info,
        "coords": coords,
        "attrs": to_json(patch.attrs.model_dump(exclude={"object_type"})),
    }
    schema = pa.schema(fields, metadata={_META_KEY: json.dumps(meta)})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, schema) as writer:
        writer.write_batch(pa.record_batch(columns, schema=schema))
    return sink.getvalue()


def decode_patch(source) -> dc.Patch:
    """Return the patch in an Arrow IPC stream (bytes, a buffer, or a file)."""
    reader = pa.ipc.open_stream(source)
    meta = json.loads(reader.schema.metadata[_META_KEY])
    if meta["version"] != FORMAT_VERSION:
        msg = f"unsupported dastor wire format version {meta['version']}"
        raise ValueError(msg)
    batch = reader.read_next_batch()
    coords = {}
    for info in meta["coords"]:
        values = _column_array(batch.column(f"coord:{info['name']}"), info)
        # The step is stated, not inferred: a one-sample coordinate has one
        # that its values can't show.
        step = from_json(info["step"])
        coord = dc.get_coord(data=values, step=step, units=info["units"])
        coords[info["name"]] = (tuple(info["dims"]), coord)
    data = _column_array(batch.column("data"), meta["data"])
    # Decoding is a transfer, not a new array: as DASCore's readers do, build
    # inside an operation so the patch keeps the data_id it was sent with.
    with operation_context():
        return dc.Patch(
            data=data, coords=coords, dims=meta["dims"], attrs=from_json(meta["attrs"])
        )


def frame(stream: pa.Buffer) -> Iterator[bytes | memoryview]:
    """Yield a length prefix and then one encoded patch, for a framed response."""
    yield _LENGTH.pack(stream.size)
    yield memoryview(stream)


def read_framed(source: BinaryIO) -> Iterator[dc.Patch]:
    """Yield the patches of a framed response read from a binary file."""
    while header := source.read(_LENGTH.size):
        if len(header) != _LENGTH.size:
            raise ValueError("truncated frame header")
        (size,) = _LENGTH.unpack(header)
        body = source.read(size)
        if len(body) != size:
            raise ValueError("truncated frame")
        yield decode_patch(pa.py_buffer(body))
