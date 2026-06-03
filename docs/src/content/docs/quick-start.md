---
title: Quick start
description: Get Longboxes running in about five minutes.
---

You need: a machine with Docker, a folder of `.cbz` / `.cbr` / `.cb7` /
`.pdf` files, and a free ComicVine API key.

## 1. Get a ComicVine key

Sign up (or log in) at [comicvine.gamespot.com](https://comicvine.gamespot.com/),
then visit your **[API page](https://comicvine.gamespot.com/api/)** and copy
the key. It's a long hex string. You'll paste it into Longboxes after the
first boot.

## 2. Pull the stack

Create a folder for Longboxes, drop in a `docker-compose.yml` and a `.env`,
then start it:

```bash
mkdir longboxes && cd longboxes
# Grab the latest compose file:
curl -O https://raw.githubusercontent.com/longboxes/longboxes/main/deploy/docker-compose.yml
# Minimal env — set LIBRARY_PATH to the folder on your machine
# holding the .cbz / .cbr files. The compose file bind-mounts it
# read-only into the container at /library, so Longboxes never
# writes to your archives.
echo 'LIBRARY_PATH=/path/to/your/comics' >> .env
echo 'POSTGRES_PASSWORD=change-me' >> .env

docker compose up -d
```

## 3. First boot

Visit [http://localhost:8612](http://localhost:8612). On first boot
Longboxes shows a setup wizard:

1. **Create the admin user.** Pick a username and password — this is
   your local account; nothing leaves your server.
2. **Paste the ComicVine API key.** Required for matching; without it
   you can still browse and read, but the library will stay unmatched.
3. **Confirm the library paths.** Should already show your mounted
   folder. If not, add it here.

## 4. Wait

Longboxes scans the library, then runs the matcher against every file
it found. **A library of ten thousand issues takes overnight to fully
match** — the matcher paces itself to stay under ComicVine's 200-calls
/hour rate limit. While it runs, the **Library** and **Story arcs**
pages fill in as matches land.

For everything else — what the review queue is, what AUTO vs PENDING
mean, what to do when a match looks wrong — see **[First scan](/first-scan/)**.

## What's running

`docker compose ps` shows seven services:

- **web** — the Longboxes app (FastAPI).
- **worker** — the match lane. Heavy; drains the file-matching backlog.
- **worker-interactive** — browse-triggered hydration on its own lane
  so the UI stays snappy during a big match run.
- **worker-scan** — the recurring library scan, isolated so a long
  walk can't park hydration jobs behind it.
- **scheduler** — fires the recurring rescan.
- **db** — Postgres.
- **redis** — job queue + rate limiter state.

All of it is one `docker compose down` away. Your library files are
untouched — Longboxes only ever reads them.
