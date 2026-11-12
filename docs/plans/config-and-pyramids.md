# Plan: config v1 and image pyramids

Agreed November 2026, after review. Nothing is started. dastor is experimental, so v1 replaces today's config rather than reading it.

**Principle:** dastor builds on DASCore's machinery (spools, their indexes, `chunk`, Zarr writes) and keeps no files of its own beside the config: no manifests, no stamps, no state. Whatever dastor needs to know is read from the spool's contents. A new kind of file needs explicit agreement.

Needs DASCore `dev` (0.1.26.dev637 or later: `dc.spool` of several paths, Zarr `append`) and `zarr>=3.4`.

## 1. Config v1

Every dastor file opens with what it is, so other programs can tell:

```yaml
kind: dastor-server        # or dastor-workspace
version: 1                 # an unknown version is refused
```

A file without them is refused with the v1 block it should become printed out, with patterns translated to whole-value form (`^public` becomes `public.*`).

```yaml
kind: dastor-server
version: 1

server:                    # an explicit --host or --port overrides
  host: 127.0.0.1          # the default: this machine only, behind a proxy
  port: 8000
  https: true
  root_path: /das          # passed to uvicorn; the proxy keeps the prefix

spools:                    # joined into one spool
  - name: raw
    path: /data/das
    index: /var/lib/dastor/raw.index     # optional, as now
    refresh_seconds: 300
  - name: fbe
    path: /data/fbe

pyramid:                   # see section 3
  path: /data/das_pyramid  # the default: beside the first spool, `<path>_pyramid`
  inputs: {...}
  products: [...]

inventory: /data/inventory # optional; read with dc.inventory and checked, never served

datasets:                  # ordered: a row belongs to the first it matches
  public:
    match: {tag: "public.*"}
    allow: ["*"]
  project_a:
    spools: [raw]
    match: {source_path: "project_a/.*"}
    allow: ["group:lab"]

groups:                    # the one place membership is stated
  lab: [alice, bob]
users:
  - {name: alice, key_sha256: ...}

annotations: {path: /data/das_annotations}
limits: {...}              # unchanged
```

