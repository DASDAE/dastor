# Agent instructions

dastor is a FastAPI server for DAS data built on DASCore's `dev` branch.

- Layout: `src/dastor/` (`config`, `auth`, `acl`, `catalog`, `query`, `wire`, `app`, `cli`, `client`), tests in `tests/`.
- The server serves one spool: one data directory and its index. Datasets are told apart by attributes or `source_path`, not by separate spools.
- Every request reads the spool through `Catalog.view`, the single access-control choke point. Keep it that way.
- Access control is per file: never serve part of a file. Rows are checked again on every selection and loaded patches again as they load (`View.select`, `View.check`), so a refresh or a changed file can only narrow what is served.
- DASDAE responses unlink their temporary file once it is open, which assumes POSIX.
- `app.py` must not use `from __future__ import annotations`: FastAPI resolves the `Annotated` aliases defined inside `create_app`.
- Checks: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`.
- CI tests Python 3.14 and 3.15.
