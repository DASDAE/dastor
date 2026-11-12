# Agent instructions

dastor is a FastAPI server for DAS data built on DASCore's `dev` branch.

- Layout: `src/dastor/` (`config`, `auth`, `acl`, `catalog`, `query`, `wire`, `tiles`, `annotations`, `sync`, `proxy`, `app`, `cli`, `client`), the viewer's page in `src/dastor/static/`, tests in `tests/`.
- The server serves one spool: one data directory and its index. Datasets are told apart by attributes or `source_path`, not by separate spools.
- Every request reads the spool through `Catalog.view`, the single access-control choke point. Keep it that way.
- Access control is per file: never serve part of a file. Rows are checked again on every selection and loaded patches again as they load (`View.select`, `View.check`), so a refresh or a changed file can only narrow what is served.
- DASDAE responses unlink their temporary file once it is open, which assumes POSIX.
- `app.py` must not use `from __future__ import annotations`: FastAPI resolves the `Annotated` aliases defined inside `create_app`.
- The viewer (`static/`) is plain JavaScript with no build step and no files from the internet. It talks only to the API. Its levels are ordinary products of the spool read through `Catalog.view`; tiles are not cached on the server, so every tile is checked as it loads.
- An annotation set is a DASCore `AnnotationSet` directory, `<owner>/<name>/`; only the store writes a feature's `created_time`, `modified_time` and `modified_by`. A set is shown to its owner and to those who may read every file of its acquisition (`View.acquisitions`), never on the strength of one file.
- `dastor browse <url>` serves the page locally and passes its requests to that server (`proxy.py`); the API key stays in the local process. `/v1/sync` exists only when `dastor browse` is given `--data-dir`: no configuration file can turn it on, since it writes to the server's disk. A synced file is written under a hidden name and linked into place whole, never replacing one (`sync.synced_file`).
- Checks: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`. After changing the viewer also run `uv run pytest -m browser` (needs Chromium; not run in CI).
- CI tests Python 3.14 and 3.15.
