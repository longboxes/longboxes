---
title: Troubleshooting & FAQ
description: Common issues and questions, with the actual fix.
---

## Troubleshooting

### "The match queue isn't draining"

Check `/admin/health` → Background jobs. Three rows tell you what's
actually happening:

- **Queued** is jobs waiting for a worker.
- **Running** is jobs in progress right now.
- **Scheduled** is jobs paused by the rate limiter (they'll resume
  when the cooldown lifts — usually 15 minutes).

A growing **Scheduled** number during a big match run is **normal** —
the rate limiter is doing its job. As long as **Running** is non-zero
or jobs are cycling through Scheduled, work is progressing.

If **Running is zero** and **Queued is non-zero** and **Scheduled is
non-zero**, the worker container probably crashed:

```bash
docker compose logs worker --tail 100
docker compose restart worker
```

### "The matcher keeps hitting rate limits"

If your library is huge and you're impatient: lower
`MATCH_WORKER_REPLICAS` to 1 or 2. Multiple match workers share the
same ComicVine rate budget, so adding more workers doesn't speed
things up — it just causes more rate-limit thrashing.

The pacer defaults to 195 calls/hour (under ComicVine's 200). If you
have a higher rate limit on your key (corporate / partner keys
sometimes get more), bump `CV_RATE_PER_HOUR` in `.env`.

### "Confirmed matches aren't showing up in /library"

Two things to check:

1. **Was the match actually confirmed?** Visit `/admin/health` and
   look at the match-status breakdown. The counts there are the
   source of truth.
2. **Are you on the right user?** Reading progress and tracking
   settings are per-user, but the library is shared. If `/library`
   shows nothing, it's not a user issue — check the match counts.

### "Wonder Woman 2011 (or whatever) matched to the wrong volume"

This used to happen on popular long-running titles. Recent matcher
work fixes it: the search endpoint switched from `/volumes/?filter=`
(which ranks by volume id) to `/search/?resources=volume` (which
ranks by relevance), and the candidate window is wider. Re-match the
affected files via `/admin` → **Match all** if you're on an older
build.

If a specific group is still wrong, use **Fix match** on the volume
page to rebind to the correct one. The matcher won't re-try a
human-confirmed match, so the fix sticks.

### "/admin/file-errors is full of archives that won't open"

These are real archive errors from the scanner — corrupt CBR
trailers, password-protected ZIPs, PDF parse failures. The page
groups them by error class with sample paths.

For each class:

- **CBR with unrar-free issues** — convert the offending files to
  CBZ. `unrar` non-free works around most of these but isn't
  distributable; the bundled `unrar-free` covers the common cases.
- **ComicInfo malformed** — Longboxes treats it as "no ComicInfo"
  and matches via filename instead. Not a blocker.
- **PDF parse errors** — usually a malformed PDF; some scanners
  produce these.

Errors are persistent — if you fix the file on disk, the next scan
clears them. Or click **Re-queue** to retry the scan for that file
without waiting for the recurring rescan.

### "Queue is stuck on a single job"

A bad input occasionally hangs a job. `docker compose logs worker
--tail 200` shows what it's working on. Often the fix is just
`docker compose restart worker` — RQ rolls the in-flight job back to
queued and the next worker picks it up cleanly.

### "Covers aren't loading on the volume page"

The volume page lazy-loads issue covers as their `cv_issues` rows
hydrate. If you opened the page right after a match, some are still
in flight — the page auto-refreshes affected rows as covers land
(watch the small "covers hydrating" toast in the corner).

If covers stay missing after the toast clears: the issue's CV cover
URL might be a broken image on CV's side. Click through to the issue
page; if the CV link there is dead too, that's a CV catalogue
problem.

### "I deleted a file but it's still in the library"

The scanner runs on a recurring schedule (default: hourly). Wait for
the next scan, or trigger one manually via `/admin` → **Rescan now**.
The scanner notices missing files and clears their rows.

### "I want to nuke everything and start over"

```bash
docker compose down -v
```

The `-v` removes the data volume. Your library files are untouched
(read-only mount); only Longboxes' database state is wiped.
`docker compose up -d` brings back a fresh setup wizard.

## FAQ

### Is Longboxes a Komga / Kavita replacement?

Same audience, different shape.

Komga and Kavita are file-organised libraries — they read a folder
structure and show you what's in it. They work with anything you
have, but the navigation is fundamentally folder → file.

Longboxes is story-organised. Files match into ComicVine's relational
graph (volumes → arcs → characters → creators → teams) and you
browse that graph. It works *best* when your files match a
catalogue ComicVine knows about, and it has the local-volume escape
hatch for when they don't.

You can run both side by side if you want — Longboxes mounts your
library read-only, so it doesn't conflict with whatever else reads
from the same folder.

### Does it support OPDS?

Not yet. The web reader is browser-only today. OPDS for mobile
reader apps (Tachiyomi, Mihon, Panels, Chunky, KOReader) is on the
roadmap.

### Does it need ComicTagger / tagging upfront?

No. Longboxes scans whatever you have. A tagged library
(ComicInfo.xml with a CV ID) matches significantly faster — the
matcher takes the fast path and finishes the run in hours instead of
days — but an untagged library works too; it just spends more time
on the ComicVine API.

### Will it write tags back to my archives?

Not yet. Metadata-sync — read the desired metadata from a confirmed
CV match, diff against the embedded `ComicInfo.xml`, write the
delta — is on the roadmap as **Phase 8**. It will be opt-in (default
off; never silent), atomic (temp-file + verify + rename), and
respect a "managed fields" boundary so hand-edited fields are left
alone.

Until it lands, your archive files are byte-identical to how you
put them on disk.

### Does it work with manga?

Yes. Reading direction is per-volume and persists — flip a Japanese
series to RTL once and every issue in it remembers. The matcher
treats Japanese publishers (Shogakukan, Kodansha, Shueisha,
Tokyopop, VIZ, etc.) like any other publisher; CV's catalog for
mainstream manga is broad.

The known limitation: manga *omnibuses* (one collected volume
reprinting three tankobon volumes) match imperfectly today because
ComicVine doesn't natively model "this collected volume reprints
these original volumes." Phase 9 (AI enrichment, roadmap) addresses
this by extracting the contents structure from collected-edition
descriptions.

### Can multiple users share an instance?

Yes. Create accounts from `/admin/users`. Library content is shared
across users (one set of files, one set of matches), but **reading
progress, tracking settings, and incognito mode are per-user**.

### How much disk / RAM does Longboxes use?

The app itself is small: about 1 GB RAM under load, 200 MB at idle.

The database grows with your library — roughly 50 KB per matched
issue (ComicVine metadata + raw payload cache), so a 10,000-issue
library is around 500 MB of Postgres data. Plus a few hundred MB of
characters / creators / arcs / publishers that get hydrated as you
browse.

Your library files (your actual archives) are mounted read-only and
take whatever space they take — Longboxes doesn't copy them.

### Is the ComicVine API key safe?

It's stored encrypted at rest in the `app_settings` table, written
to the local Postgres. Longboxes uses it only to make calls to
ComicVine's documented API. There's no telemetry, no proxy, no
phone-home.

If you're concerned, ComicVine keys are free and revocable — you can
regenerate a new one from your CV account page anytime.

### Will my library scale to N issues?

Tested up to ~22,000 files. The DB queries are paginated and the
matcher runs incrementally, so there's no hard ceiling — but the
initial match still has to pay ComicVine's rate limit for any file
without a ComicInfo CV ID. A 50k-file untagged library will take a
few days to match; the same library tagged matches in well under
a day.

### What's the license?

GPL-3.0. The full text is in [LICENSE](https://github.com/longboxes/longboxes/blob/main/LICENSE).
Bundled third-party dependencies are listed with their own licenses
in
[THIRD-PARTY-NOTICES.md](https://github.com/longboxes/longboxes/blob/main/THIRD-PARTY-NOTICES.md).
