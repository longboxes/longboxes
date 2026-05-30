# Longboxes ⇄ Kapowarr Integration — API Draft

Status: **draft, not implemented.** This is the artifact to bring into
a Kapowarr-side conversation, and the spec Longboxes implements either
way (fork or upstream). Nothing here is committed.

## Goal

Let Kapowarr (or any *arr-style downloader) treat Longboxes as the
library catalog — the truth source for "what comics do I have, what's
missing" — while Kapowarr keeps its strengths (search providers,
download client integration, monitoring, scheduling). The Bazarr
relationship with Sonarr/Radarr is the model.

This lets users with messy, folder-structure-free libraries get
*arr-style automation without re-organising their files.

## Architecture summary

```
                       ┌──────────────────────────────┐
                       │  Longboxes (library catalog) │
                       │  - cv_volumes / cv_issues    │
                       │  - files / file_matches      │
                       │  - matcher + scanner         │
                       └──────────────────────────────┘
                                ▲           │
                  reads library │           │ emits webhooks
                  + CV cache    │           │ on match events
                                │           ▼
                       ┌──────────────────────────────┐
                       │  Kapowarr (downloader / *arr)│
                       │  - monitors missing issues   │
                       │  - drives download clients   │
                       │  - drops files in library    │
                       └──────────────────────────────┘
                                │
                       drops file in library mount
                                ▼
                       ┌──────────────────────────────┐
                       │  shared library volume       │
                       └──────────────────────────────┘
                                ▲
                                │ scanner picks up
                                │ → matcher resolves
                                ▼
                       [back to Longboxes — roundtrip closed]
```

The roundtrip:

1. Kapowarr asks Longboxes "what am I missing?" (GET /api/v1/missing).
2. Kapowarr searches its providers, kicks off a download.
3. Download client writes file to the shared library mount.
4. Kapowarr POSTs `/api/v1/library/import` with the path so Longboxes
   scans and matches it fast (no waiting for the next scheduled scan).
5. Longboxes emits a `match.created` webhook to Kapowarr; Kapowarr
   marks the issue acquired.

## API conventions

- **Base path:** `/api/v1/`. Major version in the path; minor changes
  are additive (new fields, new endpoints). A `v2` would ship
  side-by-side, not in place.
