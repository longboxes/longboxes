# Demo Deployment Plan

Status: planned, not started. Companion to `worker-topology-plan.md`,
`hyperspeed-plan.md`, and `kapowarr-integration-api.md`.

A public, read-only demo of Longboxes at e.g. `demo.longboxes.app`.
Visitors land on a curated library and review queue, can browse every
interesting page (volume, issue, character, team, arc, review), can
use the reader on a small selection of public-domain content, and
cannot mutate anything. No ComicVine API key in the deployment. No
background work runs. Sealed snapshot; reproducible from a single
seed dump.

The reference model is Kavita's demo and Codex's demo: a working
instance you can poke at without setting anything up. The bar is "I
want to see what Longboxes looks like before I install it."

## Architecture

Seven decisions, all interlocking, none controversial:

### 1. `APP_ENV=demo` as the single switch

The existing `settings.app_env` already takes `"dev"` / `"production"`;
we add `"demo"` as a third value. Every demo-specific behaviour is
gated on this single string. The flag is the *only* thing the demo
container sets differently from a production deployment — meaning
the demo build can be the same Docker image; the env-var is what
diverges.

### 2. New `DEMO` role on the User model

Today `UserRole` (in `app/models/user.py`) has `ADMIN` and `VIEWER`.
We add `DEMO`. A demo user has read access to everything interesting
(library, review queue, all detail pages) and zero write access.
That's expressed via a single new dependency and a one-time route
audit — see §4 below.

The demo user is seeded at deploy time with a known UUID, a
non-functional password (auto-login bypasses it), and `role=DEMO`.

### 3. `require_writable` dependency to gate mutations

`app/auth/dependencies.py` gains a `require_writable(user)` dep
alongside `require_user` / `require_admin`. It returns 403 when
`user.role == UserRole.DEMO`. Every mutation route handler adds it
to its signature:

```python
@router.post("/{file_id}/confirm")
async def confirm(_: RequireWritableDep, ...):
    ...
```

A one-pass audit of `app/review/routes.py`, `app/admin/routes.py`,
`app/library_browse/routes.py`, `app/reader/routes.py` (for the
reading-progress writes), and `app/auth/routes.py` finds every POST
/ PUT / DELETE and adds the dep. About half a day of mechanical work
plus careful review.

Effect: a demo user clicking "Confirm" or any settings button gets
a 403. The button could be hidden in the UI for demo users to avoid
the dead-click feel — small Jinja conditional in the affected
templates. (Hiding is polish; the 403 is the real safety boundary.)

### 4. CV API client never called

`settings.cv_api_key` is left unset in the demo deployment. Every CV
call goes through `ComicVineClient`, which raises
`ComicVineKeyMissingError` on a missing key. Routes that would
normally fetch live data instead serve from the local cache (the
SWR layer's `is_fresh` check); searches see pre-populated rows in
`cv_search_cache` and never fall through to the client.

This is belt-and-suspenders with §3: even if a mutation slipped past
the audit and triggered a CV-touching path, the missing key would
stop the network call before it could damage anything. No demo
mode means no live CV traffic, period.

### 5. Mock CV search via pre-seeded `cv_search_cache`

The route the volume-search page hits (`/review/volume-search`) calls
`cache.search_volumes(query)`, which checks `cv_search_cache` first
and only falls back to the live client on a miss.

For the demo, we pre-populate `cv_search_cache` with the queries the
seed scenarios exercise — typically the parsed-series strings of
the files in the seed library. The cache key is a hash of
(endpoint, query, limit), so the seed script computes the same hash
the runtime would and inserts a row carrying a real CV envelope
shape. Hits return immediately; visitors searching for those terms
see proper results.

Searches *outside* the canned set hit the missing key and raise. In
demo mode, the route catches that and renders a small hint:

> "Demo mode — try one of these example searches: `batman`, `saga`,
> `inuyasha`."

About half a day of route-side handling plus seed-data work.

### 6. No worker, no scheduler

Demo mode runs no background jobs. The matcher doesn't fire (every
file is pre-matched in the seed); scans don't fire (the library is
fixed); revalidates don't fire (cache rows are pre-seeded fresh).
The `enqueue_*` helpers in `app/jobs/revalidate.py` and
`app/jobs/scrape.py` get a single early-return:

```python
if app_settings.app_env == "demo":
    return
```

