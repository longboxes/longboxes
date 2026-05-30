---
title: First scan
description: What to expect when Longboxes meets your library for the first time.
---

The first time Longboxes sees your library is the longest single
operation it ever runs. This page sets expectations and explains the
moving parts so you know what's happening.

## What a "scan" actually does

A scan is a walk of your library folders that opens each archive,
reads its first page (the cover), notes its page count, looks for an
embedded `ComicInfo.xml`, and writes a row in the database. It does
**not** match files to ComicVine — that's the matcher's job, kicked
off automatically once each file is registered.

On a fast disk a scan averages **20–40 files per second**. A 10,000-file
library finishes scanning in roughly five minutes.

You'll see the scan progress on `/admin/health`:

- **Total files in library** counts up as the walk discovers them.
- **Cover extracted** and **ComicInfo parsed** count alongside.
- **File errors** flags archives that wouldn't open or whose
  ComicInfo was malformed — these go to `/admin/file-errors` for
  later inspection.

## Then the matcher takes over

As each file lands, a `match_file` job is enqueued. The matcher:

1. **Looks for a ComicInfo CV ID first.** If the archive carries
   `<Web>...comicvine.gamespot.com/.../4000-12345/...</Web>`, that's
   the answer — the matcher confirms the issue from the local cache
   and writes the result. This is the fast path: tens of files per
   minute, no network call per file once the cache warms.
2. **Falls back to filename parsing.** No CV ID? Parse the filename
   for series / volume year / issue number, search ComicVine, score
   up to five candidate volumes, and pick the best. This is the slow
   path — roughly one CV call per stage, paced at 195 calls/hour to
   stay under ComicVine's documented 200/hour cap.

The matcher splits its result into three buckets:

- **AUTO** — high confidence, goes straight into your library. Shows
  up on `/library` and the issue's volume / arc / character pages.
- **PENDING** — a real candidate volume was found, but the matcher
  isn't confident enough to pick on its own. Lands in `/review`.
- **UNMATCHED** — no candidate at all (often because the series
  isn't on ComicVine, or the filename is too mangled to search on).
  Also visits `/review` so you can route it.

## Time estimates

The fast path is fast; the slow path is bounded by ComicVine's rate
limit. So how long matching takes depends entirely on how many of your
files carry a ComicInfo CV ID:

| Library mix | Approx. match time for 10,000 files |
| --- | --- |
| Mostly tagged (≥80% have ComicInfo CV ID) | 4–8 hours |
| Mixed (~50/50) | 12–24 hours |
| Mostly untagged | 24–48 hours |

Once the matcher catches up, the recurring scan keeps things current —
new files get scanned and matched within a few minutes of landing on
disk.

## What to do while it runs

You can use Longboxes immediately. Confirmed matches appear as they
land, so by the time you finish this page the library is probably
already populating.

A productive thing to do while the slow path grinds is to **work the
review queue**. The matcher pushes uncertain matches to `/review`,
grouped by series so you confirm a whole run in one go. Each group
shows you the candidate volume and lets you accept (the matcher's
pick was right), pick a different volume (fix-match), or hand-route
the file as a **local volume** (it isn't on ComicVine) or a
**supplement** (it's a cover gallery for a real series).

The review queue isn't a chore to clear before launch — it's the
normal way the long tail of a library gets cleaned up.

## When things look stuck

The job-status widget on `/admin/health` is the source of truth:

- **Queued** is the backlog (jobs waiting for a worker).
- **Running** is what's actually in progress right now.
- **Scheduled** is for jobs paused by the rate limiter — they'll
  resume automatically.

A growing **Scheduled** number during the matcher run is normal — it
means the rate limiter is doing its job. As long as **Running** is
not zero, work is progressing.

If **Running** is zero and **Queued** isn't, the worker container
crashed. `docker compose logs worker` tells you why; `docker compose
restart worker` usually fixes it.

See **[Troubleshooting](/troubleshooting/)** for the specific
failure modes.

## Next

- **[How matching works](/matching/)** is the conceptual page —
  what volume-first matching means, why it gets popular titles right,
  and how to use the review queue to handle the rest.
