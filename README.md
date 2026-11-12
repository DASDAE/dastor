<p align="center"><img src="https://raw.githubusercontent.com/DASDAE/dastor/main/docs/images/dastor-logo.svg" alt="DAS Tor logo" width="420"></p>

**Experimental.** dastor is an early-stage, experimental package. Its API and wire format will change without notice. Don't depend on it yet.

dastor is a server for distributed acoustic sensing (DAS) data, similar in spirit to FDSN web services, built on [DASCore](https://github.com/DASDAE/dascore). It serves one directory of DAS files as one spool: metadata queries backed by DASCore's spool index, and patch data trimmed to the request, with per-file access control. It comes with a viewer for looking at data in the browser, annotating it and choosing areas to download.

**Documentation: <https://dasdae.github.io/dastor/>**

## Install

dastor needs DASCore's `dev` branch until a DASCore release includes what it uses, so install both from GitHub:

```bash
pip install \
  "dascore @ git+https://github.com/DASDAE/dascore@dev" \
  "dastor @ git+https://github.com/DASDAE/dastor"
```

## Quick start

Open the viewer on a directory of DAS files:

```bash
dastor browse /data/das
```

Show a spool or patch in a notebook:

```python
import dascore as dc
import dastor

dastor.view(dc.get_example_spool("random_das"))
```

Serve a directory to others, with the access rules and users in a [config file](https://dasdae.github.io/dastor/user-guide/config.html):

```bash
dastor serve config.yaml --host 0.0.0.0 --port 8000
```

Read a server's data as a DASCore spool:

```python
from dastor import Client

spool = Client("http://localhost:8000", key=KEY).spool()
```

The [tutorial](https://dasdae.github.io/dastor/user-guide/first-look.html) walks through a first session with the viewer.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pytest -m browser    # the viewer in a headless Chromium; not run in CI
uv run --group docs great-docs build    # the docs site, in great-docs/_site (needs Quarto)
```

dastor tracks DASCore's `dev` branch (see `[tool.uv.sources]` in `pyproject.toml`). The docs' pages are in `docs/guide/`.
