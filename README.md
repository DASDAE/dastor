# dastor

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
```

```console
$ dastor serve config.yaml --host 0.0.0.0 --port 8000
```

The server runs as one process: it indexes the directory at start (and on its refresh interval), and loads patches in a thread pool.

## Access control

Access is per file. A file is visible to a user only when every patch in it matches a rule granting that user; a file is never partly served. With no rules, nothing is visible. Requests authenticate with `Authorization: Bearer <key>`; requests without the header are anonymous.

## API

| Endpoint | Returns |
|---|---|
| `GET /v1/summary` | Patch and file counts, the contents columns, the dims and each dim's extent. |
| `GET /v1/contents` | Patch metadata (JSON, or `format=arrow`). |
| `GET /v1/coverage?dim=time` | The spans of a dimension the patches cover. |
| `GET /v1/gaps?dim=time` | The gaps between them. |
| `GET /v1/data` | The patches, trimmed to the query (`format=arrow` or `format=dasdae`). |
| `GET /v1/patch/{data_id}` | One whole patch, by the `data_id` its contents row gives, as one Arrow IPC stream (404 if missing or not visible). |
| `GET /health` | Server status. |

Every endpoint but `/health` sees only what the caller may read, and takes query parameters that select patches: `<coord>_min` and `<coord>_max` bound a coordinate, and any other parameter selects an attribute, with DASCore's `*` and `?` wildcards:

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

## Development

```console
$ uv sync
$ uv run pytest
$ uv run ruff check . && uv run ruff format --check .
```

dastor tracks DASCore's `dev` branch (see `[tool.uv.sources]` in `pyproject.toml`).
