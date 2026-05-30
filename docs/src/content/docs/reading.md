---
title: Reading comics
description: The built-in browser reader, reading direction, progress tracking, and incognito mode.
---

Longboxes ships with a full-screen browser reader. No app to install,
no OPDS to wire up — you click an issue cover, the page renders.

## Opening the reader

Every issue page has a **Read** button. Clicking the cover image on
the issue page does the same thing. Both lead to `/read/{file_id}`:

- The header disappears (the reader owns the viewport).
- The first page loads, or the page you were on if you've read this
  before (see *Progress* below).
- Edge taps / arrow keys flip pages.

The reader works on any file with a readable archive — matched,
unmatched, local, supplement. If the archive opens, the pages render.

## Fit modes

The toolbar carries a fit toggle:

- **Fit width** — the page scales to the viewport width; long pages
  scroll vertically. Best on tall portrait phones.
- **Fit height** — the page scales to the viewport height; nothing
  scrolls. Best on desktops and tablets.

Mobile landscape covers (covers wider than they are tall) letterbox
cleanly in fit-height instead of stretching. Wraparound covers
(double-wide back+front spreads) crop to the front half automatically
so the reader card never shows the back cover.

## Navigation

| Action | What it does |
| --- | --- |
| Right arrow / right edge tap / right edge button | Next page |
| Left arrow / left edge tap / left edge button | Previous page |
| Click center of page | Toggles the toolbar |
| `F` | Fullscreen |
| `Esc` | Leave fullscreen (only) — does **not** close the reader |
| Close button | Returns to the issue page |

In **manga / right-to-left** mode (see below), the left/right inputs
swap so the arrow keys feel correct for RTL reading.

## Reading direction (manga mode)

The toolbar has an `LTR` / `RTL` toggle. It's per **volume**, not per
file — once you flip a Japanese series to RTL, every issue in it
remembers. The label shows the action it *performs*: "RTL" means
"flip to right-to-left."

Direction is stored on the `cv_volumes` row (or `local_volumes` for
hand-catalogued series), so it survives a ComicVine cache clear and
travels with the volume across re-scans. An unmatched file (no
volume to attach to) falls back to the default LTR.

## Progress tracking

Progress is per **user** — not per file. Each user has their own
reading position on every file they've opened.

- The reader opens at your last-read page automatically.
- Position is saved as you read (debounced, so flipping fast doesn't
  spam the database).
- Closing the tab or hitting close flushes the last page reliably
  (via `sendBeacon`, so the final flip is never lost).
- Reaching the last page stamps `finished_at`. The mark is **sticky** —
  paging *back* through a finished comic doesn't un-finish it.

Progress surfaces in four places:

- **Continue reading** on the home page — files you've started, past
  the first page, not yet finished.
- **Recently read** on the home page — files you've finished.
- A thin progress bar along the bottom edge of issue-page hero
  covers.
- The same bar on volume issue lists (table and gallery views).

## Resetting and incognito

Two controls for when you want to manage what's recorded:

**Reset reading progress** lives on the issue page. It clears your
`read_progress` row for that file — both the page position and the
`finished_at` mark. The next time you open the file, it starts from
page 1 as if you'd never opened it.

**Incognito reading** is the eye toggle in the header. With it on,
the reader still *resumes* from your saved position, but it stops
*recording* new progress. Useful when:

- You're sharing the account with a household member who's reading
  ahead.
- You're skimming back through a finished arc and don't want to
  pollute Continue reading.
- You just want to read without the bookkeeping for a session.

Existing progress is untouched. Incognito means "not tracked," not
"forgotten."

## What the reader doesn't do (yet)

- **Double-page spreads.** Each page is its own image; the reader
  doesn't yet pair them for two-up reading.
- **A page thumbnail navigator.** Today you flip linearly; jumping to
  page 47 of a 90-page omnibus means flipping 47 times. (Or editing
  the URL — `/read/{file_id}?page=46` works, but it's not exposed in
  the UI.)
- **OPDS or external reader app support.** Longboxes' reader is
  browser-only; Tachiyomi / Mihon / Panels / Chunky / KOReader can't
  connect today. On the roadmap.

## Archive format support

Reader plays anything Longboxes can scan: **CBZ**, **CBR**, **CB7**,
and **PDF**. EPUB is not supported.

PDFs are rendered page-by-page through the archive layer; performance
is reasonable for typical comic PDFs but a very large scanned PDF
(50+ MB per page) can be slow on the first paint of a fresh page.
