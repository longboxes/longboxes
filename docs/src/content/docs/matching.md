---
title: How matching works
description: The model that makes Longboxes feel like a library instead of a file browser.
---

The matcher is the heart of Longboxes. Understanding what it's doing
makes everything else — the review queue, the duplicate detector, the
character pages — read as obvious instead of mysterious.

## Volume first, issue second

Most comic matchers are issue-first: take a filename, search for that
exact issue, pick whatever comes back. That works for clean filenames
and produces "Spider-Man #1" matched to whichever Spider-Man #1
ComicVine returns first — often the wrong one.

Longboxes flips the order. The matcher finds the **volume** first
(which series, which year of which series), then resolves the issue
inside it:

1. Parse the filename → series name + (often) volume year +
   issue number.
2. Search ComicVine for candidate **volumes** matching the series.
3. Score each candidate using volume year, publisher (from the path),
   issue count, and a year-proximity tiebreaker.
4. Pick the volume.
5. *Then* look up the issue inside the picked volume's issue list.

Why this matters: there are five different "Wonder Woman" volumes on
ComicVine — 1942, 1987, 2006, 2011, 2016. A folder of
`Wonder Woman 003 (2012).cbz`-style files is unambiguously the 2011
New 52 run, but an issue-first matcher will happily pick the 1987
run because that's what its results page shows first. Volume-first
gets it right because year-of-volume is a stronger signal than
year-of-issue when scoring candidates.

## Four stages

The matcher pipeline has four stages, in order of cost. Each stage
short-circuits if it succeeds, so common cases never pay the
expensive paths.

### Stage 1 — ComicInfo CV ID

If the archive carries an explicit ComicVine issue ID in its
`ComicInfo.xml` (the `<Web>` field), use it. This is a database
lookup in the local ComicVine cache — no network call, sub-second.
Confidence: AUTO.

Tagged libraries (ComicTagger, Mylar, etc.) hit this path for the
vast majority of files. The fastest possible match.

### Stage 2 — Parsed filename + volume year + issue number

Filename parser pulls out series + volume year + issue number. The
matcher searches ComicVine's `/search/?resources=volume` endpoint
for the series, applies a year-proximity filter, picks the highest-
scoring candidate, and resolves the issue.

Most untagged but reasonably-named files match cleanly here.
Confidence: usually AUTO, sometimes PENDING for popular titles with
multiple plausible candidates.

### Stage 3 — Long series name + path-year fallback

Some scanlator naming conventions cram a lot into the filename ("The
Amazing Spider-Man Vol 5 (2018-) 023 (digital).cbz"). The long-form
series parser handles these. If the filename is missing a year, the
matcher also tries the parent folder name (`/library/Spider-Man (2018)/`
is a strong year hint).

### Stage 4 — Scoring tiebreakers

When multiple candidate volumes score similarly, the tiebreakers fire
in this order: volume-year proximity → issue-cover-date proximity →
publisher match (from path) → issue count plausibility. The first
decisive signal wins.

If nothing's decisive after all that, the file goes to **PENDING**
with all candidates shown — better to defer to a human than to guess.

## The review queue

`/review` is where PENDING and UNMATCHED files live. It's grouped by
series + volume year so you confirm a whole run in one action,
not one file at a time.

A typical review group looks like:

> **Wonder Woman (2011)** — 47 files
>
> Candidate volume: *Wonder Woman* (2011, DC Comics, 52 issues) ✓
>
> [Confirm group] [Pick a different volume] [Search ComicVine]

Confirming the group resolves every file to its corresponding issue
in the picked volume. The issue numbers come from the filenames; the
issue *identity* (the CV `cv_id`) comes from the picked volume's
issue list.

### When the candidate is wrong

**Pick a different volume** opens the same review search you'd use
from the per-file flow. Type a query, the matcher's volume-search
runs against ComicVine, and you pick from the result cards. The
group reassigns to the new volume.

### When ComicVine doesn't have the book

For small-press, self-published, or convention-exclusive content,
ComicVine often doesn't have a record. The review queue offers
**Create as local volume** as the escape hatch:

- A `local_volumes` row is created with the series name, volume
  year, and publisher you supply (hand-typed; nothing comes from CV).
- Each file in the group gets a `local_issues` row with its issue
  number.
- The volume appears on `/library` next to your CV-matched volumes,
  flagged as **Local** in the corner.

Local volumes have their own URL space (`/local/volume/{uuid}`)
and behave the same as CV volumes for browsing and reading — they're
just metadata islands (no character / arc pages, no credits, because
those are CV-keyed).

### When the file is a cover gallery, not an issue

A "Spider-Man Cover Gallery.cbz" sitting next to the Spider-Man run is
real library content but isn't a numbered issue. The review queue's
**Attach as supplement** action handles this:

- Find the right volume in the picker (same flow as fix-match).
- Pick a supplement type (currently: `cover_gallery`).
- The file attaches to that volume's "Supplements" section.

Supplements show on the volume page but don't count toward owned
issues or the completion ring — they're extras.

## Status states

Every file has a `MatchStatus`:

| Status | What it means |
| --- | --- |
| `AUTO` | Matched, high confidence. Lives in `/library`. |
| `PENDING` | Has candidate(s), waiting on you to confirm or fix. |
| `UNMATCHED` | No candidate — nothing to do until you route it. |
| `CONFIRMED` | You explicitly accepted the match. |
| `REJECTED` | You explicitly rejected; matcher won't retry. |
| `LOCAL` | Hand-catalogued; resolves to a local volume/issue. |
| `SUPPLEMENT` | Attached to a CV volume as extra content. |
| `EXCLUDED` | "Not a comic" — won't be matched or shown. |

`AUTO`, `CONFIRMED`, `LOCAL`, and `SUPPLEMENT` are all *resolved*
states — the file lives in your library and the matcher won't
overwrite them on a re-run.

## The character / creator graph

Once a file is matched, it joins ComicVine's relational graph
automatically. The volume's characters, creators, story arcs, and
team appearances are all cached as part of the match commit, and
their pages (`/character/{cv_id}`, `/creator/{cv_id}`,
`/story_arc/{cv_id}`, `/team/{cv_id}`) light up with everything you
own that touches them.

This is the read-by-story payoff: a character page lists every
volume they appear in, sorted alphabetically with the issue range
each one covers. A story arc page walks the issues in order across
multiple series. The graph is ComicVine's; the *navigation* is what
Longboxes brings.

## Duplicate detection

If two files match to the same CV issue, that's a duplicate group.
`/admin/duplicates` lists them ranked by archive quality (page count
plausibility + interior resolution + cover quality + ComicInfo
coverage) and recommends a keeper. You can mark the duplicate as
`SUPPLEMENT` ("this isn't the issue, it's the variant cover gallery")
or rebind it to a different issue, or just leave it — duplicates
don't break anything, they just show up in the duplicates report.

## What the matcher won't do

- **Open or modify your archive files.** Read-only, always. Future
  metadata-sync support will write tags back, but only as an explicit
  opt-in.
- **Match on cover art alone.** Cover matching is on the roadmap but
  not yet built; today, the matcher works off filename + ComicInfo +
  CV metadata.
- **Guess at content not in ComicVine.** Use **Create as local
  volume** for those. The matcher won't fabricate a fake CV record.