That keeps the read-path code unchanged (it still *calls*
`enqueue_revalidate` from `_upsert_volume` etc.) but the calls
become no-ops in demo. The `worker` and `scheduler` services
drop out of the demo compose entirely.

Demo deploys then consist of **three containers**: `web`, `db`, and
`redis` (still needed for session cookies if the auth layer uses
Redis-backed sessions; if it's cookie-only, `redis` drops too and
demo is a two-container deploy).

### 7. Auto-login on first visit

A middleware in `app/main.py` (or a tweak to the existing session-
loading dep) creates a session for the demo user if no session
cookie is present. No login page, no signup wall, no creds.

```python
# Pseudocode for the middleware shape
@app.middleware("http")
async def auto_login_demo(request, call_next):
    if (
        settings.app_env == "demo"
        and "session" not in request.cookies
    ):
        # Set the session cookie targeting the demo user.
        ...
    return await call_next(request)
```

The login page can stay reachable on a hidden URL for admin
inspection; the navigation never points at it.

### 8. Reader included

The reader (`/read/<file_id>`) streams pages from the actual archive
files on disk. For the demo, the seed bundles a small selection of
**public-domain** comics (Digital Comic Museum) so the reader has
real content to serve. Roughly 10–20 issues, ~500MB–1GB on disk,
fits comfortably in a small VPS.

The `ReadProgress` writes (each page-flip stamps a row) need the
`require_writable` treatment — see §3. Demo users can read but not
save progress. The reader UI still tracks position client-side, so
within a session the experience is intact; reloads start at page 1.

## Branch strategy: hybrid (mostly main + a demo branch for content)

The question is where the demo bits live. Two reasonable shapes,
plus a third that takes the best of both.

**Pure-branch approach.** Demo code on a `demo` branch, rebased from
main periodically. Pure separation, but main / demo drift, and
every rebase has merge work whenever core auth / review / library
code shifts. The same files get touched in both branches
constantly.

**Pure-flag approach.** Everything in main, gated by
`settings.app_env == "demo"`. No drift, no merge pain, but demo
code lives in main forever and seed dumps bulk up the repo.

**Hybrid (recommended).** Split the two kinds of "demo stuff":

- **Gating logic lives in main.** The `app_env` flag, the `DEMO`
  role, `require_writable`, the demo-banner partial, the
  `enqueue_*` no-ops, the auto-login middleware, the mock-search
  fallback hint. These are clean, small additions that any operator
  could enable to run a read-only kiosk deployment. Total: tens of
  lines spread across a handful of files, all behind the env flag.

- **Demo-specific artifacts live on a `demo` branch.** The seed SQL
  dump (`demo_seed.sql`), the PD comic archive files, the
  `deploy/demo-compose.yml`, the `deploy/Caddyfile`, the seed
  helper script. *None* of these are referenced from main; they're
  pure additions. Rebasing the demo branch onto main is conflict-
  free because the demo branch only adds files, never modifies
  existing ones.

This split avoids both pain points: main isn't bloated with binary
seed dumps, and the demo branch never has to merge upstream code
changes. Operationally:

```
main:    code (with app_env=demo gating) ← all PRs target here
└── demo/main: seed dump + DCM archives + deploy compose
                ↑ rebased from main as new releases ship
```

A simple GH Action rebases `demo/main` onto `main` weekly; if the
rebase is clean (which it should be — additive only), it
auto-deploys to the demo host. If the rebase conflicts, the action
opens a PR for manual resolution.

## Implementation steps

### Step 1 — `app_env="demo"` accepted in `Settings`

`app/config.py`: extend the validator on `app_env` to accept
`"demo"` alongside `"dev"` / `"production"`. No new field; just an
allowed value.

### Step 2 — `DEMO` role on `UserRole`

`app/models/user.py`: add `DEMO = "demo"` to the `UserRole`
`StrEnum`. No migration needed — `role` is a free-text `String`
column, so any new value just slots in.

### Step 3 — `require_writable` dependency

`app/auth/dependencies.py`: a new dep that returns 403 when
`user.role == UserRole.DEMO`. Plus a sibling
`RequireWritableDep = Annotated[User, Depends(require_writable)]`
to import.

### Step 4 — Audit mutation routes; add `require_writable`

Pass through every router; replace `RequireAdminDep` /
`RequireUserDep` with `RequireWritableDep` on every POST/PUT/DELETE
handler. Routers in scope:

