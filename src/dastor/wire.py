"""
The Arrow wire format for patches.

Each patch is one Arrow IPC stream in DASCore's ``ARROW`` format: the values
are a single ``data`` column and the dims, coordinates and attrs are JSON in
the schema metadata (see ``docs/arrow-patch-format.md``). DASCore reads such a
stream back exactly, ids included, and any Arrow library can read its data.

A response with several patches frames them: each stream is preceded by its
length as an unsigned 64-bit little-endian integer.
"""

from __future__ import annotations

import io
import struct
from collections.abc import Iterator
from typing import BinaryIO

import dascore as dc
from dascore.io.arrow import ArrowV1

PATCH_MEDIA_TYPE = "application/vnd.apache.arrow.stream"
FRAMED_MEDIA_TYPE = "application/vnd.dastor.patches+arrow"
_ARROW = ArrowV1()
_LENGTH = struct.Struct("<Q")


def encode_patch(patch: dc.Patch) -> memoryview:
    """Return one patch as an Arrow IPC stream."""
    sink = io.BytesIO()
    _ARROW.write(patch, sink, stream=True)
    return sink.getbuffer()


def decode_patch(source: bytes | memoryview) -> dc.Patch:
    """Return the patch in an Arrow IPC stream."""
    return _ARROW.read(io.BytesIO(source))[0]


def frame(stream: memoryview) -> Iterator[bytes | memoryview]:
    """Yield a length prefix and then one encoded patch, for a framed response."""
    yield _LENGTH.pack(stream.nbytes)
    yield stream


def read_framed(source: BinaryIO) -> Iterator[dc.Patch]:
    """Yield the patches of a framed response read from a binary file."""
    while header := source.read(_LENGTH.size):
        if len(header) != _LENGTH.size:
            raise ValueError("truncated frame header")
        (size,) = _LENGTH.unpack(header)
        body = source.read(size)
        if len(body) != size:
            raise ValueError("truncated frame")
        yield decode_patch(body)
