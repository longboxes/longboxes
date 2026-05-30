# Phase 8 — Metadata Sync

**Status:** Planned. Not started.
**Depends on:** Phase 7 (review queue + confirm/reject) — sync operates on confirmed / auto matches.
**Sibling, deferred:** file renaming / moving (see *Out of scope*).

## Why

ComicTagger writes metadata into a comic archive once and forgets it. Longboxes
already maintains a stale-while-revalidate cache of ComicVine, so it can do
something CT structurally cannot: treat the archive's embedded `ComicInfo.xml`
as a **projection of ComicVine that it keeps current**.

This phase delivers a reconciliation loop, not a one-shot tagger:

1. Derive the metadata a file *should* carry from its confirmed CV match.
2. Detect where the file's embedded metadata has **drifted** from that.
3. Preview the difference and let the reviewer **apply** it.
4. When ComicVine itself changes — a corrected title, a fixed cover date, added
   credits — notice that matched files are now stale and offer to **re-sync**.

Step 4 is the differentiator. CT has no persistent CV cache and no notion of
"CV changed since you tagged," so it cannot offer it.

This is also the payoff of the matcher. Today a confirmed match produces only a
`file_matches` row — nothing outside the database. Writing the tag is what makes
the volume-first matching *mean* something: portable metadata that travels with
the file and is readable by any other tool (Komga, Kavita, ComicTagger).

## The core model

Everything in the phase is a comparison of two metadata states:

- **Desired** — computed from the confirmed CV issue plus its volume, story
  arcs, and credits. "What the tags should say."
- **Embedded** — the `ComicInfo.xml` currently inside the archive (already
  parsed into `ComicInfoExtract`).

The comparison yields a per-file **sync status**, which is to metadata what
`MatchStatus` is to matching:

| Status | Meaning |
|---|---|
| `untagged` | No ComicInfo in the archive at all. |
| `synced` | Embedded matches desired. |
| `drifted` | Embedded disagrees with desired (tagged elsewhere, or partial). |
| `cv_updated` | A CV revalidate moved the desired state; the file is now stale. |
| `unmatched` | No confirmed match — nothing to sync against. |
| `unsyncable` | Can't be written (CBR, or a write error). |

## Components

1. **Desired-metadata builder.** A service function: a confirmed `FileMatch` →
   cached CV issue / volume / arcs / credits → a `ComicInfo` document model.
   Maps CV fields onto the ComicInfo schema — Series (volume name), Number,
   Volume (start year), cover date, Title, Summary, creator credits,
   Characters / Teams / Locations, StoryArc, Publisher, and the CV issue ID
   into `<Web>` so a future re-scan hits the Stage 1 fast path. Shared by the
   drift detector, the dry-run preview, and the writer — single source of truth.

2. **Drift detector.** Field-level diff of desired vs embedded. Produces the
   per-file sync status and the data behind every preview.

3. **The write path.** comicbox (already a dependency, for reading) does the
   archive surgery. Longboxes builds the ComicInfo document; comicbox embeds it.

4. **Surfaces.** A `/sync` queue mirroring `/review` (grouped, bulk actions,
   mandatory dry-run preview); sync-status badges on the volume and issue
   pages; a per-file desired-vs-embedded diff with a "Sync now" action.

5. **The live loop.** When a background CV revalidate updates a `cv_issues`
   row, mark every file matched to that issue `cv_updated`. This is the
   "syncing" narrative made operational.

## Design risks — settle these first

**Content identity (the Longboxes-specific one).** Longboxes' whole data model
is keyed on content hash (`files.sha256`; move / duplicate / replace
detection). Writing tags changes the archive's bytes, so its sha256 changes —
to the scanner the file would look like an external "replace." The writer must,
inside one transaction: embed the metadata, re-hash the archive, update
`files.sha256`, and record the post-write hash (`synced_sha256`) so both drift
detection and the scanner recognize the change as Longboxes' own. CT never had
to solve this because CT is stateless. **Spike this before writing any write
code.**

**CBR is write-hostile.** Rar cannot be created by open tooling. Two options:
mark CBR `unsyncable` ("convert to CBZ to sync"), or offer an explicit, opt-in
CBR→CBZ conversion. Recommended: the conversion — but only as a consented
action, since it changes the user's file format on disk. (Many users want
CBZ-everything; it should not be silent.)

**Atomicity.** Never mutate the original in place. Write to a temp copy, embed,
re-open and verify the ComicInfo round-trips, then `os.replace` over the
original. Optional "keep a backup before first sync" setting.

**Manual-edit safety.** Longboxes owns a defined set of CV-sourced fields and
stamps `<Notes>` with a "synced from ComicVine by Longboxes @ <date>" marker.
Fields outside that managed set are never touched. This keeps re-sync
idempotent and non-hostile to anyone who hand-edited a personal field.

## Data-model changes

- A `file_metadata_sync` table (or columns on `file_matches`): `sync_status`,
  `last_synced_at`, `synced_sha256` (post-write hash), optionally
  `embedded_fingerprint` (cheap hash of the embedded ComicInfo to detect
  external edits).
- A `MetadataSyncStatus` enum mirroring the statuses above.
- An Alembic migration.

## Settings (admin)

- Which schema(s) to write: ComicInfo, MetronInfo, or both.
- Backup-before-write toggle.
- CBR policy: skip vs offer conversion.
- Auto-sync-on-confirm toggle — **default off**. Syncing a file is always an
  explicit action, never a silent side effect of confirming a match.

## Sub-phases

Sequenced so the risky part is isolated and everything before it is safe.

- **8A — Read-only foundations.** Desired-metadata builder, drift detector,
  `MetadataSyncStatus`, schema + migration. The UI shows sync status and
  field-level diffs but writes nothing. All upside, zero blast radius;
  de-risks everything downstream.
- **8B — The write path.** Atomic CBZ write via comicbox + re-hash + verify.
  Single-file "Sync now," behind a mandatory dry-run preview.
- **8C — Bulk sync.** The `/sync` queue, reusing the `/review` grouping and
  bulk-action patterns.
- **8D — The live loop.** CV revalidate → mark affected files `cv_updated` →
  resurface in the sync queue.
- **8E — Format coverage.** Opt-in CBR→CBZ conversion; PDF metadata writing;
  the backup setting.

## Testing

The write side needs more than the read side did. Round-trip tests are the
core: build a known desired-metadata document, write it into a real CBZ,
re-open and re-parse, assert the embedded ComicInfo equals desired. Cover the
re-hash path (post-write `sha256` recorded, scanner treats it as a no-op), the
verify-then-`os.replace` failure modes (a half-written temp must never replace
the original), and — in 8E — the CBR→CBZ conversion preserving page order and
content. Idempotence: syncing an already-synced file is a no-op.

## Out of scope

**File renaming / moving** is a sibling capability and its own future phase. It
will reuse this phase's desired-metadata model for its filename template, so
8A's builder pays for it too. Kept separate because it is commodity (CT does it
the way everyone does) and it interacts with the scanner's move detection — a
different risk surface.

## Open decisions

- ComicInfo only, or ComicInfo + MetronInfo? (MetronInfo's identifier resources
  are cleaner; ComicInfo is universal.)
- CBR: skip, or offer conversion? (Recommended: offer, opt-in.)
- Is the managed field set fixed for v1, or configurable?
- Does "confirm" in the review queue stay purely a match action, with sync
  always separate — or is there a power-user "confirm & sync" affordance?
