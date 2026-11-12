<p align="center"><img src="docs/images/dastor-logo.svg" alt="DAS Tor logo" width="420"></p>

**Experimental.** dastor is an early-stage, experimental package. Its API and wire format will change without notice. Don't depend on it yet.

dastor is a server for distributed acoustic sensing (DAS) data, similar in spirit to FDSN web services, built on [DASCore](https://github.com/DASDAE/dascore). It serves one directory of DAS files as one spool: metadata queries backed by DASCore's spool index, and patch data trimmed to the request, with per-file access control. Datasets live together under the directory and are told apart by their attributes (such as `tag` or `station`) or their paths.

## Running a server

dastor needs DASCore's `dev` branch until a DASCore release includes what it uses, so install both from GitHub:

```console
$ pip install "dascore @ git+https://github.com/DASDAE/dascore@dev" "dastor @ git+https://github.com/DASDAE/dastor"
```

Then make an API key and write a config:

```console
$ dastor new-key
key:        <give this to the user>
key_sha256: <put this in the config>
```

```yaml
spool:
  path: /data/das               # a directory of DAS files, indexed recursively
  refresh_seconds: 300          # re-index every 5 minutes (omit to index only at start)
  rules:
    - column: tag               # a column of the spool contents
      pattern: "^public"        # a regular expression searched in it
      allow: ["*"]              # everyone, including anonymous requests
    - column: source_path       # paths are relative to the directory
      pattern: "^project_a/"
      allow: ["group:lab"]      # or "user:<name>"
users:
  - name: alice
    key_sha256: <digest from dastor new-key>
    groups: [lab]
limits:
  max_request_bytes: 2147483648 # largest data response, estimated before loading
  max_concurrent_loads: 4       # patches loading at once
  max_tile_bytes: 67108864      # most one viewer tile may read
  max_view_bytes: 536870912     # most the viewer reads for one screen
  max_upload_bytes: 67108864    # largest annotation set uploaded as a file
annotations:                    # optional: turns on the annotation API
  path: /data/das_annotations   # a directory outside the data directory
  anonymous_owner: guest        # optional: the name anonymous requests save under
```

```console
$ dastor serve config.yaml --host 0.0.0.0 --port 8000
```

The server runs as one process: it indexes the directory at start (and on its refresh interval), and loads patches in a thread pool.

## Viewer

dastor includes a viewer for looking at data in the browser, annotating it and choosing areas to download. To use it on a directory of your own, with no config file:

```console
$ dastor browse /data/das
```

This serves the directory on `127.0.0.1`, opens the viewer, and keeps annotations beside the data in `/data/das_annotations` (`--annotations` puts them elsewhere). A server started with `dastor serve` has the same viewer at `/viewer/`. The viewer has no sign-in yet, so on a server with users it shows only what anonymous requests may read, and saves annotations only if the config names an `anonymous_owner`; use it on a trusted network.

**Products and levels.** The viewer draws products: the patches of one kind (the same `acquisition_key`, `tag` and DASCore's other kind attributes) with the same dims and steps. Products sharing an `acquisition_key` are one recording, and the Levels panel lists them from coarse to fine. Tick the ones to zoom through; the viewer draws the coarsest ticked product with at least one sample for each pixel along time (the finest, when none has). The control at the bottom right shows which level is on screen and holds the view on one when clicked ("auto" follows the zoom again). dastor does not make coarse products: write them into the directory with DASCore (for example the maximum over each second), tagged so they can be told apart.

**Limits.** A view which would read more than `max_view_bytes` of a product is not loaded. The nearest coarser ticked product stands in; with none, or when the view is held on that product, the viewer draws an outline of where the product has data. Tick a coarser product, set the level to auto, or zoom in.

**Annotations.** Points, paths, polygons and boxes are drawn into the set being edited (the first drawing makes one, `annotations`) and saved as DASCore annotation sets, each a directory `<owner>/<name>/` with Parquet tables, which `dascore.annotations(path)` reads. Only a set's owner changes it. Others read it if they may read every file of the acquisition it was made on (a set made on data naming no acquisition counts as on all such files), so annotations never describe data their reader can't see. A label box opens at each new annotation with the last label already in it, so repeating a label is one key. A column added to one annotation is offered on the others of its kind (point, path, polygon or box) in the set. To correct someone else's annotations (or a detector's), select them and copy them into your own set (`c`): each copy records its source in `derived_from` (`owner/set/feature id`) and `derived_revision`. The server sets each feature's `created_time`, `modified_time` and `modified_by`. Edits are saved to the server's annotation directory as they are made, and "save" writes them at once; "download" gives the set being edited as a zip of its folder, and "load" makes a new set from such a zip or from a bare table of annotations (`.parquet` or `.csv`), so sets move between the viewer and DASCore scripts. Features too close together to tell apart are drawn as one marker with their number; hovering says what it holds and a click zooms in.

**Downloading an area.** The Download section takes an area from the selected annotation ("selected": the box around it), from what is on screen ("window"), or from a box dragged over the data ("draw"). Tick the products to take (the finest by default; any product of the recording, in the levels or not): it shows their size over that area and downloads them as one DASDAE file from `/v1/data?product=<id>&product=<id>`. "Sync", which will copy the area into a local data directory, is not available yet and is greyed out.

**Mouse and keys.** The left button belongs to the tool: "select" picks an annotation and drags it or one of its vertices, and the drawing tools place points and drag boxes. A path or polygon ends with Enter, a double-click or a right-click. A click near a polygon's first corner closes it, and a point placed near a vertex of any annotation on show snaps to it. The viewer starts with the pan tool, whose drag moves the view and whose click selects an annotation; a double-click (or `v`) changes to the select tool, and Esc goes back. The middle button (or Space with the left button) drags the view in every tool, the wheel zooms, the arrows pan, and Tab moves on by a screen in time (Shift+Tab back). The strip under the time axis is the whole recording, with the annotations and the window on screen marked; a click or drag on it moves the window, and `b` goes back to the view before a jump. The colour bar beside the plot shows the limits, a drag on its top or bottom moves that limit and on its middle both, and a click on it sets them from what is on screen. The diverging colour maps (white in the middle) keep the limits either side of zero. The button beside `?` changes between a dark and a light theme. `?` lists every key.

## Access control

Access is per file. A file is visible to a user only when every patch in it matches a rule granting that user; a file is never partly served. With no rules, nothing is visible. Requests authenticate with `Authorization: Bearer <key>`; requests without the header are anonymous.

## API

| Endpoint | Returns |
|---|---|
| `GET /v1/summary` | Patch and file counts, an estimate of the array bytes, the contents columns, the dims and each dim's extent. |
| `GET /v1/contents` | Patch metadata (JSON, or `format=arrow`). |
| `GET /v1/coverage?dim=time` | The spans of a dimension the patches cover. |
| `GET /v1/gaps?dim=time` | The gaps between them. |
| `GET /v1/data` | The patches, trimmed to the query (`format=arrow` or `format=dasdae`). |
| `GET /v1/patch/{data_id}` | One patch, by the `data_id` its contents row gives, as one Arrow IPC stream; `<coord>_min`/`<coord>_max` trim it (404 if missing or not visible). |
| `GET /v1/index` | A DASCore spool index (SQLite) of the patches the caller may read: metadata only, for the client. |
| `GET /v1/viewer/products` | The products the caller may read (see [Viewer](#viewer)), each with an `id`, its steps, extents and recorded spans, and the viewer's limits. |
| `GET /v1/viewer/tile?product=<id>` | One image of a product: `<x>_min`/`<x>_max` and `<y>_min`/`<y>_max` bound it, `width` and `height` size it, and `stat` is `max`, `mean` or `absmax`. Little-endian float32 rows, the lowest y first, NaN where there is no data (204 if none, 413 over `max_tile_bytes`). |
| `GET /v1/annotations` | The annotation sets the caller may read (their own, and those on acquisitions all of whose files they may read), and the name the caller saves under. |
| `GET`, `PUT`, `DELETE /v1/annotations/{owner}/{name}` | One annotation set, as the JSON records of its annotations and features tables. A save states the `base_revision` it was read at (409 if the set has moved on); only the owner saves or deletes. |
| `GET /v1/annotations/{owner}/{name}/export` | The set as a zip archive of its directory. |
| `POST /v1/annotations/{owner}/{name}/import?filename=<name>` | Makes a new set from the request body: a zip of a set's directory, or a bare `.parquet` or `.csv` table (`dims` for a table stating none, `acquisition_key` for a set naming none). 409 if the set exists. |
| `GET /health` | Server status. |

Every endpoint but `/health` sees only what the caller may read, and the summary, contents, coverage, gaps, data, patch and tile endpoints take query parameters that select patches: `<coord>_min` and `<coord>_max` bound a coordinate, `product=<id>` keeps one product of `/v1/viewer/products` (repeat it to keep several), and any other parameter selects an attribute, with DASCore's `*` and `?` wildcards:

```console
$ curl -H "Authorization: Bearer $KEY" \
    "http://localhost:8000/v1/data?time_min=2020-01-03T00:00:04&time_max=2020-01-03T00:00:10&distance_max=50&tag=public*" \
    -o patches.arrow
```

In JSON, times are ISO 8601 strings and durations are seconds. Data requests answer 204 when nothing matches and 413 when the response would exceed `max_request_bytes`. Each patch is checked against the access rules again as it loads, so a file changed since it was indexed isn't served by its old permissions: a DASDAE request answers 409, and an Arrow stream ends early (as it also does if patches turn out larger than estimated).

### Wire format

`format=arrow` data responses hold one Arrow IPC stream per patch, each preceded by its length as an unsigned 64-bit little-endian integer. Each stream is a patch in DASCore's `ARROW` format: the values as one Arrow column, with the dims, coordinates and attributes in the schema metadata (see [`docs/arrow-patch-format.md`](docs/arrow-patch-format.md)). DASCore reads a single stream with `dc.spool(path)` once it is saved as `.arrows`. With dastor installed:

```python
from dastor.wire import read_framed

with open("patches.arrow", "rb") as fi:
    patches = list(read_framed(fi))  # DASCore patches
```

`format=dasdae` returns one DASDAE HDF5 file, which `dascore.spool` reads.

## Python client

`dastor.Client` opens the server's patches as a DASCore spool:

```python
import dascore as dc
from dastor import Client

spool = Client("http://localhost:8000", key=KEY).spool()
sub = spool.select(time=("2020-01-03T00:00:04", "2020-01-03T00:00:10"), tag="public*")
for patch in sub.chunk(time=2):
    ...
```

The spool comes from the server's index (`/v1/index`), so selecting, chunking, coverage and contents run locally without further requests. Each patch is downloaded when it loads (`/v1/patch/{data_id}`), trimmed on the server to the part the selection needs.

## Development

```console
$ uv sync
$ uv run pytest
$ uv run ruff check . && uv run ruff format --check .
$ uv run pytest -m browser    # the viewer in a headless Chromium; not run in CI
```

dastor tracks DASCore's `dev` branch (see `[tool.uv.sources]` in `pyproject.toml`).
