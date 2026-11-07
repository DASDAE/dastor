# DASCore Arrow patch format, version 1.0

DASCore's `ARROW` format holds one patch in one [Apache Arrow IPC](https://arrow.apache.org/docs/format/Columnar.html#serialization-and-interprocess-communication-ipc) stream (`.arrows`, `application/vnd.apache.arrow.stream`) or file (`.arrow`). dastor sends patches in it. The reference implementation is `dascore/io/arrow`; this page describes what it writes and reads.

```python
import dascore as dc

dc.write(patch, "patch.arrow", "ARROW")  # a file
dc.write(patch, "patch.arrows", "ARROW", stream=True)  # a stream
dc.write(patch, "patch.arrow", "ARROW", compression="zstd")  # or "lz4"
patch = dc.spool("patch.arrow")[0]
```

## Layout

```
IPC stream                            (dims [time, distance], shape [1000000, 300])
├── schema
│   ├── field "data": float (not nullable)
│   └── metadata {"dascore:patch": "{...dims, shape, dtype, coords, attrs...}"}
├── record batch: whole time slices   (at least 65,536 values, about 16 MiB)
├── ...
└── end-of-stream marker
```

- **One column.** The schema has one non-nullable field, `data`, holding the patch's values flattened in C order. Any Arrow reader sees an ordinary numeric column; `shape` in the metadata turns it back into the array.
- **Batches.** Every batch but the last holds whole slices of the first dim, at least 65,536 values (or the whole patch) and about 16 MiB. A caller who wants a patch streamed along another dim transposes it first.
- **Metadata first.** Dims, shape, coordinates and attrs are a JSON document under the schema metadata key `dascore:patch`, so a reader knows the patch's size before any data arrives.
- **Compression** (`zstd` or `lz4`) is optional and off by default.

### Data types

| Patch dtype | Arrow type |
|---|---|
| `bool` | `bool` |
| `int8` … `uint64` | the integer of the same width and signedness |
| `float16`, `float32`, `float64` | `halffloat`, `float`, `double` |
| `datetime64` in `s`, `ms`, `us`, `ns` | `timestamp` of that unit, no time zone |
| `timedelta64` in `s`, `ms`, `us`, `ns` | `duration` of that unit |

`NaT` is written as the int64 minimum, a valid value, never an Arrow null. Writers refuse complex, string, object and masked data, extended-precision floats, and time units Arrow lacks. Big-endian data is written little-endian.

## The `dascore:patch` document

```json
{
  "format": "dascore.patch",
  "version": "1.0",
  "dims": ["distance", "time"],
  "shape": [300, 2000],
  "dtype": "<f8",
  "coords": {
    "time": {
      "dims": ["time"], "dtype": "<M8[ns]", "shape": [2000], "units": "s",
      "step": {"$scalar": ["<m8[ns]", 4000000]},
      "runs": [{"origin": 1505692800000000000, "step_num": 4000000, "step_den": 1,
                "count": 2000, "k0": 0, "phase": 0, "parent_count": null, "stride": 1}]
    },
    "station": {
      "dims": ["distance"], "dtype": "<U3", "shape": [300], "units": null, "step": null,
      "values": {"$array": ["<U3", ["A01", "A01", "..."]]}
    }
  },
  "attrs_class": null,
  "attrs": {
    "tag": "random",
    "data_units": {"$quantity": "m / s"},
    "data_id": "fdc7f4ada12f4844aac075182ee87e98",
    "origin_id": "fdc7f4ada12f4844aac075182ee87e98",
    "history": []
  }
}
```

- `format` is `"dascore.patch"`; `version` is `"<major>.<minor>"`, and readers refuse majors they don't know.
- `dims` are distinct non-empty strings, at least one; `shape` has one non-negative integer per dim.
- `dtype` is the data's numpy `dtype.str` and must agree with the column's Arrow type.
- `coords` maps each coordinate name to DASCore's own dump of it (`coord.model_dump()`), plus its `dims`. Numeric coordinates are a list of `runs` (exact grids, float grids with their parent slice, stored labels, blanks), rebuilt with `get_coord(runs=...)`; string coordinates carry their `values`. Stored labels are cut to the window the patch uses, so a slice doesn't send its parent array. Content ids aren't sent.
- `attrs` is `PatchAttrs.model_dump()`, every attribute kept. `attrs_class` is the registered model tag of an attrs subclass (as DASDAE records it), or `null`; readers resolve it through DASCore's model registry, which may import installed plugins but never a module named by the file.

### Values

Coordinates and attrs share one encoding. JSON strings, booleans, `null`, numbers (integers of any size, exact in Python), lists and string-keyed objects carry themselves; tuples become lists. Everything else is an object with one `$` key:

| Value | Encoding |
|---|---|
| NaN, infinity | `{"$float": "nan"}`, `{"$float": "inf"}`, `{"$float": "-inf"}` |
| numpy scalar | `{"$scalar": ["<f4", 0.5]}` |
| numpy array | `{"$array": ["<i8", [1, 2, 3]]}` (nested lists for several dims) |
| pint quantity | `{"$quantity": "5 m / s"}` |
| pint unit | `{"$unit": "m / s"}` |

Numpy values carry their full `dtype.str`, so `float32` stays `float32` and unit multiples such as `M8[10ms]` round-trip. Times and durations are int64 counts of their dtype's unit, `NaT` the int64 minimum. Python and pandas times are written as numpy times. Writers refuse time-zone-aware times, bytes, dicts with non-string keys, and anything else without an encoding.

## Identity and exactness

A patch read back has the same data (bit-for-bit, NaN and `NaT` included), dims, coordinates (labels, dtype, units, declared step and runs), attrs (values and types, after the normalisations above), attrs class, `data_id` and `origin_id`. Nothing is snapped unless the read asks for `snap=True`. As DASDAE does, the reader carries the stored origin id so it isn't derived again from the file's path.

## Reading

Malformed input raises `InvalidFiberFileError`: a wrong format, version, dims, shape or data field, unsupported dtypes, coordinates that can't be built or don't fit the shape, nulls in the data, and too few or too many values. A stream whose first message claims more than 256 MiB of metadata isn't read. Decoding runs no code from the data.

A client reading untrusted network streams may want tighter limits than these, such as checking each batch header before Arrow reads its body. That belongs in the client, not the format.

## Use in dastor

`GET /v1/data?format=arrow` streams the matching patches, each an `ARROW` stream preceded by its length as an unsigned 64-bit little-endian integer (`dastor.wire.read_framed` reads them back).

## Why Arrow

- **Not protobuf or JSON.** Protobuf is for small messages (2 GB limit) and parses whole messages; JSON needs base64, which is a third larger and can't stream. A custom byte layout is what this avoids.
- **A flat column, not Arrow's tensor type.** `fixed_shape_tensor` limits a row to 2^31−1 values and few Arrow libraries read it. A flat column has no such limit and is still a zero-copy view.
- **JSON metadata.** Coordinates and attrs are small next to the data and must arrive first. Writing them as DASCore's own coordinate dump keeps them exact without a second coordinate model.