- **Format:** JSON request / response. UTF-8. Field names `snake_case`.
- **Times:** ISO-8601 with timezone, UTC.
- **Auth:** see [Authentication](#authentication) below.
- **Errors:** standard HTTP status codes plus a JSON body:
  ```json
  { "error": { "code": "missing_volume", "message": "CV volume 12345 not in cache" } }
  ```
- **Pagination:** cursor-based for list endpoints. Request with
  `?cursor=<opaque>&limit=N` (default 100, max 500). Response includes
  `next_cursor` (null when exhausted). Cursor is opaque to the client.
- **Idempotency:** mutating endpoints accept `Idempotency-Key` header
  for safe retries.

## Authentication

API keys, minted in the Longboxes admin UI. Each key is a long random
string with a configurable name and a set of **scopes**:

| Scope          | Grants                                              |
| -------------- | --------------------------------------------------- |
| `library:read` | List volumes/issues/files, query missing, read CV cache. |
| `library:write`| Trigger scans, import files, register webhooks.     |
| `cv:read`      | Read the ComicVine cache without library access.    |

Request header:

```
Authorization: Bearer <api_key>
```

A 401 is returned for missing/invalid keys; 403 for insufficient
scope. Keys can be revoked from the admin UI; revocation is
immediate.

For Kapowarr's normal operation: one key with
`library:read library:write cv:read`.

## Library endpoints

### `GET /api/v1/volumes`

List CV volumes that have any matched file in the library. The "I
care about this volume because I own at least one issue of it" set —
exactly what Kapowarr should monitor.

Query params:
- `owned_only` (bool, default `true`) — exclude volumes with zero
  present issues (still listed if Kapowarr is monitoring them, but
  the default surface is owned-only).
- `format` (string) — filter by classified format
  (`ongoing` / `limited` / `one_shot` / `collection`).
- `cursor`, `limit` — pagination.

Response:
```json
{
  "volumes": [
    {
      "cv_id": 22293,
      "name": "Inuyasha",
      "year": 2003,
      "publisher": "Viz",
      "format": "ongoing",
      "issue_count_total": 56,
      "issue_count_present": 8,
      "issue_count_missing": 48,
      "cover_url": "https://comicvine.gamespot.com/.../inuyasha.jpg",
      "url": "/api/v1/volumes/22293"
    }
  ],
  "next_cursor": "eyJvZmZzZXQiOjEwMH0="
}
```

### `GET /api/v1/volumes/{cv_id}`

Detail for one volume — same shape as a list entry, plus the full CV
description / themes / publisher / etc. from the cached `cv_volumes`
row.

### `GET /api/v1/volumes/{cv_id}/issues`

Every issue in the volume (from Longboxes' cv_issues cache) with
present/missing flags. The primary query Kapowarr uses to fill out a
volume.

Query params:
- `status` — `all` (default), `present`, `missing`.
- `cursor`, `limit`.

Response:
```json
{
  "volume_cv_id": 22293,
  "issues": [
    {
      "cv_id": 134041,
      "issue_number": "1",
      "name": "Turning Back Time",
      "cover_date": "2003-01-01",
      "cover_url": "https://comicvine.gamespot.com/.../inuyasha-001.jpg",
      "present": true,
      "file": {
        "file_id": "f4d2c8a0-...-uuid",
        "path": "/library/InuYasha/InuYasha 001 (1998).cbz",
        "sha256": "8a3f...",
        "size_bytes": 25192034,
        "page_count": 192,
        "match_status": "auto",
        "match_source": "comicinfo_cvid",
        "match_confidence": 1.0
      }
    },
    {
      "cv_id": 134046,
      "issue_number": "8",
      "name": "Stolen Spirit",
      "cover_date": "2003-12-01",
      "cover_url": "https://...",
      "present": false,
      "file": null
    }
  ],
  "next_cursor": null
}
```

Note: `present` is true when *any* `FileMatch` with status `auto` or
`confirmed` points at that issue's `cv_id`. Pending / rejected /
unmatched files don't count as present.

### `GET /api/v1/missing`

Flat list of every missing-issue gap across owned volumes. Kapowarr's
primary monitoring signal — "what should I search for next."

Query params:
- `volume_cv_id` — restrict to one volume.
- `format` — restrict by volume format.
- `since` (ISO-8601) — only issues whose CV `cover_date` is on or
  after this date.
- `cursor`, `limit`.

Response:
```json
{
  "missing": [
    {
      "issue_cv_id": 134046,
      "issue_number": "8",
      "name": "Stolen Spirit",
      "cover_date": "2003-12-01",
      "volume": {
        "cv_id": 22293,
        "name": "Inuyasha",
        "year": 2003
      }
    }
  ],
  "next_cursor": null
}
```

### `GET /api/v1/files`

List of files in the library. Useful for Kapowarr to spot duplicates,
broken matches, etc.

Query params:
- `match_status` — `auto`, `confirmed`, `pending`, `unmatched`,
  `rejected`, `local`, `supplement`, or `any` (default).
- `volume_cv_id` — only files matched to issues in this volume.
- `cursor`, `limit`.

Response: same `file` shape as the issues endpoint, paginated.

### `GET /api/v1/files/{file_id}`

Detail for one file. Same `file` shape plus a `matched_issue` block
when present.

## ComicVine cache piggyback

This is the high-value optional addition: expose Longboxes' CV cache
so Kapowarr doesn't need its own ComicVine integration. **One
ComicVine rate budget for the combined system.**

### `GET /api/v1/cv/volume/{cv_id}`

Cached CV volume payload. Returns `{ "data": <CV payload>, "fetched_at":
"..." }`. If the row is a stub or missing, Longboxes fetches it
inline (the same way `cv_cache.get_volume` does today) and returns
the result.

### `GET /api/v1/cv/issue/{cv_id}`

Cached CV issue payload. Same pattern.

### `GET /api/v1/cv/search`

Search via Longboxes' search cache.

Query params:
- `q` (required) — search query.
- `resources` — `volume` (default), `issue`, `all`.
- `limit` — max 100.

Response is the CV envelope as Longboxes caches it.

### Rate-limit awareness

These endpoints share Longboxes' `RedisRatePacer`. If a request would
exceed the pacer's `max_inline_wait`, the API responds:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 600
{ "error": { "code": "cv_rate_limited", "message": "..." } }
```

Kapowarr respects `Retry-After` and backs off. This is the same
contract the rate-limit hardening work landed for internal callers
during the stress test.

## Library mutation

### `POST /api/v1/library/scan`

Trigger a library scan. Equivalent of the admin "Rescan" button.

Body (all optional):
```json
{ "path": "/library/some/subdirectory" }
```

When `path` is omitted, scans all configured library roots. When
present, scoped to that subtree.

Response:
```json
{ "job_id": "rq:job:abc...", "queued_at": "2026-05-25T22:00:00Z" }
```

### `POST /api/v1/library/import`

Fast-lane "I just dropped this file, please pick it up now." This is
what Kapowarr calls after its download client writes a file. Avoids
waiting for the next interval-based scan.

Body:
```json
{
  "path": "/library/InuYasha/InuYasha 010.cbz",
  "expected_volume_cv_id": 22293,
  "expected_issue_cv_id": 134048
}
```

`expected_*` are optional hints. If present, Longboxes can use them
as a tiebreaker in matching (a downloader who *knows* the right
issue shouldn't need to roll the matcher dice).

Response (synchronous on success, async if matching needs the
matcher queue):
```json
{
  "status": "matched",
  "file_id": "f4d2c8a0-...",
  "match": {
    "issue_cv_id": 134048,
    "volume_cv_id": 22293,
    "match_status": "auto",
    "match_confidence": 1.0
  }
}
```

Or, if the matcher has to do real work:
```json
{ "status": "queued", "file_id": "f4d2c8a0-...", "job_id": "rq:job:..." }
```

Kapowarr can poll `GET /api/v1/files/{file_id}` or wait for the
`match.created` webhook.

## Webhooks

Longboxes pushes events to Kapowarr-registered URLs so Kapowarr
doesn't have to poll.

### `POST /api/v1/webhooks`

Register a webhook.

Body:
```json
{
  "url": "https://kapowarr.local/longboxes/webhook",
  "events": ["match.created", "match.updated", "file.imported", "file.removed"],
  "secret": "shared-secret-for-hmac"
}
```

Response: `{ "id": "wh_...", "url": "...", "events": [...] }`.

### `DELETE /api/v1/webhooks/{id}` / `GET /api/v1/webhooks`

Standard CRUD for managing registered hooks.

### Event delivery

Each event POSTs:

```
POST <subscriber-url>
Content-Type: application/json
X-Longboxes-Event: match.created
X-Longboxes-Delivery: <uuid>
X-Longboxes-Signature: sha256=<hmac>
```

Body:
```json
{
  "event": "match.created",
  "delivered_at": "2026-05-25T22:00:00Z",
  "data": { "file_id": "...", "issue_cv_id": 134048, "volume_cv_id": 22293, "match_status": "auto" }
}
```

The HMAC signs the raw body with the `secret` registered for the
hook. Kapowarr verifies before processing.

**Delivery semantics:** at-least-once with retry (exponential
backoff, ~6 attempts over ~24h). The `X-Longboxes-Delivery` UUID is
stable across retries — Kapowarr should dedupe on it.

### Events

| Event             | When fired                                                                |
| ----------------- | ------------------------------------------------------------------------- |
| `match.created`   | A new `file_matches` row is written.                                      |
| `match.updated`   | `file_matches.status` changes (e.g., `pending` → `confirmed`).            |
| `file.imported`   | A new `files` row is created by the scanner (independent of matching).   |
| `file.removed`    | A `file_locations` row's last current path goes missing.                  |

## File handoff workflow

The roundtrip Kapowarr drives, end-to-end:

1. **Discovery.** Kapowarr periodically calls `GET /api/v1/missing`
   (or reacts to a webhook event from Longboxes).
2. **Search + download.** Kapowarr's own logic; Longboxes is
   uninvolved.
3. **File placement.** The download client writes to a path inside
   the shared library mount. Naming is at Kapowarr's discretion —
   Longboxes is folder-agnostic, so anything readable works.
4. **Fast import.** Kapowarr POSTs `/api/v1/library/import` with the
   file path and the issue/volume CV IDs it expects.
5. **Match.** Longboxes scans the file (hash, ComicInfo, page count)
   and runs the matcher. With the `expected_*` hints, Stage 1 hits
   directly and writes a `FileMatch` row.
6. **Notification.** Longboxes fires `match.created` to Kapowarr's
   webhook URL.
7. **Done.** Kapowarr marks the issue acquired in its own state.

A graceful failure case: if the imported file doesn't match the
expected issue (filename misnamed, content mismatch), Longboxes
returns a `match_status` of `unmatched` or `pending` and the file
still lands in the library — just flagged for review. Kapowarr can
surface that to its UI.

## Multi-user model

For phase 1: **single tenant.** An API key is global to the
Longboxes instance. Kapowarr is treated as a service account.

For later: per-user API keys with library-path scoping, so a
multi-user Longboxes can host separate Kapowarr instances per user.
Designed-around: scope strings (e.g., `library:read:/path/to/user`)
that the API key checker enforces. Deferred until there's a
concrete multi-tenant ask.

## What's explicitly out of scope

- **Kapowarr's internals.** This doc only specifies the contract;
  Kapowarr's side (forking vs. plugin interface) is a separate
  conversation.
- **Other downloaders.** The API is generic. Whether it works with
  some hypothetical Mylar-replacement is downstream of "does
  Kapowarr ship support."
- **Library writes from Kapowarr beyond import.** Kapowarr doesn't
  edit `cv_*` rows or `file_matches`. Library state is owned by
  Longboxes.
- **Streaming / page-level reading APIs.** Out of scope here; that's
  reader-side territory.

## Open questions

1. **Pagination shape.** Cursor-based is the proposal; offset would
   be slightly simpler. Cursor wins for stability under concurrent
   writes (a freshly-matched file mid-page won't shift later pages).
2. **Auth granularity.** Are scopes enough, or do we need per-path
   ACLs from the start? Lean: scopes for v1, ACLs as a v2 add.
3. **Webhook retry backoff.** 6 attempts over 24h is a starting
   point — needs validation. Should Kapowarr report back acks /
   nacks so we stop retrying on permanent rejection?
4. **CV-cache write-through.** If Kapowarr discovers a CV entity
   that Longboxes hasn't cached, should the API trigger a Longboxes
   fetch? Today's design says yes (the cache piggyback endpoints
   transparently fill misses). Worth confirming that's the right
   default.
5. **Match-status visibility.** Should `match.updated` fire for
   review actions (auto → pending → confirmed)? Lean: yes — Kapowarr
   may want to know an auto-match got rejected by a human and the
   file is "back on the market."
6. **Bulk endpoints.** `GET /api/v1/missing` could get large. Is
   pagination enough, or do we want a "give me missing as a
   newline-delimited stream" mode? Defer until measured.
7. **Time-window queries.** Kapowarr wants "what changed since last
   poll." Cursor on the missing endpoint partly handles this; an
   explicit `since` watermark would be cleaner. Add when shaping the
   webhook retry path.

## Next steps

1. **Reach out to Kapowarr.** Bring this doc as a concrete starting
   point. Frame it as: "Longboxes wants to be the catalog half; this
   is what we'd expose; are you receptive to a `LibraryProvider`
   integration upstream, or should we plan around a fork?"
2. **If upstream is on the table:** iterate the spec with Kapowarr's
   maintainer; consider whether *they* publish the contract instead
   (the way Sonarr/Radarr publish the API Bazarr consumes).
3. **If fork is the path:** scope the gut-and-replace work on the
   Kapowarr side; identify which Kapowarr modules own library state.
4. **Either way — implement the Longboxes side.** The API in this
   doc is what Longboxes ships regardless. Sensible split:

   - **Phase 1:** read-only library endpoints + auth + minimal docs.
     Unblocks experiments and gives Kapowarr something to point at.
   - **Phase 2:** `/library/scan`, `/library/import`, webhooks. Closes
     the roundtrip.
   - **Phase 3:** CV cache piggyback. The big-leverage feature once
     phases 1–2 are stable.

## Files this would touch

- `app/api/` — new module for the v1 API routes (probably mirror
  the existing `app/admin/`, `app/review/`, `app/library_browse/`
  router style).
- `app/services/library.py` — already has most of the query shapes;
  thin DTOs wrap them.
- `app/services/health.py` — possibly extend for missing-count
  aggregates.
- `app/models/` — new tables only for API keys + webhooks (small
  Alembic migration).
- `docs/` — this doc, plus a generated OpenAPI / Swagger spec.

No changes to the matcher, scanner, pacer, or cache layer for
phases 1–2. Phase 3 (CV cache piggyback) shares the existing
`RedisRatePacer`; no new mechanism.