- **Server.** `host` defaults to `127.0.0.1`. The CLI's `--host` and `--port` default to nothing, so only a flag given overrides the file; startup logs the address and where it came from. `root_path` is uvicorn's: the proxy passes the prefix on. One host name per server still holds (cookies aren't kept to a path or port), and `check` says so when `root_path` is set.
- **Spools.** Each is opened with `dc.spool(path, index=...)` and updated on its own refresh; the server joins them with `+` (DASCore can't yet update a joined spool, see `dascore/scratch/joined-spools.md`). A joined spool's `source_path` is absolute, so dastor keys a row by its spool's name and its path relative to that spool's root: matching, the API and responses never show absolute paths. Names are unique; `pyramid` is reserved; paths nested in one another are refused. `~` is expanded, and relative paths are relative to the config file. No environment interpolation: secrets come from `key_env`.
- **Datasets replace rules.** A dataset names the `spools` it covers (all of them when left out; an unknown name is an error), a `match` and an `allow` (`*`, `user:*`, `user:<name>`, `group:<name>`). `match` maps contents columns to regular expressions matching the whole value; a dataset with no `match` takes every row of its spools. A row without the column, or holding nothing in it, doesn't match. Columns holding numbers or times (`time_step`) are compared by value, not as text. A row belongs to the first dataset it matches, or to none, and is then served to no one. A file is visible when every row's dataset grants the reader: access stays per file.
- **Visitors.** A server is open to visitors when a dataset granting `*` matches at least one row.
- **Groups** are a section of their own, in the style of POSIX groups; users lose their `groups` key.
- **Workspace** (`kind: dastor-workspace`): `spools`, `pyramid`, `annotations`, `inventory` and `limits`, for `dastor browse` and `dastor.view`. A workspace is local only: it has no datasets (everything in it is open, as `dastor browse` is today), and nothing in it can turn on `/v1/sync`, which stays a command-line choice. `dastor browse <dir>` and `dastor.view(spool)` keep working without a file; they are a workspace with defaults (`dastor.view` with a spool handed in rather than a path). `dastor browse <url>` takes no workspace: it reads the server's.
- **Units** are pint's, as in DASCore: `s`, `min`, `h`, `d` for time, `m`, `km` for distance. A number without units counts samples. A unit of the wrong kind is refused with what it was taken for (`time: 1m` is "1 metre: did you mean 1min?").

Commands:

- `dastor init server [PATH] [--data DIR]` and `dastor init workspace [PATH] [--data DIR]`: write a starting file, commented, which `check` passes. They refuse to replace a file. `init server` makes one user and prints that user's key once; only its digest goes in the file. Given `--data`, they list the products found as commented pyramid inputs, from an existing index or a temporary one (never writing into the data directory).
- `dastor check CONFIG`: validates the file, then reads the existing indexes (`--index` updates them first) and reports what each user, group and visitors may read (files and bytes per dataset), files in no dataset, datasets matching nothing, overlapping datasets where an earlier one grants more, product inputs matching no or several products, stale products, and whether the inventory loads. It imports no custom function (`--import` does). `serve` refuses to start on what `check` calls an error.
- `dastor config-schema`: the JSON Schema of both kinds, for editors.

## 2. Pyramid access

A pyramid's files are ordinary rows of the joined spool, and the builder writes two attributes on each product (indexed, like any attribute): `pyramid_input`, the id of the input it was built from, and the input's `acquisition_key`.

**A pyramid row is visible to a reader only when every input row it covers is.** It covers the input rows of the same recording, from the same input, whose time spans overlap its own. The check is made on the contents in memory, as raw rows are checked today, so a pyramid follows the raw data at once: reordered or edited datasets, retagged or moved files, and files spanning several datasets narrow the levels on the next refresh, with no rebuild and nothing recorded beside the files. A row naming no input, or an unknown one, is served to no one; an attribute written by hand grants nothing, since access comes from the input rows.

A store holding many input files is hidden from a reader when any of them is, so the builder keeps a store to one dataset of one recording.

## 3. Pyramid products

```yaml
pyramid:
  path: /data/das_pyramid
  inputs:                                # products already in the spool, by selector
    raw: {spools: [raw], tag: raw}
    fbe: {spools: [fbe], tag: FBE}       # frequency band energy, written elsewhere
  products:                              # each builds on an input or another product
    - {id: max_1s,        from: raw, reduce: max,  time: 1s}
    - {id: max_100s_20m,  from: raw, reduce: max,  time: 100s, distance: 20m}
    - {id: mean_100s_20m, from: raw, reduce: mean, time: 100s, distance: 20m}
    - {id: std_100s_20m,  from: raw, reduce: std,  time: 100s, distance: 20m}
    - {id: max_1h_100m,   from: max_100s_20m, reduce: max, time: 1h, distance: 100m}
    - {id: fbe_rms_10s,   from: fbe, reduce: "labtools.reduce:rms", time: 10s}
    - {id: bands_60s,     from: raw, apply: "labtools.fbe:bands", chunk: 60s, overlap: 2s}
```

- **Inputs** select from the joined spool by `spools` and attributes, matched as datasets are, never from the pyramid itself. In each recording an input must match exactly one product (one kind, one set of steps); `check` fails otherwise and names them, so a selector is narrowed (`{tag: raw, time_step: 0.001}`).
- **Ids.** Every input and product has an id, which is the product's `tag`; an id equal to a tag already in the spool is refused. `from` names an input or a product, so products form a graph: cycles are refused, and products are built in dependency order.
- **Reductions** (`reduce`, over blocks of `time` and `distance`): `max`, `min`, `mean`, `std`, `rms`, `absmax`, `extreme` (the value of largest magnitude, with its sign) and `count`, ignoring NaN, or a custom `module:function`, `f(values, axis) -> values` in the style of NumPy, which must cope with NaN.
- **Transforms** (`apply`): a custom `module:function`, `f(patch) -> patch`, for what is not a reduction over blocks (band energy over windows). It gets chunks of `chunk` along time with `overlap` on both sides (required, `0` allowed), NaN where there is no data, and dastor trims the overlap from what it returns. Its output has a time dim, perhaps with a new step, of which `chunk` and `overlap` must be whole multiples. Its output is a recording of its own, not a level of its input.
- **Blocks.** A block size is a number of samples, or a quantity rounded to whole samples of the input with a warning. Time blocks align to multiples of their length from 1970, distance blocks to distance zero; a block's coordinates are its first sample. Where an input has gaps, a block holds NaN there.
- **Building** uses DASCore: `spool.chunk(time=<block>)` hands the builder one block at a time across files, and each block is reduced in memory. Memory is one input block; `check` refuses a product whose input block would exceed `limits.max_build_bytes` and suggests building it from an intermediate product. Chained reductions are exact for `max`, `min`, `absmax`, `extreme`, `count`, and `mean` over full blocks; others are allowed and warned about.
- **Storage:** written with `dc.write(..., "ZARR", append="time")`, one store per dataset, recording and product. DASCore writes the new chunks before the shape and consolidates last, so readers never see part of a block, and the spool's next update sees the new data. A block continuing none (after a gap) becomes a new group in the store.
- **The live end** of a recording: the last block is written only once data beyond it exists; `--final` writes it with NaN where data is missing.
- **Freshness** is read from the contents, with nothing recorded: a product is stale where its input covers time it doesn't (new data, a backfill) or it covers time its input no longer does (a deletion). Source files are assumed not to be rewritten in place; after doing so, rebuild with `--force`. A stale store is rewritten whole (`dc.write` without `append` replaces it atomically).
- **Command:** `dastor build-pyramid CONFIG|DIR [--product ID] [--final] [--force]`. Given a directory with no config it builds a default ladder (`extreme` at about 1 s and 100 s) into `<dir>_pyramid`. It builds only what is stale, and imports custom functions only here.

## 4. Viewer

The Levels panel is removed. A pyramid button beside the level control opens a pop-up listing the recording's products by reduction and resolution, with stale ones marked; the user picks the reduction to zoom through and which of its levels. Each level switches in at one sample per pixel (today's rule); other thresholds sit under "advanced", in seconds per screen. The choice is kept in the browser per recording; nothing is written to the server. The tile statistic defaults to the level's `reduce`. With no pyramid, the button is hidden, and the "too much to load" message names `dastor build-pyramid`. `/v1/viewer/products` returns `pyramid_input`, `reduce` and `stale` per product, and for each recording how many of its levels the reader can't see, so the page can say levels are hidden rather than show nothing.

## Examples

A server with two spools, a pyramid and three datasets:

```yaml
kind: dastor-server
version: 1

server:
  host: 127.0.0.1             # the proxy on this machine serves it to others
  port: 8000
  https: true
  root_path: /das

spools:
  - name: raw
    path: /data/das
    index: /var/lib/dastor/raw.index    # the data mount is read-only
    refresh_seconds: 300
  - name: fbe                           # frequency band energy, from another pipeline
    path: /data/fbe
    refresh_seconds: 3600

pyramid:
  path: /data/pyramid
  inputs:
    raw: {spools: [raw], tag: raw}
    fbe: {spools: [fbe], tag: FBE}
  products:
    - {id: max_1s,        from: raw,          reduce: max,  time: 1s}
    - {id: max_100s_20m,  from: raw,          reduce: max,  time: 100s, distance: 20m}
    - {id: mean_100s_20m, from: raw,          reduce: mean, time: 100s, distance: 20m}
    - {id: std_100s_20m,  from: raw,          reduce: std,  time: 100s, distance: 20m}
    - {id: max_1h_100m,   from: max_100s_20m, reduce: max,  time: 1h,   distance: 100m}
    - {id: fbe_rms_10s,   from: fbe,          reduce: "labtools.reduce:rms", time: 10s}
    - {id: bands_60s,     from: raw,          apply: "labtools.fbe:bands", chunk: 60s, overlap: 2s}

inventory: /data/inventory

datasets:
  public:
    match: {tag: "public.*"}
    allow: ["*"]
  project_a:
    spools: [raw]
    match: {source_path: "project_a/.*"}
    allow: ["group:lab", "user:carol"]
  fbe:
    spools: [fbe]
    allow: ["user:*"]

groups:
  lab: [alice, bob]

users:
  - {name: alice, key_sha256: 3f0c...e91a}
  - {name: bob,   key_sha256: 8b21...04cd}
  - {name: carol, key_sha256: c7d9...5f30}

annotations:
  path: /data/das_annotations
```

- Visitors read only `public`. Without it, no dataset grants `*` and visitors are sent to `/signin`.
- `project_a`'s coarse levels are read by those who may read the raw files they cover: `group:lab` and `carol`. No dataset names the pyramid.
- `bands_60s` is a recording of its own, not a level of `raw`.

A workspace for `dastor browse workspace.yaml`:

```yaml
kind: dastor-workspace
version: 1

spools:
  - name: field2026
    path: ~/das/field2026

pyramid:                                 # path: ~/das/field2026_pyramid
  inputs:
    raw: {tag: raw}
  products:
    - {id: absmax_1s, from: raw,       reduce: absmax, time: 1s}
    - {id: absmax_1m, from: absmax_1s, reduce: absmax, time: 1min, distance: 10m}

inventory: ~/das/inventory
annotations:
  path: ~/das/annotations
```

The smallest server, everything public:

```yaml
kind: dastor-server
version: 1
spools:
  - {name: data, path: /data/das}
datasets:
  all: {allow: ["*"]}
```

## Order

1. Config v1 with workspaces: `kind` and `version`, `server`, named spools joined into one, datasets, groups, units; `browse` and `dastor.view` moved onto it; `init`, `check`, `config-schema`; the docs (`41-config`, `32-access`, `13-proxy`, `44-cli`, the glossary, README, AGENTS.md).
2. `inventory`, loaded and checked.
3. The builder: graph, reductions, transforms, chunked builds, Zarr appends, freshness, `build-pyramid`.
4. Pyramid access in the catalog, and `/v1/viewer/products`.
5. The viewer's pyramid pop-up, replacing the Levels panel.

This plan replaces item 4 of `viewer-reductions.md` (the level builder) and absorbs its signed extreme (item 1) as the `extreme` reduction.