- `app/admin/routes.py` — every settings / rescan / match-all route.
- `app/review/routes.py` — confirm / reject / bulk-confirm /
  volume-confirm / local-group / supplement / hydrate-issues POSTs.
- `app/library_browse/routes.py` — `/volume/{id}/hydrate-issues`
  POST (already idempotent; in demo this still wants gating because
  it would otherwise enqueue jobs the worker never processes).
- `app/reader/routes.py` — `ReadProgress` POSTs, direction toggle,
  reset-progress.
- `app/auth/routes.py` — change-password etc.

In templates, hide the affected buttons when
`user.role == "demo"` so the UI doesn't lead the visitor toward
dead-end 403s. Two-line Jinja conditional per button cluster.

### Step 5 — Mock CV search

`app/review/routes.py` and any other route handler that calls
`cache.search_volumes` / `cache.search`: in demo mode, wrap the call
in a try/except. On `ComicVineKeyMissingError`, return the page with
a `demo_search_hint` flag set, and template renders the canned-query
suggestion.

The actual cached results don't need any code change — they live in
`cv_search_cache` and the existing cache layer reads them
transparently.

### Step 6 — Auto-login middleware

`app/main.py`: a middleware that runs before the session-loader.
When `settings.app_env == "demo"` and the request has no session
cookie, set one for the demo user (read from a config or known
UUID). Existing session paths handle the rest.

The demo user is created at startup (or seeded — see Step 9).

### Step 7 — Banner partial

`app/templates/_demo_banner.html`: small Jinja macro rendering a
slim slate banner across the top of every page when
`app_env == "demo"`. Text along the lines of:

> Public demo · read-only · seeded with public-domain titles
> · [source on GitHub]

Include it in `base.html` (or the existing header partial) gated on
`app_env`.

### Step 8 — No-op enqueue paths in demo mode

`app/jobs/revalidate.py::enqueue_revalidate` and the scrape-enqueue
helpers in `app/jobs/scrape.py`: early `return` when
`app_settings.app_env == "demo"`. Belt-and-suspenders alongside
the missing CV key — even if a route handler somehow triggers an
enqueue, the call no-ops cleanly.

### Step 9 — Seed script

A Python helper, run **once** against a dev environment that has a
real CV API key, that:

1. Walks a curated set of `.cbz` / `.cbr` files (the PD library).
2. Runs the matcher against them to produce
   `files` / `file_locations` / `file_matches` rows.
3. Fetches the relevant CV entities (volumes, issues, publishers,
   characters, arcs, teams) so the demo's `cv_*` tables are
   pre-populated.
4. Computes the search-cache hashes for the canned demo queries
   and writes the corresponding `cv_search_cache` rows.
5. Creates the demo user (UUID, role=DEMO, password=`*`).
6. Dumps the result to `demo/seed.sql` (or a binary
   `pg_dump`-style file) committed on the demo branch.

Lives under `app/scripts/seed_demo.py`. Read-only against an
existing dev DB; produces a single deterministic dump.

### Step 10 — Slimmed compose + Caddyfile

On the demo branch under `deploy/demo/`:

```yaml
# deploy/demo/docker-compose.yml — three services.
services:
  web:
    image: ghcr.io/longboxes/longboxes:demo  # tagged demo build
    environment:
      APP_ENV: demo
      DATABASE_URL: postgresql+asyncpg://demo:demo@db:5432/demo
      REDIS_URL: redis://redis:6379/0
      LIBRARY_PATHS: /library
    volumes:
      - ./content:/library:ro

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: demo
      POSTGRES_PASSWORD: demo
      POSTGRES_DB: demo
    volumes:
      - ./db-init/seed.sql:/docker-entrypoint-initdb.d/seed.sql:ro
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
```

Caddyfile handles TLS automatically; an A record on
`demo.longboxes.app` points at the VPS. Cloudflare in front handles
DDoS + IP rate-limiting on the free tier.

## Seed content

Curated to exercise the demo's interesting surfaces. Two priorities:

- **Library** large enough to feel real (~20–30 volumes, 100–150
  issues) but small enough to host on a $5/mo VPS (~500MB on disk
  if using DCM source files).
- **Review queue** that visibly demonstrates the matcher's value:
  a few clean groups ready to confirm, a couple with the
  year-disambiguation showing off (Batman-style scenario), a
  deliberately misnamed file or two routed through the per-file
  picker, the local-volume escape hatch demonstrated on a "fake"
  obscure title that has no CV record.

