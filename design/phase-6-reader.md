# Phase 6 — Reader: possible follow-ups

The Phase 6 reader shipped — full-screen viewer at `GET /read/{file_id}`,
per-volume reading direction, per-user `read_progress` with sticky
`finished_at`, Continue reading / Recently read shelves, incognito
toggle. Migrations 0009 / 0010 / 0011.

The pieces deliberately deferred at build time, kept here as future
ideas rather than re-derive them later:

- **Volume-level progress on the library grid.** Issue lists show
  progress; the `/library` volume cards do not yet show an aggregate
  (e.g. "read 4 of 12").
- **In-reader extras.** Double-page spreads, a thumbnail page
  navigator, and a keyboard-shortcut overlay were not built.
- **An archive-handle LRU.** The reader opens the archive per request;
  a small cache could speed rapid page flips (see `comicbox_reader.py`).
