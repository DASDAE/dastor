# Plan: config v1 and image pyramids

Agreed November 2026. Nothing is started. dastor is experimental, so v1 replaces today's config rather than reading it: an old file fails with an error naming what to add.

## 1. Config v1

Every dastor file opens with what it is, so other programs can tell:

```yaml
kind: dastor-server        # or dastor-workspace
version: 1                 # an unknown version is refused
```

```yaml
kind: dastor-server
version: 1

server:                    # the CLI's --host and --port still override
  host: 127.0.0.1          # the default: this machine only, behind a proxy
  port: 8000
  https: true
  root_path: /das

spools:                    # joined into one spool
  - name: raw
    path: /data/das
    index: /var/lib/dastor/raw.index     # optional, as now
    refresh_seconds: 300
  - name: fbe
    path: /data/fbe

pyramid:                   # see section 3
  path: /data/das/.pyramid               # the default: inside the first spool
  inputs: {...}
  products: [...]

inventory: /data/inventory               # optional; dc.inventory, for the gateway

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

- **Server.** `host` defaults to `127.0.0.1`, here as on the command line: serving other machines is a choice written down, and a proxy on the same machine needs nothing more.
- **Spools.** Each has a name and its own index and refresh. `source_path` stays relative to its own root. Paths nested in one another are refused. `AGENTS.md`'s "one directory" becomes "one spool, joined from named directories".
- **Datasets replace rules.** A dataset names the `spools` it covers (all of them when left out; an unknown name is an error, where a pattern would silently match nothing), a `match` (regular expressions over contents columns, each matching the whole value, all of which must match) and an `allow` (`*`, `user:*`, `user:<name>`, `group:<name>`). A row belongs to the first dataset it matches, or to none, and is then invisible. A file is visible when every row's dataset grants the reader: access stays per file.
- **Groups** are a section of their own, in the style of POSIX groups; users lose their `groups` key.
- **Workspace** (`kind: dastor-workspace`): `spools`, `pyramid`, `annotations`, `inventory` and `limits` for `dastor browse` and `dastor.view`. `dastor browse <dir>` keeps working without a file, as a workspace with defaults. A workspace never holds a key: it names where one comes from (`key_env: DASTOR_KEY`).

Commands:

- `dastor check CONFIG`: validates, indexes, and prints what each user, group and visitors may read (files and bytes per dataset), datasets which match nothing, overlapping datasets, stale pyramids, and whether the inventory loads.
- `dastor init server [PATH] [--data DIR]` and `dastor init workspace [PATH] [--data DIR]`: write a starting file, commented, which `check` passes. They refuse to replace an existing file. `init server` makes one user and prints that user's key once; only its digest goes in the file. Given `--data`, they index the directory and list the products found as commented pyramid inputs.
- `dastor config-schema`: the JSON Schema of both kinds, for editors.

## 2. Pyramid access

The builder never mixes datasets: it groups the input rows by dataset, and stamps each product with `dataset: <name>` and `dataset_rev: <hash of that dataset's match>`. Custom attributes are indexed, so both are columns of the contents.

- Rows of a pyramid spool take their dataset from the stamp, not from `match`; rows of a raw spool always from `match`, so a raw file can't grant itself access by carrying a `dataset` attribute.
- A pyramid row whose `dataset_rev` differs from the dataset's current hash is hidden until rebuilt, so a narrowed dataset never leaks through levels built before the change.

## 3. Pyramid products

```yaml
pyramid:
  path: /data/das/.pyramid
  inputs:                                # products already in the spool, by selector
    raw: {tag: raw}
    fbe: {spools: [fbe], tag: FBE}       # frequency band energy, written elsewhere
  products:                              # each builds on an input or another product
    - {id: max_1s,        from: raw, reduce: max,  time: 1s}
    - {id: max_100s_20m,  from: raw, reduce: max,  time: 100s, distance: 20m}
    - {id: mean_100s_20m, from: raw, reduce: mean, time: 100s, distance: 20m}
    - {id: std_100s_20m,  from: raw, reduce: std,  time: 100s, distance: 20m}
    - {id: max_1h_100m,   from: max_100s_20m, reduce: max, time: 1h, distance: 100m}
    - {id: fbe_rms_10s,   from: fbe, reduce: "mypkg.reductions:rms", time: 10s}
    - {id: fbe_bands,     from: raw, apply: "mypkg.fbe:bands", chunk: 60s, overlap: 2s}
```