**Source: Digital Comic Museum**
(<https://digitalcomicmuseum.com>) — public-domain Golden Age
comics, free to download and redistribute, all on ComicVine with
real volume / issue records. The library can include classic stuff
like:

- *Marvel Mystery Comics* (1939) — Marvel/Timely Golden Age, exists
  on CV.
- *Captain America Comics* (1941) — long run, good for the volume
  page demo.
- *Black Cat Comics* (1946) — Harvey, good variety.
- Several mid-tier publishers represented so the publisher filter
  has something to show.

All PD, all CV-catalogued, all legally redistributable.

## What does NOT change

The matcher, the scanner, the rate-pacer, the cache layer, the
review service, the library service, the reader service, the
RQ-job code paths. Nothing about the runtime logic. Demo mode is
purely a deployment configuration: a env-var flag, a role, a
write-guard dep, and a missing CV key. The same Docker image runs
prod and demo with different env vars.

## Open questions

1. **Hidden login page.** Should `/login` stay reachable in demo
   mode for admin access (so we can poke at the running demo
   instance via a real admin login)? If yes, the auto-login
   middleware needs to skip when the session already exists, and
   the demo-banner needs to know whether the current user is the
   demo user or a real admin. If no, admin debugging on the demo
   host happens via SSH-and-psql only.

2. **DCM archive bundling.** The PD comics ship inside the demo
   container image, or get downloaded on first boot from a
   permanent URL? Bundling is simpler; the image gets big (~1GB).
   Download-on-boot keeps the image small but adds a deploy step.

3. **Read-progress persistence within a session.** Demo users
   can't write `ReadProgress` (Step 4 blocks it). The reader UI
   does client-side tracking too; that means within a single
   session the visitor's place is remembered, but a reload resets.
   Acceptable? Or shim a client-side `localStorage` fallback for
   demo mode so reloads remember within the browser?

4. **Cloudflare in front.** Recommended for DDoS + IP rate
   limiting, but adds DNS + a config step. Skip for the initial
   launch and add if the demo gets popular?

## Files this would touch

**On main (gating logic):**

- `app/config.py` — accept `app_env="demo"`.
- `app/models/user.py` — `DEMO` role.
- `app/auth/dependencies.py` — `require_writable`, `RequireWritableDep`.
- `app/admin/routes.py`, `app/review/routes.py`,
  `app/library_browse/routes.py`, `app/reader/routes.py`,
  `app/auth/routes.py` — swap deps on every mutation route.
- `app/jobs/revalidate.py`, `app/jobs/scrape.py` — early-return in
  demo mode.
- `app/main.py` — auto-login middleware.
- `app/templates/_demo_banner.html` — new partial.
- `app/templates/base.html` — include the banner gated on `app_env`.
- Various templates — hide write-action buttons when
  `user.role == "demo"`.

**On the `demo` branch (content + deploy artifacts):**

- `app/scripts/seed_demo.py` — the seed-builder script.
- `deploy/demo/docker-compose.yml`
- `deploy/demo/Caddyfile`
- `deploy/demo/db-init/seed.sql`
- `deploy/demo/content/` — PD comic archives.
- `deploy/demo/README.md` — operator notes.

## Estimated effort

| | effort |
|---|---|
| Steps 1–4 (flag, role, dep, mutation-route audit) | ~a day |
| Step 5 (mock search route handling) | ~half a day |
| Steps 6–8 (auto-login, banner, enqueue no-ops) | ~half a day |
| Step 9 (seed script + curated content + DB snapshot) | ~a day |
| Step 10 (compose, Caddyfile, VPS deploy) | ~half a day |
| Branch wiring + rebase GH Action | ~half a day |

**~3.5 days of focused work**, plus ongoing ~$5–10/mo hosting.

## Rollout

1. Land all main-side changes (Steps 1–8) behind the
   `app_env=demo` flag. Production deploys are unaffected — the
   flag stays at `"production"`.
2. Spin up the `demo` branch with content + deploy artifacts.
3. Build the seed dump against a dev environment with a real CV
   key; commit the dump.
4. Deploy to the VPS (or Fly.io app). Verify Cloudflare / DNS.
5. Add a "Try the live demo" link to the GitHub README.

Once live, the demo is essentially self-maintaining: the weekly
rebase action picks up the latest main, the seed dump doesn't
change unless you intentionally regenerate it, and nothing about
the running instance writes back to anywhere.
