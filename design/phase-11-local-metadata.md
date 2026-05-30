# Phase 11 — Local Metadata & Supplements: possible extensions

Phase 11 shipped — sub-phases 11A–11F. `local_volumes` / `local_issues`
tables, `MatchStatus.LOCAL` / `SUPPLEMENT`, the three new
`file_matches` polymorphism columns, the per-file create-local and
attach-as-supplement workflows, browse-side merge into `/library` and
the home page, the volume page's "Supplements" section, bulk
create-from-group, edit + merge for local volumes, the matcher skip.

Deliberately out of scope at build time; kept here as future ideas:

- **Rich local metadata** — descriptions, creators, characters, arcs.
  Local issues are currently metadata islands by design; they can't
  join CV story arcs or character pages (which are keyed on CV ids).
  Extending the model to add a parallel `local_credit` / `local_arc`
  story would close that gap.
- **Per-issue supplements.** Phase 11F attaches supplements at the
  volume level only. A gallery of just issue #1's variant covers
  still attaches to the volume. Per-issue attachment is a clean
  future extension — the `file_matches` row would gain an optional
  `supplement_issue_cv_id`.
- **Supplement types beyond cover galleries in the picker.** The
  schema's `supplement_type` column is open-ended; only `cover_gallery`
  is wired into the UI. Adding `sketch`, `script`, `trade_dress`,
  etc. is just a picker change — no migration.
- **Editing a CV-matched issue's metadata.** Closer to a future
  metadata-sync (Phase 8) concern than a local-metadata one.
- **Promoting a local entry to a CV match** when ComicVine later
  catalogues the book. Today a reviewer can reject and re-match by
  hand; automatic reconciliation is future work.
- **Local cover-art upload.** Today the cover is the file's own first
  page. A separate uploaded cover image would let a local entry carry
  a poster-style image distinct from the archive content.
- **Private / unlisted library content.** The `local_*` tables are
  also a foundation for any future notion of library content that
  intentionally never goes to ComicVine — fan works, scanlations,
  private builds.
