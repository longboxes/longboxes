# Longboxes

ComicVine-native, self-hosted comic library manager.

> **Status:** Phase 5 — full library browse UI. `/library` shows every volume you own at least one matched issue from (grid or table view, filters by publisher / year / has-missing-issues). `/volume/{id}` shows the volume's full issue list with owned/missing badges, story-arc names, and links into each issue. `/issue/{id}` shows full credits, characters, arcs, neighbors (prev/next by issue number), and which files on disk hold the issue's content — hydrating stub records from ComicVine on first view. The home page surfaces recently-added items; reader-dependent sections wait for Phase 6.

See [`comicvine-library-manager-design.md`](../comicvine-library-manager-design.md) for the design doc (kept in your local design notes, not committed here).

## Prerequisites

- Docker + Docker Compose
- Optional: [`just`](https://github.com/casey/just) for the shortcuts in `justfile`
- Optional (editor / IDE only): [`uv`](https://docs.astral.sh/uv/) and Python 3.12

## First-run

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec web alembic upgrade head
curl http://localhost:8612/health
```

Or with `just`:

```sh
cp .env.example .env
just up
just migrate
curl http://localhost:8612/health
```

You should see:

```json
{"status":"ok","env":"dev"}
```

## Verify the background job queue

```sh
just test-job
just logs worker
```

You should see a `noop job ran: phase-0 test` line in the worker logs.

## First-run admin setup (Phase 1)

After the stack is up and migrations have run, open the web UI:

```
http://localhost:8612/
```

You'll be redirected to `/setup` to create the first admin account. Subsequent
users can be added with the CLI helper:

```sh
just create-user alice mypassword viewer    # role defaults to viewer if omitted
just create-user bob  anotherpw    admin
```

(The admin web UI for managing users arrives in Phase 9 — the CLI is the
supported path until then.)

Sign out from the home page; sessions are Redis-backed and survive restarts of
the `web` container but not of `redis`.

### Forgot your password?

The CLI bypasses the web auth entirely, so password loss is a one-line
recovery rather than a database surgery:

```sh
just list-users                              # see who exists
just reset-password alice newpass123         # rewrite the password_hash for an existing user
just create-user recovery somepass admin     # or just provision a fresh admin if you forgot the username too
```

`reset-password` only touches `password_hash`; the user's role and any
other fields stay put. `list-users` never echoes the password hash.

## Pointing at a library (Phase 2)

Set the host path you want indexed via the `LIBRARY_PATH` env var. That path
gets bind-mounted read-only into the `web` and `worker` containers at
`/library`, and `LIBRARY_PATHS=/library` (the default) tells the scanner to
walk it:

```sh
LIBRARY_PATH=/path/to/your/comics docker compose up -d --build
```

Then either wait for the scheduled scan (default: hourly) or trigger one
immediately from the admin UI (`/admin` → "Rescan now") or the CLI:

```sh
just scan
just logs worker          # watch progress
just library-paths        # confirm the path was seeded
```

**Expect the first scan to be slow.** Every file is hashed and ComicInfo-parsed
exactly once. For a 10,000-file / 500 GB library, plan on 30–90 minutes
depending on storage speed and CPU. Subsequent scans complete in seconds
because unchanged files take the path + mtime + size fast path.

The scanner is content-aware: it tracks files by sha256 in a `files` table and
paths in a separate `file_locations` table (many-to-one). A file moved between
directories is recognised as the same content — your existing match state is
preserved. Duplicates show up as one `files` row with multiple location rows.

## ComicVine metadata (Phase 3)

Get a free ComicVine API key from
[comicvine.gamespot.com/api](https://comicvine.gamespot.com/api/) (sign-in
required, takes a minute). Then paste it into `/admin` → **ComicVine API key**
→ Save.

Add your first volume from the same page: paste the CV ID (the integer in URLs
like `comicvine.gamespot.com/volume/4050-18166/`) into **Add a volume**. The
fetch is synchronous; on success the page shows the volume in "Recently
fetched volumes" and stub rows for every issue in the volume are persisted in
`cv_issues` (they'll be hydrated to full records when something asks for that
specific issue).

CLI equivalent:

```sh
just add-volume 18166
```

## Matching files to ComicVine issues (Phase 4)

Once a scan has indexed your files and you've fetched the relevant volumes,
the matcher fires automatically: the scanner enqueues a `match_file` job for
each new file. To re-run matching across the whole library (e.g., after
fetching more volumes or marking files un-excluded):

```sh
just match-all          # or click "Match all unmatched" on /admin
```

Check progress in `docker compose logs worker -f` (each job logs its result)
or visit `/admin/health` for the aggregate report:

- Total files / locations / duplicate footprint (bytes)
- ComicInfo coverage breakdown (`full_with_cvid` / `partial` / `none`)
- Match-status breakdown (`auto` / `confirmed` / `pending` / `unmatched`)
- **Projected** auto-match rate (from ComicInfo coverage alone — answers
  "will Longboxes work well for my library?" before the matcher has run)
- **Observed** auto-match rate (the actual `(auto + confirmed) / total`
  after a matcher run)

Inspect individual matches in psql:

```sql
SELECT
  fl.path,
  fm.status,
  fm.confidence,
  cvi.name AS issue_name,
  cvi.issue_number,
  cvv.name AS volume_name,
  cvv.year AS volume_year
FROM file_matches fm
JOIN files f         ON f.id = fm.file_id
JOIN file_locations fl ON fl.file_id = f.id AND fl.missing_since IS NULL
LEFT JOIN cv_issues cvi   ON cvi.cv_id = fm.issue_cv_id
LEFT JOIN cv_volumes cvv  ON cvv.cv_id = cvi.volume_cv_id
ORDER BY fm.confidence DESC;
```

The HTTP client is rate-limited (per-resource token buckets), retries on
HTTP 420/429 and CV-level rate-limit codes with exponential backoff, and
serves cached reads stale-while-revalidate: a stale read returns the cached
row immediately and enqueues a background revalidate job. TTL defaults
follow the design doc — 7 days for volumes/issues, 30 days for
persons/characters/teams/publishers, 1 hour for search.

## Editor / local Python setup (optional)

If you want autocomplete and type-checking outside Docker:

```sh
uv sync
```

This creates `.venv/` with all dependencies. Point your editor's Python interpreter at it.

## Project layout

```
app/
  main.py            FastAPI app
  config.py          Settings (pydantic-settings)
  db.py              SQLAlchemy async engine + session factory
  redis_client.py    Async Redis client (used by sessions)
  templates_env.py   Jinja2 templates wrapper
  worker.py          RQ worker entrypoint
  admin/             Admin-only routes (rescan, CV key, add-volume, health)
  archives/          CBZ/CBR readers + ComicInfo.xml parser
  comicvine/         CV HTTP client + rate limiter + cache layer
  library_browse/    Public-facing /library /volume/{id} /issue/{id} routes
  matcher/           Filename parser + 4-stage match pipeline
  auth/              Password hashing, sessions, dependencies, routes
  jobs/              RQ background jobs (scan, match_file stub)
  models/            ORM models (one per file, registered in __init__.py)
  scanner/           Library walker + two-phase reconciler
  scripts/           Operator CLI helpers (create_user, ...)
  services/          App-level services (settings, ...)
  templates/         Jinja2 HTML templates
alembic/             Migrations
  versions/          One file per revision
tests/               Pytest tests
  fixtures/          Synthetic CBZ builder
docker-compose.yml
Dockerfile           Single image used by web, worker, and scheduler
justfile             Common dev commands
pyproject.toml       Dependencies (managed by uv)
```

## Common commands

| Command | What it does |
|---------|--------------|
| `just up` | Build + start the stack |
| `just down` | Stop the stack (keeps data) |
| `just nuke` | Stop + wipe volumes (destructive) |
| `just logs [service]` | Tail logs |
| `just migrate` | Run migrations to head |
| `just makemigration <name>` | Autogenerate a migration from model changes |
| `just shell` | Bash shell in the web container |
| `just psql` | psql against the running db |
| `just test-job` | Enqueue a no-op job |
| `just test` | Run pytest (auto-creates a `longboxes_test` database) |
| `just scan` | Enqueue an ad-hoc library scan |
| `just library-paths` | Print the configured library paths from `app_settings` |
| `just add-volume <cv_id>` | Fetch a ComicVine volume + stub its issues |
| `just match-all` | Enqueue a match job for every unmatched/pending file |
| `just create-user <name> <pass> [role]` | Create a user from the CLI |
| `just reset-password <name> <newpass>` | Rewrite an existing user's password |
| `just list-users` | List every user account (no hashes) — useful when recovering a lost username |
| `just lint` / `just fmt` | Ruff lint / format |

## Reading your library (Phase 6)

Any file with a readable archive gets a page-by-page web reader at
`/read/{file_id}` — reached from the "Read" buttons on issue pages and in
the review queue, or by clicking an issue cover. The reader is chrome-free
and owns the whole viewport: arrow keys, clicking the page, or the edge
buttons flip pages, and a thin toolbar carries close, a page counter,
fit-width/height, a left-to-right ↔ right-to-left toggle, and fullscreen
(Escape leaves fullscreen).

Reading direction is stored per volume — flip a manga series to RTL once
and every issue of it opens that way.

Reading progress is per user. The reader opens at your last-read page and
records your position as you go, marking an issue finished when you reach
the last page. The home page grows two sections from it — **Continue
reading** and **Recently read** — and a thin progress bar appears on issue
covers and across volume issue lists. Reset an issue's progress from its
page, or pause tracking entirely with the eye toggle in the header
("incognito reading") — existing progress is kept, just not added to.

The schema for all of this is migrations `0009`–`0011`; run `just migrate`
after pulling. See `design/phase-6-reader.md`.

## Next

Phases 8–10 are designed but not yet built — see `design/`:

- **Phase 8 — Metadata Sync** (`phase-8-metadata-sync.md`): keep each
  archive's embedded `ComicInfo.xml` reconciled against the ComicVine cache.
- **Phase 9 — AI Enrichment** (`phase-9-ai-enrichment.md`): an optional LLM
  layer over the keyword/count heuristics for volume classification.
- **Phase 10 — Cover Matching** (`phase-10-cover-matching.md`): bring cover
  art into the matcher via perceptual hashing.

## License

Longboxes is free software, licensed under the **GNU General Public
License, version 3.0** — see [`LICENSE`](LICENSE).

It depends on `comicfn2dict` (GPL-3.0) for filename parsing, which is
why Longboxes itself is GPL-3.0. Longboxes is distributed as a Docker
image that bundles its dependencies; every bundled package and its
license is recorded in [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).
