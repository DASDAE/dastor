# DASCore Arrow patch format, version 1.0 (draft 3)

**Status:** draft for review. It is meant to become a DASCore format (`ARROW`), with dastor using it to send patches over HTTP. Nothing here is implemented yet. Draft 3 follows two rounds of review (Arrow interoperability, DASCore fidelity, a general review and an edge-case conformance review). The coordinate and attribute documents are to come from a public DASCore API ([DASCore #1485](https://github.com/DASDAE/dascore/issues/1485)); this document gives them normatively so other languages can implement them.

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## Purpose

A way to move one DASCore patch through a byte stream or a file:

- **fast:** the array crosses without copies where the layout allows it (see [Zero-copy](#zero-copy)), and can be streamed in pieces;
- **exact:** a patch read back equals the patch written, as defined under [Exactness](#exactness);
- **standard:** every byte layout is Apache Arrow's; this document defines only a column and a metadata convention;
- **safe to read from an untrusted source:** decoding runs no code from the data, and a reader can bound the memory and work a stream makes it use (see [Reading safely](#reading-safely)).

Complex-valued patches are out of scope: Arrow has no complex type, and writers MUST refuse them.

## Overview

A patch is one Arrow IPC stream (or one Arrow IPC file, for storage). The schema has a single column, `data`, holding the patch's values flattened in C (row-major) order. Everything else (dims, shape, coordinates, attributes) is a JSON document in the schema's custom metadata under the key `dascore:patch`, so it arrives before any data.

```
IPC stream                            (dims [time, distance], shape [1000000, 300])
├── schema
│   ├── field "data": float (not nullable)
│   └── metadata {"dascore:patch": "{...dims, shape, dtype, coords, attrs...}"}
├── record batch: 65,700 values       (time 0..218, all 300 channels)
├── record batch: 65,700 values       (time 219..437)
├── ...
├── record batch: the rest            (ending at time 999,999)
└── end-of-stream marker
```

A reader that doesn't know this document sees an ordinary numeric column; it needs `shape` from the metadata to see the array.

## Containers

- **Streams.** A patch in transit is one Arrow IPC stream ([IPC streaming format](https://arrow.apache.org/docs/format/Columnar.html#ipc-streaming-format)), media type `application/vnd.apache.arrow.stream`, extension `.arrows` when saved. Writers MUST end it with the end-of-stream marker, and nothing may follow the marker in the enclosing message (HTTP response body or file).
- **Files.** A stored patch is one Arrow IPC file ([IPC file format](https://arrow.apache.org/docs/format/Columnar.html#ipc-file-format)), media type `application/vnd.apache.arrow.file`, extension `.arrow`. The footer's list of record batches MUST name every batch of the embedded stream exactly once, in the order they are stored, with their actual offsets and lengths (Arrow only recommends this). The footer repeats the schema, so the metadata is stored twice, and the two copies MUST be identical.
- **One patch per stream or file.** Several patches travel as several streams (in dastor, several HTTP responses); several stored patches are several files, or a DASDAE file.
- Writers MUST write IPC metadata version V5 and little-endian schemas, and MUST NOT put field-level metadata (including `ARROW:extension:*` keys) on `data`.

## The `data` column

The schema MUST have exactly one field, named `data`, not nullable, of one of the value types below. Its values MUST contain no nulls.

The column holds the patch's array flattened in C order: the value at index `(i0, i1, ..., in)` is at position `((i0 * s1 + i1) * s2 + ...) + in`, where `s0..sn` is `shape`. Writers keep the patch's own dim order; a caller who wants a patch streamed along another dim (usually time, for long records) transposes it before writing.

**Batches.** The batch lengths, in order, add up to `prod(shape)`. A patch with no values has no batches. Every batch MUST hold at least one value, and every batch but the last MUST hold at least `min(65536, prod(shape))` values, which bounds the number of batches (and so their overhead) by the size of the data. Writers SHOULD end batches on slices of the first dim (multiples of `prod(shape[1:])`), so each batch is a sub-patch along it, and SHOULD keep batches at most 64 MiB.

Patches have at least one dim and at most 32. Patches with no dims aren't supported.

### Value types

| Patch dtype | Arrow type |
|---|---|
| `bool` | `bool` (bit-packed in Arrow, so it is always copied) |
| `int8` ... `int64`, `uint8` ... `uint64` | the integer of the same width and signedness |
| `float16`, `float32`, `float64` | `halffloat`, `float`, `double` |
| `datetime64` in `s`, `ms`, `us` or `ns` | `timestamp` of the same unit, with no time zone |
| `timedelta64` in `s`, `ms`, `us` or `ns` | `duration` of the same unit |

Writers MUST refuse other dtypes, including complex, string, object, structured, extended-precision floats, time units Arrow lacks (such as `D`), and masked arrays. Data in non-native byte order is written little-endian, and bool values are written as true or false whatever byte numpy held them in; [Exactness](#exactness) lists both normalisations.

`NaT` in time data is the int64 minimum, the value numpy uses for it, written as a valid value and never as an Arrow null. Writers MUST build time columns from the int64 view of the array, since some converters (including pyarrow's from numpy) turn `NaT` into nulls. Arrow readers that don't know this document see it as a timestamp about 292 billion years before 1970.

### Compression

Writers MAY compress batch bodies with Arrow IPC body compression (`ZSTD` preferred, or `LZ4_FRAME`). DASCore readers MUST support both; general Arrow readers may not (in arrow-js, arrow-rs and Java it needs extra setup), so writers SHOULD NOT compress unless the reader asked for it. Compressed batches can't be read without a copy.

## The `dascore:patch` metadata

The schema's custom metadata MUST hold the key `dascore:patch` exactly once. Its value is a UTF-8 JSON object:

```json
{
  "format": "dascore.patch",
  "version": "1.0",
  "dims": ["time", "distance"],
  "shape": [4095, 3],
  "dtype": "<f4",
  "coords": {
    "time": {
      "dims": ["time"], "shape": [4095], "dtype": "<M8[ns]", "units": "s",
      "step": ["976562", "ns"],
      "runs": [{"grid": {"origin": "1577836800000976562", "step_num": "1953125",
                         "step_den": "2", "phase": "1", "count": 4095}}]
    },
    "distance": {
      "dims": ["distance"], "shape": [3], "dtype": "<i8", "units": "m",
      "step": "1",
      "runs": [{"grid": {"origin": "0", "step_num": "1", "step_den": "1", "phase": "0", "count": 3}}]
    },
    "station": {
      "dims": ["distance"], "shape": [3], "dtype": "<U3", "units": null, "step": null,
      "runs": [{"values": ["A01", "A01", "A02"]}]
    }
  },
  "attrs_class": null,
  "attrs": {
    "tag": "random",
    "data_units": {"$quantity": [1, "m / s"]},
    "data_id": "fdc7f4ada12f4844aac075182ee87e98",
    "origin_id": "fdc7f4ada12f4844aac075182ee87e98",
    "history": []
  }
}
```

- `format` MUST be `"dascore.patch"`. `version` is `"<major>.<minor>"`, matching `^[1-9][0-9]*\.(0|[1-9][0-9]*)$` (see [Versioning](#versioning)).
- `dims` lists the patch's dims in array order: non-empty strings, no repeats, between 1 and 32 of them.
- `shape` lists the size of each dim: non-negative integers, as many as `dims`.
- `dtype` is the numpy `dtype.str` of the data as written (little-endian, so `<f4`, `|u1`, `|b1`, `<M8[ns]`), and MUST agree with the column's Arrow type.
- `coords`, `attrs_class` and `attrs` are described below.

### JSON rules

- The document MUST be valid UTF-8 JSON with no duplicate keys in any object (compared after decoding escapes). Strings, including keys, MUST be sequences of Unicode scalar values (no lone surrogates).
- Integers that can exceed 2^53 (the largest integer many JSON parsers keep exactly) are decimal strings matching `^-?(0|[1-9][0-9]*)$`: grid `origin`, `step_num`, `step_den` and `phase`, integer and time labels and steps, and attribute integers under `$int`. Other integers (sizes, counts, `k0`, strides) are JSON numbers below 2^53, and readers MUST parse them without going through binary64.
- Floats are written in the shortest form that reads back to the same double, and always with a decimal point or exponent (`1.0`, not `1`), so they are told apart from integers; `float16` and `float32` values are widened exactly to doubles. NaN and infinities are `{"$float": "nan"}`, `{"$float": "inf"}` and `{"$float": "-inf"}`. A float that overflows a double (`1e400`) is an error.

### Coordinates

`coords` maps each coordinate name to a document mirroring DASCore's coordinate model. Every dim MUST have a coordinate of the same name whose `dims` are exactly `[dim]`. Other coordinates MAY be present.

- `dims`: the patch dims the coordinate rides, in order and without repeats; `[]` for a coordinate on no dim.
- `shape`: the coordinate's shape. If `dims` is not empty, `shape` has one entry per dim, equal to that dim's size. If `dims` is empty, `shape` is `[]` (a scalar) or one entry (several values on no dim).
- `dtype`: the label dtype as numpy's `dtype.str`, little-endian. Allowed: `|b1`, integers, `<f2`/`<f4`/`<f8`, `datetime64` and `timedelta64` in the fixed-length units `D`, `h`, `m`, `s`, `ms`, `us`, `ns`, and unicode strings `<U<n>` with `1 <= n <= ` the reader's string-width limit. Writers MUST refuse other dtypes, including object (so time-zone-aware times), complex and extended-precision floats, and convert big-endian labels to little-endian.
- `units`: a units string as DASCore writes it (`dascore.units.get_quantity_str`), or `null`. String and bool coordinates have no units.
- `step`: the declared sampling step, or `null`. It is kept apart from the runs because a coordinate with stored labels can still declare a step (jittered or gapped sampling), and inferring it again gives a different answer. A time step is `[count, unit]` with a decimal-string count and its own fixed-length unit (DASCore can declare a nanosecond step on a microsecond coordinate); an integer step is a decimal string; a float step is a number. String and bool coordinates have `null`.
- `runs`: a non-empty list of runs whose counts add up to `prod(shape)`, read in order. Each run is an object with exactly one member, naming its kind; readers MUST reject run kinds they don't know. Writers emit the coordinate's runs as DASCore holds them, without splitting or merging; readers may get DASCore's canonical runs back.

Which runs a coordinate may have:

| Coordinate | Runs allowed |
|---|---|
| one entry in `shape`, integer or time dtype | `grid`, `values`, or a single `blank` |
| one entry in `shape`, float dtype | `float_grid`, `values`, or a single `blank` |
| one entry in `shape`, bool or string dtype | a single `values` run |
| `shape` `[]`, or more than one entry | a single `values` run, or (numeric and time dtypes) a single `blank` |

#### `grid`: an exact evenly sampled run

Fields: `origin`, `step_num`, `step_den`, `phase` (decimal strings) and `count` (non-negative). `step_den` MUST be positive and `0 <= phase < step_den`; `step_num` may be negative (a descending coordinate) or zero (a constant one). Ticks are nanoseconds for time coordinates and the integers themselves otherwise. In exact integer arithmetic, label `k` for `k` in `0 .. count-1` is

```
ticks(k) = origin + floor((phase + k * step_num) / step_den)
```

For an integer coordinate, every `ticks(k)` MUST fit the coordinate's dtype. For a time coordinate, every `ticks(k)` MUST fit int64; a value equal to the int64 minimum is `NaT`, and otherwise the label is `floor(ticks(k) / unit)`, the number of the coordinate's units, where `unit` is the unit's length in nanoseconds (so negative times floor towards earlier times). Readers evaluate only the `count` labels they need and MUST reject grids whose labels don't fit.

#### `float_grid`: a floating-point evenly sampled run

Fields: `origin`, `step`, `count`, `k0`, `stride`, `parent_count`, `origin_dtype` and `step_dtype`. A float grid can be a slice of a longer parent grid, which it keeps so its labels stay bit-for-bit those of the parent; `origin` and `step` keep their own numpy types, which affect the arithmetic and can differ from each other.

- `origin_dtype` and `step_dtype` are each `"<f2"`, `"<f4"`, `"<f8"`, or `"float"` for a Python float (which numpy treats as a "weak" float64 in promotion, [NEP 50](https://numpy.org/neps/nep-0050-scalar-promotion.html)). `origin` and `step` are numbers or `$float` tags, exact in those types.
- `count >= 0` and `stride != 0`. If `parent_count` is `null`, `k0` MUST be 0 and `stride` 1. Otherwise `parent_count >= 1` and every index `k0 + k * stride`, for `k` in `0 .. count-1`, lies in `0 .. parent_count-1`.

For time coordinates on a coarse unit, the same run kind holds a grid whose `origin` is a `[count, unit]` time and whose `step` is a `[count, unit]` duration (decimal-string counts), and `origin_dtype`/`step_dtype` are their numpy `dtype.str`. With `idx = k0 + k * stride` and `num = parent_count` if given, else `count`, label `k` is computed with numpy 2 scalar arithmetic and promotion rules (no fused multiply-add):

```
if num == 1 or the coordinate is a time coordinate:
    label = origin + idx * step                          (in the coordinate's dtype)
else:
    last    = (origin + step * num) - step
    wide    = result_type(origin, last, 0.0)             (0.0 a Python float)
    delta   = (last - origin) computed in wide
    spacing = delta / (num - 1)
    x       = idx converted to wide
    value   = (x / (num - 1)) * delta  if spacing == 0  else  x * spacing
    label   = last  if idx == num - 1  else  value + origin
    label converted to the coordinate's dtype
```

This is numpy `linspace`'s arithmetic, including its exact endpoint. Readers evaluate only the `count` labels they need, never the parent. The reference implementation is DASCore's `Grid.labels`; the format's normative test vectors (mixed origin and step types, float16 overflow and underflow, signed zeros, endpoint rounding, empty runs, reversed and strided slices) fix the arithmetic, so a change in numpy or DASCore can't change it.

#### `values`: stored labels

A list of `count` labels, in C order for a coordinate with several dims. Integer labels are decimal strings; time labels are decimal-string counts of the coordinate's own unit, with `NaT` as the int64 minimum; float labels are numbers or `$float` tags; bool and string labels are themselves. Every label MUST fit the coordinate's dtype (an integer in range, a string no longer than its width).

#### `blank`: samples with unknown labels

Fields: `start` and `stop`, each a label or `null`. Time labels here are `[count, unit]` (they can be finer than the coordinate's unit), and other labels are encoded as in `values`. A blank is a coordinate's only run and covers its whole shape. DASCore makes these for dims built without a coordinate and for empty selections.

Readers MUST rebuild coordinates from labels and these fields alone. DASCore's dumps also carry content ids for stored labels; they are never written, and a reader MUST NOT accept ids from a stream.

### Attributes

`attrs` holds the patch's attributes, as DASCore's `PatchAttrs.model_dump()` gives them, in the value encoding below. Every attribute is kept, including ones a reader doesn't know. `history` is a string or a list of strings; a list reads back as DASCore's tuple.

`attrs_class` is the registered model tag of the attrs class when it is a subclass of `PatchAttrs` (as DASDAE records it), or `null`. Readers look the tag up among the classes already registered when reading starts, and MUST NOT import modules or load plugins to find it. A reader MUST reject a patch whose class it doesn't have, since rebuilding it as plain `PatchAttrs` can drop attributes; it MAY offer an explicitly lossy mode that does so with a warning naming the class.

JSON strings, booleans, `null`, lists and string-keyed objects carry themselves; integer and float numbers follow the [JSON rules](#json-rules). Other values are objects with a single `$`-prefixed key:

| Value | Encoding |
|---|---|
| integer of magnitude 2^53 or more | `{"$int": "1578009600000000000"}` |
| NaN, infinity | `{"$float": "nan"}`, `{"$float": "inf"}`, `{"$float": "-inf"}` |
| numpy scalar | `{"$scalar": ["<f4", value]}`, for bool, integer and float dtypes only; the value is encoded as above |
| `datetime64` | `{"$datetime64": ["1578009600000000000", "ns"]}`: decimal-string count and a fixed-length unit; `NaT` is the int64 minimum |
| `timedelta64` | `{"$timedelta64": ["4000000", "ns"]}` |
| `pandas.Timestamp` | written as `datetime64[ns]` |
| `datetime.datetime` | written as `datetime64[us]` |
| pint quantity | `{"$quantity": [magnitude, "m / s"]}` (units as `get_quantity_str` writes them) |
| pint unit | `{"$unit": "m / s"}` |
| bytes | `{"$bytes": "<lowercase hex>"}` |
| object whose keys aren't all strings, or with a key starting with `$` | `{"$dict": [[key, value], ...]}`, keys being strings or integers (as above) |

- Tuples are written as lists. Time-zone-aware times, tuple keys, and anything else without an encoding MUST be refused by writers, never turned into strings.
- In a `$dict`, keys MUST be distinct after decoding (so `1` and `true` can't both appear, and `true` isn't a key type).
- Readers MUST reject unknown tags and tags with malformed payloads.
- This tag grammar is the format's own. It is not DASCore's identity encoder (`dascore.utils.identity`), which is deliberately lossy for hashing and shares some tag names with different payloads.

### Identity

`attrs` carries the patch's `data_id` and `origin_id`. A reader returning a whole patch MUST restore both verbatim, and MUST NOT change labels on a default read (for example by snapping jittered coordinates), since a changed patch would keep an id that no longer describes it. A read that selects part of a patch derives narrowed ids, as DASCore does for any selection.

In DASCore this means (informative): build the patch inside `dascore.utils.identity.operation_context()`; when reading or scanning through a FiberIO, carry the stored origin id (`STORED_ORIGIN_ID`) so it isn't derived again from the file's path; and default the format's `snap` to `False`, so an explicit `snap=True` read derives a new `data_id` ([DASCore #1484](https://github.com/DASDAE/dascore/issues/1484) is the same problem in DASDAE).

## Exactness

A patch read back is exact when it has:

- the same dims, shape and data dtype (after byte-order normalisation), and the same data: the same bytes for integer, float and time data (NaN payloads, signed zeros and `NaT` included), and the same true/false values for bool data;
- for every coordinate, the same dims, shape, dtype, units, declared step and labels, and runs that DASCore considers equal;
- the same attributes with the same values and types, after the normalisations stated under [Attributes](#attributes) (tuples to lists, `pandas.Timestamp` and `datetime.datetime` to `datetime64`), and the same attrs class;
- the same `data_id` and `origin_id`.

Coordinate content ids (each coordinate's own id, and so the patch's `strong_data_id`) need not match: DASCore computes them again on read from labels, and a slice of stored labels is sent as its values, not with its parent array. In DASCore, `Patch.__eq__` ignores ids, extra attributes and history and compares data approximately, so tests of this format MUST compare the properties above directly.

## Zero-copy

The data column is read without copying when the batch is uncompressed, the value type isn't `bool`, and the source buffer is aligned (readers SHOULD ask for type-specific alignment, e.g. pyarrow's `ensure_alignment=Alignment.DataTypeSpecific`, which copies only when needed). Then each batch is a read-only view of the received bytes, and a patch of one batch is a view of the stream. A patch of several batches is copied once into one array; writers of files meant to be memory-mapped SHOULD use a single batch. A writer copies nothing for a C-ordered array of a supported dtype other than `bool`.

## Reading safely

Arrow libraries read a whole message, and decompress its buffers, before the caller sees it, and they accept buffers larger than a batch needs. So a reader can't make a stream safe by checking a patch after Arrow has read it. A safe reader reads the stream **one message at a time** (for example with pyarrow's `MessageReader`, which reads exactly one message and never reads ahead), checks each message's header before its body is read, and only then has Arrow decode it.

The reader sets these limits (DASCore SHOULD document its defaults, for example the values in brackets): metadata bytes per message [16 MiB], data bytes [the caller's choice], total stream or file bytes, coordinate bytes [256 MiB], JSON nesting depth [64], numeric token length [64 characters], string width [1 Mi characters], attribute mapping entries [100,000].

For a **stream**, a reader MUST:

1. **Read the schema message.** Read its length prefix (`0xFFFFFFFF` and an int32 length) and reject it if longer than the metadata limit, then read it. The key `dascore:patch` MUST appear exactly once in the schema's key-value list (checked on the list itself, since converting it to a dictionary hides repeats).
2. **Check the metadata before any batch:** parse the JSON within the depth and token limits and the [JSON rules](#json-rules); check `format`, the version's syntax and major; check every structural rule in this document with overflow-checked integer arithmetic; check the field (name, type, nullability, no field metadata) against `dtype`; check that the data's size, `prod(shape)` times the item size, is within the data limit; and check that the coordinates' size, the sum of `prod(shape)` times the item size (string width included) over all coordinates, is within the coordinate limit, before computing or storing any label.
3. **Check each record batch's header before reading its body:** the message is a record batch; its body length is within what remains of the stream budget; its row count is at least 1, at most the values remaining, and obeys the batch-size rule; it has the buffers the type needs (validity and values) with offsets and lengths inside the body, and a values buffer no longer than the rows need plus padding to 64 bytes; any validity buffer reports no nulls; and, if the batch is compressed, compression was asked for, and each buffer's recorded uncompressed length (the first 8 bytes of a compressed buffer) equals what the rows need. Only then read the body and decode it.
4. **Check each decoded batch:** run Arrow's full validation (`validate(full=True)` in pyarrow), confirm it has no nulls, and confirm the actual decompressed sizes match step 3.
5. **Complete only at the end-of-stream marker:** the patch is complete when the values received equal `prod(shape)` and the reader has itself seen the end-of-stream marker (an Arrow library's iterator ending is not enough, since it ends the same way when the bytes stop), with nothing after it and the enclosing transfer finished successfully. Anything else is an error. A reader that hands out batches as they arrive MUST mark them provisional until then; a consumer that stops early has not validated the rest.
6. **Run no code from the data:** no evaluation, imports, unpickling, plugin loading or lookups other than those this document names.

For a **file**, a reader MUST check the leading and trailing `ARROW1` magic, check the footer's length against the metadata limit before reading the footer, check that the footer schema's metadata equals the embedded schema's, check that the footer's batch list matches the stored batches as required under [Containers](#containers), and then check the schema and each batch as in steps 2 to 4, reading each batch's header before its body.

## Versioning

`version` is `"<major>.<minor>"`. A reader MUST reject majors it doesn't know. Within a known major, a reader MUST ignore members it doesn't know in the top-level document, in coordinate documents, and in the payload objects of known run kinds and tags; a run's single member names its kind, so an unknown run kind is an error, as is an unknown tag. A minor version MAY add only members safe to ignore, which can't change how the patch is rebuilt. New value types, run kinds, tags, or changes of meaning need a new major. Attributes are content, not format: unknown attribute names are always kept. The arithmetic of a major version is fixed by its normative test vectors.

## Use in dastor (informative)

dastor sends one patch per HTTP response as an uncompressed Arrow IPC stream, compressing with ZSTD only when the client asks. Clients fetch many patches as many requests, in parallel over HTTP/2, which also lets them retry one patch rather than a whole download. dastor sends the schema, and so the patch's size, before reading any data, and reads and sends one batch at a time where the source file's format allows it.

## Rationale (informative)

- **Arrow rather than protobuf, JSON or a custom layout.** Protobuf is for small messages (2 GB hard limit) and parses messages whole; JSON needs base64 (a third larger, several copies in memory, no streaming); a custom byte layout is what this design avoids. Arrow IPC streams typed buffers with zero-copy reads and is readable from most languages.
- **A flat column rather than Arrow's tensor type.** `arrow.fixed_shape_tensor` limits a row to 2^31−1 values (one day at 25 kHz doesn't fit), makes one row the smallest batch, and is recognized only by Arrow C++, pyarrow, R and arrow-rs; DuckDB rejects rows over 100,000 values, and pyarrow's tensor helpers refuse bool and time types. A flat column has none of these limits and is still a zero-copy view. The cost is that a reader without this document sees a vector rather than an array. Arrow's IPC `Tensor` message is a separate message type that streams and files don't carry.
- **One patch per stream.** Transports already separate messages (HTTP responses, files), so no framing of our own is needed.
- **Coordinates and attributes as JSON metadata.** They are small next to the data, must arrive first so readers can size and check the patch, and have different lengths along different dims. Their encoding mirrors DASCore's coordinate model so patches round-trip exactly.
- **The format's own tag grammar.** DASCore's identity encoder is built for hashing and loses information on purpose (numpy scalar types, units spelling), and changing it changes every id, so the two are kept apart.

## Open questions

1. **Name.** `ARROW` as the DASCore format name, or something specific like `DASARROW`?
2. **DASCore API.** The coordinate and attribute documents should come from the public API proposed in [DASCore #1485](https://github.com/DASDAE/dascore/issues/1485), whose field names this document should then adopt. A fix for rebuilding blank coordinates through `get_coord(runs=...)` is on a DASCore branch.
3. **Empty ids.** With provenance tracking off, DASCore's ids are empty strings, but building a patch with an empty `origin_id` mints a new one, so empty ids don't round-trip yet. #1485 asks DASCore to decide.
4. **Large irregular coordinates.** A million-label irregular coordinate is about 20 MB of JSON, stored twice in a file. Is the metadata limit, with refusal, enough for v1?
5. **Batch-size floor.** Is 65,536 values the right minimum for every batch but the last?
6. **Multi-patch files.** Out of scope here; is a directory of `.arrow` files plus a DASCore index enough?
7. **Chunk alignment.** Should the metadata record the source file's natural chunk size, so clients can align requests?
