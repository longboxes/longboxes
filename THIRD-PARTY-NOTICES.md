# Third-Party Notices

Longboxes is free software licensed under the GNU General Public
License, version 3.0 — see [`LICENSE`](LICENSE).

Longboxes is distributed as a Docker image that bundles its Python
dependencies, so the published image contains copies of the
third-party packages listed below, each under its own license. The
complete, verbatim license text of every bundled package travels
inside the image, in that package's installed metadata
(`*.dist-info/` under the Python environment).

This file records the project's **direct** dependencies and their
licenses. The image also contains their transitive dependencies — for
a complete, machine-generated inventory of everything installed, run a
tool such as [`pip-licenses`](https://pypi.org/project/pip-licenses/)
against the image:

```
docker run --rm --entrypoint sh ghcr.io/longboxes/longboxes:latest \
  -c "pip install pip-licenses >/dev/null && pip-licenses --with-license-file"
```

## Runtime dependencies

| Package | License |
|---|---|
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| sqlalchemy | MIT |
| alembic | MIT |
| asyncpg | Apache-2.0 |
| psycopg | LGPL-3.0-or-later |
| redis (redis-py) | MIT |
| rq | BSD-3-Clause |
| pydantic | MIT |
| pydantic-settings | MIT |
| argon2-cffi | MIT |
| jinja2 | BSD-3-Clause |
| rq-scheduler | MIT |
| rarfile | ISC |
| comicbox | LGPL-3.0-only |
| comicfn2dict | GPL-3.0-only |
| rapidfuzz | MIT |
| pillow | HPND (MIT-CMU) |

## Development-only dependencies

Installed in the image but used only for tests and linting — all
permissive: pytest (MIT), pytest-asyncio (Apache-2.0), ruff (MIT),
httpx (BSD-3-Clause), fakeredis (BSD-3-Clause), respx (BSD-3-Clause).

## Copyleft dependencies

Three dependencies are copyleft and warrant a specific note:

- **comicfn2dict — GPL-3.0-only.** Longboxes uses it directly (the
  filename matcher) and `comicbox` requires it. A GPL-3.0 component
  bundled into the distributed work is the reason Longboxes itself is
  licensed under GPL-3.0. Source: <https://github.com/ajslater/comicfn2dict>
- **comicbox — LGPL-3.0-only.** Used unmodified as a dependency. The
  LGPL is satisfied by shipping its source / a written offer, keeping
  its notices, and not preventing a user from replacing it. Source:
  <https://github.com/ajslater/comicbox>
- **psycopg (v3) — LGPL-3.0-or-later.** Same posture as comicbox —
  used unmodified; the LGPL obligations are notice retention and
  allowing replacement. Source: <https://github.com/psycopg/psycopg>

## Base image

The Docker image is built on Debian (`python:3.12-slim`) and adds the
system packages `unar`, `curl`, and `build-essential` via `apt`. These
carry their own licenses, documented by Debian. `unar` is used instead
of `unrar` for RAR/CBR extraction specifically to keep the image free
of `unrar`'s non-free license.