- **Inputs** select from the joined spool, by `spools` as datasets do and by attributes, so an input may come from any spool. The pyramid is one section, not a part of a spool: its `path` is read as a spool of its own, named `pyramid`.
- **Matching** is the same as for datasets: regular expressions matching the whole value, so `tag: raw` is exactly `raw`. In each recording an input must match exactly one product (one kind, one set of steps); `check` fails otherwise and names them, so a selector is narrowed (`{tag: raw, time_step: 0.001}`) rather than feeding a product mixed data.
- **Ids.** Every input and product has an id, unique in the spool, which is also the product's `tag`. `from` names an input or a product, so products form a graph: cycles are refused, and products are built in dependency order.
- **Reductions** (`reduce`, over blocks of `time` and `distance`): `max`, `min`, `mean`, `std`, `rms`, `absmax`, `extreme` (the value of largest magnitude, with its sign) and `count`, or a custom `module:function`, `f(values, axis) -> values` in the style of NumPy. dastor does the blocking, alignment and edges.
- **Transforms** (`apply`): a custom `module:function`, `f(patch) -> patch`, for what is not a reduction over blocks (band energy over windows). dastor hands it aligned chunks of `chunk` along time, each with `overlap` on both sides (required, `0` allowed), and trims the overlap from what it returns, so chunk edges leave no seams. The output may change the other dims but keeps time.
- **Inexact chains** are allowed and warned about by `check`: a built-in reduction over a reduced input which differs from the same reduction over the source (`mean` of `max`, `std` of `std`). `max`, `min`, `absmax`, `extreme` over the same reduction, and `count` summed, are exact.
- **Units** are physical and must be whole multiples of the input's step (`check` fails rather than rounding). Time blocks align to multiples of their length from 1970, so incremental builds line up; a partial block at an edge is kept.
- **Storage:** Zarr, one store per dataset, recording (`acquisition_key`) and product, appended along time. Its attributes record the product's `reduce` or `apply`, its `from`, and its block sizes or chunking.
- **Build:** `dastor build-pyramid CONFIG [--spool NAME] [--product ID]` reads each input file once and computes every product built from that input in one pass, then the products built on those. It builds only what is stale.
- **Freshness:** a manifest in the pyramid directory records each product's spec hash and the fingerprints of the files it was built from (path, size, modified time, from the index). Staleness compares them with the index in memory, opening no file, so it is as fresh as the last refresh; it passes down the graph, so a product built on a stale one is stale too. `/v1/viewer/products` reports `stale` per product; `dastor check` lists them.

## 4. Viewer

A pyramid icon beside the level control opens a panel listing the recording's products, grouped by reduction and ordered by resolution, with the stale ones marked. The user picks the reduction to zoom through, which of its levels, and from how many samples per pixel each switches in. The choice is kept in the browser per recording; nothing is written to the server. The tile statistic defaults to the level's `reduce`. A product made by `apply` is a recording of its own, not a level of its input, when its dims differ.

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
- The pyramid's files carry their input's dataset, so `project_a`'s coarse levels are read by exactly `group:lab` and `carol`; no dataset names the pyramid.
- `bands_60s` changes the dims, so the viewer shows it as a recording of its own, not as a level of `raw`.

A workspace for `dastor browse workspace.yaml`:

```yaml
kind: dastor-workspace
version: 1

spools:
  - name: field2026
    path: ~/das/field2026

pyramid:                                 # path: ~/das/field2026/.pyramid
  inputs:
    raw: {tag: raw}
  products:
    - {id: absmax_1s, from: raw,       reduce: absmax, time: 1s}
    - {id: absmax_1m, from: absmax_1s, reduce: absmax, time: 60s, distance: 10m}

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

1. Config v1: `kind` and `version`, `server`, named spools joined into one, datasets with `spools` and `allow`, groups; `browse` and `dastor.view` moved onto it; `init`, `check` and `config-schema`.
2. `inventory`, loaded and checked.
3. Pyramid builder: spec and its graph, reductions (built-in and custom), then transforms, one pass per input file, Zarr appends, dataset stamps, manifest, `build-pyramid`.
4. Pyramid access and freshness in the catalog and `/v1/viewer/products`.
5. The viewer's pyramid panel.
6. Workspaces.

This plan replaces item 4 of `viewer-reductions.md` (the level builder) and absorbs its signed extreme (item 1) as the `extreme` reduction.
