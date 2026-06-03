---
title: Install
description: The full install path with the explanations.
---

This is the Quick start with the *why* spelled out. If something here
looks unfamiliar, this is the page that explains it.

## Prerequisites

- **Docker Engine 20+** with Compose v2. Anywhere you can run a
  modern Docker stack — Linux VM, macOS, Windows with WSL2, a NAS
  with container support — works.
- **About 1 GB of RAM free** for Longboxes itself. Your library on
  disk stays where it is; Postgres adds a few hundred MB for cached
  ComicVine metadata.
- **A folder of comic archives.** `.cbz`, `.cbr`, `.cb7`, and `.pdf`
  are all supported. Subfolders are walked recursively.
- **A free [ComicVine API key](https://comicvine.gamespot.com/api/).**
  Required for the matcher. Without one, Longboxes will scan your
  files but won't be able to build the library.

## The stack

Longboxes runs as seven services in a single `docker-compose.yml`:

| Service | What it does |
| --- | --- |
| `web` | The FastAPI app — every page you see in the browser. |
| `worker` | Match jobs. Heavy lane; one process, drains the backlog. |
| `worker-interactive` | Browse-triggered hydration on its own lane so browsing stays snappy during a big match run. |
| `worker-scan` | The recurring library scan, isolated so a long walk can't park hydration jobs behind it. |
| `scheduler` | Fires the rescan on its cron. |
| `db` | Postgres 16 — your library data + ComicVine cache. |
| `redis` | RQ job queue + the rate-limiter state. |

The "compose file" is the unit of install — `docker compose up` brings
them all up together. There's no "install Postgres separately" or
"add Redis later" step.

## Step 1 — Get the compose file

```bash
mkdir longboxes && cd longboxes
curl -O https://raw.githubusercontent.com/longboxes/longboxes/main/deploy/docker-compose.yml
```

This pulls the canonical compose from `deploy/`. It uses the published
GHCR images (`ghcr.io/longboxes/longboxes:latest`), so you don't
need to clone the source.

## Step 2 — Set environment variables

Create a `.env` next to the compose file. The minimum:

```bash
# Where your library lives on the host. The compose file
# bind-mounts this folder read-only into the container at
# /library, where the scanner walks it. Use an absolute path.
LIBRARY_PATH=/path/to/your/comics

# Postgres password. Change this. Don't reuse a password you use
# elsewhere — it's only ever read by the local DB container, but
# it's still good hygiene.
POSTGRES_PASSWORD=change-me

# How many match worker processes to run in parallel. 1 is fine;
# 2-3 helps on a fast machine; more than 3 thrashes the rate limit.
MATCH_WORKER_REPLICAS=2
```

Optional:

```bash
# Host port the web service binds. Defaults to 8612. Change to
# anything you like — e.g. WEB_PORT=616 if you want Earth-616 on
# the URL bar. Ports below 1024 ("privileged") work fine with
# Docker's default daemon, since the host bind happens as root;
# only rootless Docker on Linux needs an extra sysctl tweak.
WEB_PORT=8612

# Public URL Longboxes will see itself at. Only matters if you put
# a reverse proxy in front and want links in emails / sharing to
# use the public hostname instead of localhost.
PUBLIC_URL=https://longboxes.example.com

# Override the default page size (rows per page in library / review).
PAGE_SIZE=50

# Tighten the CV rate cap if you find Longboxes pacing too aggressively
# (default 195/hr — under CV's documented 200).
CV_RATE_PER_HOUR=195
```

## Step 3 — Point at your library

For a **single library folder**, you're done after Step 2 — the
compose file's volume mount reads `${LIBRARY_PATH}` from `.env` and
bind-mounts it read-only into the container at `/library`. No YAML
editing required.

The `:ro` flag on the mount is important — Longboxes mounts your
library **read-only**. It never writes to your archive files.
(Future "metadata sync" support will need write access, but
that's an explicit opt-in when it lands.)

### Multiple library folders

If your library spans multiple folders, the simplest approach is to
point `LIBRARY_PATH` at a parent that contains all of them — the
scanner walks subdirectories recursively, so one mount handles any
nested layout.

If that's not workable (e.g. the folders live on different volumes),
edit `docker-compose.yml` directly to add the extra mounts and
override the in-container scanner paths:

```yaml
services:
  web:
    volumes:
      - /mnt/marvel:/library/marvel:ro
      - /mnt/dc:/library/dc:ro
      - /mnt/indie:/library/indie:ro
    environment:
      LIBRARY_PATHS: /library/marvel,/library/dc,/library/indie
```

Apply the same mounts to the `worker`, `worker-interactive`, and
`worker-scan` services so background jobs see the same files the
`web` container does.

## Step 4 — Bring the stack up

```bash
docker compose up -d
docker compose logs -f web
```

The first boot runs database migrations and seeds the admin user table.
You'll see lines like `Applied migration 0016` followed by
`Uvicorn running on http://0.0.0.0:8080` *(inside the container)*.
That's the cue to open the browser at `http://localhost:8612`.

## Step 5 — First login

Visit `http://localhost:8612` (or wherever your `PUBLIC_URL` points).
The host port is configurable via `WEB_PORT` in `.env` (default
`8612`) — the container listens on `8080` internally.
You'll be walked through:

1. **Create the admin user.** First account, full admin rights. You
   can create read-only accounts later from `/admin/users`.
2. **Paste your ComicVine API key.** Set under
   `/admin/settings`. Test it with the button — Longboxes makes one
   call to confirm the key is valid before writing it.
3. **Confirm the library paths.** The admin UI shows the
   *in-container* path (`/library` by default) — that's the
   mount point your host `LIBRARY_PATH` is bind-mounted to. The
   seed step writes it to `app_settings` so it survives container
   restarts.

Hit **Start scan** on the admin page. The first scan and match
backlog will run in the background — you can browse what's there
while it works. See **[First scan](/first-scan/)** for what to
expect.

## Upgrading

Pull the latest image and recreate:

```bash
docker compose pull
docker compose up -d
```

Migrations run automatically. Your library data lives in the
`db` volume; nothing in the upgrade touches your archive files.

### Auto-updates with Watchtower

[Watchtower](https://containrrr.dev/watchtower/) works out of the
box. The image is public on GHCR, `:latest` is the floating tag
Watchtower watches, and migrations run on the web container's
startup so its "pull + restart" cycle is enough to apply them.

For a pre-1.0 app like Longboxes, **opt in deliberately rather than
auto-updating the whole stack.** A breaking migration in `main`
could ship overnight; with Watchtower running unattended you wake
up to a broken stack and no recent backup. Two patterns work:

- **Watch only the Longboxes services**, via per-service labels:

  ```yaml
  services:
    web:
      labels:
        com.centurylinklabs.watchtower.enable: "true"
    worker:
      labels:
        com.centurylinklabs.watchtower.enable: "true"
    # ...etc on worker-interactive, worker-scan, and scheduler
  ```

  Then run Watchtower with `--label-enable` so it ignores anything
  without that label.

- **Watch + notify-only**, with `--monitor-only`. Watchtower checks
  for updates and pings you (email, Discord, ntfy, …) but doesn't
  apply them. You decide when to upgrade.

Once Longboxes hits a stable release line, full unattended
auto-updates become a safer default.

## Behind a reverse proxy

Longboxes serves plain HTTP on the host port set via `WEB_PORT`
(default `8612`). Put your favourite proxy (Caddy, nginx, Traefik, …)
in front to add TLS. The app reads `X-Forwarded-Proto` and
`X-Forwarded-Host` so links generated inside the app respect your
public URL — set `PUBLIC_URL` in `.env` to make the canonical link the
public one.

A minimal Caddyfile:

```caddyfile
longboxes.example.com {
  reverse_proxy localhost:8612
}
```
