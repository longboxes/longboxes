# Phase 10 — Cover Matching

**Status:** Planned. Not started.
**Depends on:** nothing hard. The perceptual-hash floor is fully
self-contained. The optional embedding tier (10D) shares Phase 9's "optional
model behind a generic endpoint" posture and could reuse that plumbing, but
doesn't require Phase 9.

## Why

The cover is the strongest per-issue signal Longboxes isn't using. Series
name, volume, year and issue number are all *shared* across a run — the one
thing unique to an individual issue is its cover art. Bringing it into the
matcher addresses exactly where the matcher is weakest today:

- right volume, but which issue — the cover disambiguates.
- one-shots and graphic novels with no issue number to map.
- right series, wrong volume — a different cover gives it away.

It's also a different matching *axis* from the filename. Today everything
flows from `comicfn2dict` parsing the filename into a series to search. The
cover is independent corroboration — and confirmation strong enough to turn a
shaky PENDING into an AUTO, cutting manual review.

ComicTagger already does cover matching, via perceptual hashing. So a
perceptual-hash floor here is CT parity — proven, and worth having. The
improvement over CT is the optional embedding tier; CT is ~10 years old and
never moved past hashing.

## The problem shape

This is **not** exact-image matching. The file's cover is the first page of
the archive — a scan or a digital rip — and the CV cover is ComicVine's
uploaded image. They're the same artwork in different *reproductions*:
different resolution, JPEG compression, crop, sometimes a border or a
scanlator logo. The task is near-duplicate detection across reproductions.

Two real-world complications:

- **The first page isn't always the cover.** It can be a credits page, an ad,
  or a "scanned by…" page. comicbox's `cover_filename()` (honoring ComicInfo's
  `<Page Type="FrontCover">`) mitigates this, and Longboxes already uses it —
  but it isn't perfect.
- **Variant covers.** One issue can have several cover artworks. CV exposes
  the main cover plus `associated_images`; the file's cover may match any one
  of them. The comparison has to run against the *set*, taking the best
  similarity.

And one honest limitation: cover matching **disambiguates and confirms; it
does not originate candidates.** ComicVine has no reverse-image-search API, so
the matcher still needs the filename / ComicInfo to surface a candidate
volume. The cover then picks and confirms within that candidate set — it
can't match a zero-metadata file out of nothing.

## Approach — two tiers

Same shape as Phase 9: a cheap deterministic floor, an optional model tier on
top.

**Tier 1 — perceptual hash (the floor).** A pHash/dHash-style hash
(`imagehash`, pure Python) — resize the cover to a tiny grayscale grid, derive
a bit-string, compare with Hamming distance. Free, fast, deterministic, zero
external dependencies. This is the same family ComicTagger uses, so it's CT
parity, and it catches the easy majority. Its weakness is sensitivity to crop
and aspect-ratio differences.

**Tier 2 — vision embeddings (optional).** A CLIP-style model turns each cover
into a vector; cosine similarity gives "same artwork" robustly across crop,
resolution, compression and even variant treatments — exactly where the plain
hash gets shaky. Runs locally on CPU as a background job, or via an embedding
endpoint. Off by default.

**Not** a multimodal LLM judging two covers. That's per-comparison cost
(≈5 candidates per file), slow, non-deterministic, and overkill for what is
fundamentally a similarity number. Explicitly out of scope.

## Architecture

Cover signatures — the hash, and optionally the embedding — are computed
**once per image and cached**, the same SWR / background-enrichment pattern as
cover hydration and Phase 9. The per-comparison work (a Hamming distance or a
cosine) is then trivially cheap; there are no expensive per-comparison calls.

- When a file is scanned, extract its cover (already done for display),
  compute its perceptual hash (and, if the embedding tier is on, its
  embedding), store on the `files` row.
- When a CV issue is hydrated, compute the same for its cover — and for its
  `associated_images` variant covers — store on `cv_issues`.
- The matcher reads these at scoring time and computes similarity in-process.

## Matcher integration

Cover similarity becomes a signal in `_score`, computed per candidate (the
file's cover vs *this* candidate issue's cover set):

- **Strong match → confidence boost.** A near-certain cover match is near-proof
  of the issue; it should be able to lift a shaky candidate into AUTO.
- **Clear mismatch → mild penalty.** Mild on purpose — the first archive page
  isn't always the cover, and variants exist. A mismatch lowers confidence; it
  does not hard-reject.
- **Missing either cover → neutral.** No signal, no effect.

The exact weighting and thresholds are a 10B design task — the existing
`_score` blends weighted components and then applies multipliers, and the
cover signal has to slot in without letting a false mismatch (wrong first
page) torpedo a correct match. The posture matches the format penalty: the
cover *confirms*, it doesn't blindly override.

## Data model

- `files`: `cover_phash`, optional `cover_embedding`, `cover_hashed_at`.
- `cv_issues`: `cover_phash`, optional `cover_embedding`, hashed-at — plus the
  variant covers from `associated_images` (either a small `cv_issue_cover`
  table, or a list of hashes on the row).
- Embeddings are vectors; at this scale (comparing a file against ~5
  candidates) they don't need a vector index — store them and compute cosine
  in Python. `pgvector` would be overkill.
- Alembic migrations.

## Settings (admin)

- Cover matching: on / off (the perceptual-hash tier).
- Embedding tier: off (default) / a model endpoint — the same generic-endpoint
  posture as Phase 9, for an embeddings API.
- Optionally the cover signal's scorer weight, once 10B has sane defaults.

## Risks

- **Wrong first page.** Mitigated by comicbox's `cover_filename()`, and by
  keeping the mismatch penalty *mild* rather than a hard reject.
- **Variant covers.** Compare against the issue's main cover plus
  `associated_images`; take the best similarity.
- **pHash crop-sensitivity.** The known weakness of the floor — and the entire
  reason the embedding tier exists.
- **Optional-dependency cost.** The embedding tier is a resource cost (local)
  or an external one (hosted); the perceptual-hash floor has neither, which is
  why it's the default.
- **Cost surface.** Perceptual hashing is cheap CPU — fine to run for every
  file. Embeddings are heavier but still a once-per-image background job;
  CV-side, only the issues in candidate volumes get hashed.

## Sub-phases

- **10A — Perceptual-hash foundation.** Hash file covers and CV issue covers
  (including `associated_images`); store and cache; a similarity helper. No
  scoring change yet — a safe, self-contained foundation.
- **10B — Matcher integration.** Cover similarity as a scorer component in
  `_score` — boost on a strong match, mild penalty on a clear mismatch,
  neutral when data is missing.
- **10C — Review surfacing.** A cover-match indicator on the review candidate
  cards (queue, file, bulk) so a reviewer sees the signal that moved the
  score.
- **10D — Embedding tier.** Optional CLIP-style embeddings as the
  higher-accuracy similarity, behind a setting; the cosine path layered over
  the hash floor.

## Also enables

The cached cover signatures are useful beyond the matcher:

- **Softer duplicate detection.** The scanner dedups by sha256 — exact bytes.
  Cover signatures catch "the same comic, two different scans / rips"
  (different sha256, near-identical cover), which sha256 can't.
- **A local cover index.** With CV issue covers hashed and stored, a file with
  no usable filename metadata could be matched by searching its cover against
  the local index of already-cached CV covers — a partial reverse-image search
  that works for any issue already in the cache.

## Out of scope

- **A multimodal LLM comparing covers.** Wrong tool — see *Approach*.
- **Cover-art generation or editing.** Longboxes mirrors ComicVine; it doesn't
  invent art.
