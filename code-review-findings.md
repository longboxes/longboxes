# Longboxes pre-launch code review

## Executive summary

The codebase is in good shape overall — strong layering (routes → services → cache → client), well-documented intent in nearly every function, and only one stray `TODO`. The dominant remaining risks are size and accreted duplication: `app/services/library.py` is 4,102 lines with a single function (`get_volume_detail`) at 888 LOC, and `app/library_browse/routes.py` (~1,820 lines) and `app/review/routes.py` (~1,650) both should split. The eight quick-win refactors from the original review have been executed (see "Completed" section at the bottom); what remains is the medium and architectural cleanup. None of this is a blocker for OSS launch.

## Findings — quick wins (remaining)

- **`app/services/local.py` has 28 dataclasses/funcs at 965 LOC** with no inner separators except `# ----`; adding a `__all__` would make the public API legible for an OSS audience reading the file cold.

- **Browse route file's `_normalize_format` (~line 285) and `_LIBRARY_FORMATS` (~line 280) sit next to the deduped `_normalize_letter`.** All three input-normalizers belong in a single `app/library_browse/_param_normalizers.py` (or at the top of the file under a `# ---- Param normalizers ----` band).

- **`app/services/__init__.py`, `app/reader/__init__.py`, `app/auth/__init__.py` are docstring-only.** Fine, but `app/scripts/__init__.py` is 0 bytes — add the one-line docstring for consistency or delete and let it be implicit.

- **`app/comicvine/cache.py` `_noop_enqueue` is a one-liner that takes two args and returns None.** Replace with `lambda *_: None` and inline at the use site; saves a top-level symbol.

- **`app/jobs/scan.py:104` does `from rq import Queue` at function scope** — the other two job files import `Queue` at module level. Inconsistent.

- **`app/services/library.py` has 28+ `@dataclass` definitions at the top, then 25+ more interleaved with functions starting at line 2393 (`ArcIssueRow`).** Pull all dataclasses to the top, or split into `app/services/library_models.py`.

## Findings — medium (each 1-2 hours)

- **`app/comicvine/cache.py:177-295` — seven `get_X` methods + seven `_upsert_X` methods that differ only in (entity-name, model class, payload-stub field set).** `get_story_arc`, `get_character`, `get_person`, `get_team` are five-line cookie cutters. `_upsert_story_arc/_character/_person/_team` (lines 677-785) are *byte-for-byte the same template*. Collapse into a generic `_upsert_simple_entity(model, payload, ttl_kind)` + a registry dict `_ENTITY_MODELS = {"story_arc": CvStoryArc, ...}`. `get_volume`, `get_issue`, `_upsert_volume`, `_upsert_issue` should keep their own bodies — they have real per-type variation. End state: ~250 fewer LOC.

- **`app/library_browse/routes.py:1337-1561` (character/creator/team routes) and `app/services/library.py:3615/3826/3959` (3 details).** Each route repeats `for X in detail.foo: if not X.is_hydrated: enqueue_revalidate("entity", X.cv_id)` walks 3-6 times. Extract `_enqueue_hydration_for_cards(cards, entity_type)` helper.

- **`app/library_browse/routes.py:cv_cache_ctx` is not used in `app/review/routes.py` — 6 manual `client = ComicVineClient(); try: ... cache = ComicVineCache(...) ... finally: await client.aclose()` blocks remain** at `app/review/routes.py:449, 899, 1243, 1439, 1581, 1650`. Hoist `cv_cache_ctx` to `app/comicvine/__init__.py` and migrate the review file.

- **`app/library_browse/routes.py:_cv_error_response` has no counterpart in `app/review/routes.py`.** Review does the equivalent inline via `raise HTTPException(...)` with hand-written 503/502/404 branching. Two patterns coexist. Pick one and route through a single helper.

- **Hydration polling endpoints follow the same template six times.** `library_hydration` (~440), `volume_issues_hydration` (~660), `arc_issues_hydration` (~1247), `publisher_arcs_hydration` (~1667); `volume_confirm_covers_hydration` (~552). Each: parse CSV → query rows → render swap fragments via two `templates.env.get_template(...).module.X` calls → return `{swaps, completed_ids}`. With `parse_id_csv` already extracted, a `render_hydration_swaps(rows, render_pairs)` helper would consolidate the remaining ~60 LOC × 5 endpoints.

- **`app/services/library.py:get_volume_detail` is 888 lines (`1193-2080`).** Doing arc-fill, gallery-shelf computation, credit filtering, segment computation, list-rail and gallery-rail driving, plus pagination. Split into: `_load_volume_and_issues`, `_apply_credit_filter`, `_fill_arc_membership_from_arcs`, `_build_segments_and_shelves`, `_build_rails_for_volume`. Today it's un-reviewable in a PR.

- **`app/jobs/match_file.py:_run`, `app/jobs/revalidate.py:_run`, `app/jobs/scan.py:_run`, `app/jobs/scrape.py` `_run` (twice) all share the boilerplate:** `engine = create_async_engine(...NullPool); try: ... finally: await engine.dispose(); asyncio.run(_run())`. Three additionally do `RedisRatePacer` + `ComicVineClient` setup. Extract `app/jobs/_runtime.py` with a `@rq_async_job(want_cv: bool = False)` decorator. End state: each `*_job` body is ~10 lines of real work instead of 25+. Bonus: the rate-limit reschedule pattern (now using `reschedule_delay` from pacer) merges in too.

- **`app/comicvine/cache.py:hydrate_volume_issues` is 136 lines with a 70-line inner loop.** Pull the per-issue upsert into `_bulk_upsert_one_issue`.

- **`app/services/rail.py:build_list_rail` (223 LOC) and `build_gallery_rail` (310 LOC).** Likely share 60-70% of their model assembly. Pull shared body into `_build_rail_model_base(...)`.

- **`app/services/library.py:list_library_volumes` (326 LOC, line 649)** — splittable into `_build_cv_query(filters)` and `_merge_local_volumes(rows, filters)`.

- **Test fixture builders (`_file()`, `_cv_volume()`, `_cv_issue()`, `_local_issue()`, `_local_volume()`) are reinvented per file** — `tests/test_duplicates.py:48,319,338`, `tests/test_health.py:21`, `tests/test_library_service.py:142`, `tests/test_match_enqueue_order.py:61`, `tests/test_reader_direction.py:38,50,65`, `tests/test_reader_progress.py:50,71,83`. Pull into `tests/fixtures/model_builders.py` exporting `make_file()`, `make_cv_volume()`, etc.

- **`tests/fixtures/__init__.py`, `tests/fixtures/cbz_builder.py`, `tests/fixtures/images.py` exist** — directory is already there but used only for CBZ assembly. Extend with model builders.

- **`app/review/routes.py:_file_volume_search`, `volume_search`, and `volume_fix_match_search` (library_browse) all do the same CV-search-and-publisher-fill dance** with subtle but accidental variation: `volume_fix_match_search` doesn't catch `ComicVineKeyMissingError` / `ComicVineKeyInvalidError` separately. With `cv_search` helpers now extracted, the final consolidation is a `cv_volume_search(db, query, *, include_publishers=True)` wrapper into `app/services/cv_search.py` that wraps the search-and-publisher-fill block.

- **`app/services/library.py` `CharacterDetail`, `CreatorDetail`, `TeamDetail` each have 5 nearly-identical tab-pagination blocks (`X`, `X_page`, `X_page_count`, `X_total`, `X_letter`, `X_filtered_count`).** 30+ trivially-repeated fields. A `@dataclass TabPagination` could express each tab as `friends: TabPagination[EntityCard]`.

## Findings — architectural (worth a separate work session)

- **Split `app/library_browse/routes.py` into 4-5 files: `library.py` (`/library` index), `volume.py`, `issue.py`, `entity.py` (arc/publisher/character/creator/team), `fix_match.py`.** At ~1,820 lines, even with the per-section banner comments, finding the route for a given URL means scrolling. The path is straightforward — the file already groups by `# ---- /library ---` bands. Sequencing: extract `cv_cache_ctx` + `_cv_error_response` + `_entity_not_found_response` to `app/library_browse/_helpers.py` first, then split. Estimate: half a day.

- **`app/services/library.py` at 4,102 LOC is the single biggest readability liability.** Split into `library_index.py`, `volume_detail.py`, `issue_detail.py`, `arc_detail.py`, `publisher_detail.py`, `entity_pages.py`. Each ends up ~500-800 LOC. Keep `app/services/library.py` as a thin re-export façade so route imports don't break. Estimate: 1 day including test-import sweep.

- **The "scrape" vs "revalidate" enqueue pattern is duplicated for no real reason.** Both modules define `enqueue_X(..., *, queue=_QUEUE_NAME)` + deterministic job id + `Queue(queue, ...).enqueue(...)`. Generalize into `app/jobs/_enqueue.py:enqueue_idempotent(func, args, *, queue, job_id, at_front=False, dedupe=True)`.

- **Worker-topology import-rename trick is too clever for its blast radius.** `from app.jobs.revalidate import enqueue_revalidate_interactive as enqueue_revalidate` in two router files, plus 20+ lines of docstring/comment defending the pattern. (The scrape wrappers were consolidated into `app/jobs/scrape.py`, so only the revalidate rename remains.) A future contributor renaming the import or forgetting to apply it in a new route hands an OSS user a silent regression. Better: have `enqueue_revalidate` take `queue` as a *required* kwarg (no default) so every call site declares its lane, then drop the wrapper. Migrating costs ~50 mechanical call-site changes; long-term cost is zero.

- **Templates' "two macros per fragment, render both into the JSON swap" pattern is repeated in five hydration endpoints.** A small `render_swap_pair(rows, list_macro_loc, gallery_macro_loc, id_prefix_list, id_prefix_gallery)` helper would consolidate ~50 LOC × 5 endpoints. Bigger reach: would force macro names to be consistent (today: `arc_issue_row`, `volume_issue_row`, `library_grid_card`, `gallery_card`, `list_row` — five names for the same semantic role).

- **`app/services/review.py` at 1,720 LOC** — same split treatment as `library.py`: `review_queue.py`, `volume_confirm.py`, `bulk_confirm.py`, `file_review.py`, `fix_match.py`.

## What's good

- **Layered architecture: route → service → cache → client is followed religiously.** Routes are mostly thin (long ones are long because they hydrate, not because they re-implement business logic).
- **Docstrings are the strongest single quality.** Nearly every function has one that explains *why*, with cross-references to design docs in `design/`. Above average for OSS.
- **No `TODO` / `FIXME` debris.** Exactly one comment is load-bearing documentation, not a deferred fix.
- **No `print()` in app code outside `app/scripts/`.**
- **`tests/conftest.py`'s `db_session` truncate is the right shape for hard isolation.** `NullPool` choice in tests + jobs is correctly motivated and consistently applied.
- **Template macros (`entity_hero`, `tab_bar`, `person_card`, `credit_alphabet`) are doing real reuse work.**
- **Type annotations are present everywhere they matter.** Ruff is configured with a sensible rule set.
- **The cache layer's stub-row pattern is well-considered.** Docstrings explaining FK-safety motivation are exactly what an OSS reader needs.
- **Helper consolidation.** `safe_int`, `parse_id_csv`, `parse_iso_date` live in `app/services/cv_helpers.py`; CV-search helpers in `app/services/cv_search.py`; rate-limit `reschedule_delay` on `app/comicvine/pacer.py`. Cross-router private imports are gone.

## Sanity stats

### Largest 10 files by LOC (excluding `.venv`):
| LOC  | Path |
|------|------|
| ~4,090 | `app/services/library.py` |
| 3,434 | `tests/test_library_service.py` |
| ~1,820 | `app/library_browse/routes.py` |
| 1,720 | `app/services/review.py` |
| ~1,650 | `app/review/routes.py` |
| ~1,020 | `app/matcher/pipeline.py` |
| 1,003 | `tests/test_matcher.py` |
| 965  | `app/services/local.py` |
| ~920 | `app/comicvine/cache.py` |
| 689  | `app/admin/routes.py` |

### Marker counts:
- `# TODO`: **0** in app code.
- `# FIXME` / `# HACK` / `# XXX`: **0**.

### Wildcard imports: **0**. Clean.

### Print calls in app code (outside `app/scripts/`): **0**. Clean.

## Deferred to post-launch

These are tracked but explicitly out of scope for the launch — none of them are user-visible blockers, and shipping them now would expand the test surface right before cut.

- **Bulk volume fetch for character / creator / team pages.** Today, opening a character page with 40 un-hydrated volumes enqueues 40 separate `revalidate("volume", cv_id)` jobs and burns 40 ComicVine calls back-to-back. CV's `/volumes/` endpoint accepts a comma-separated `filter=id:1|2|3...` and returns up to 100 in one round-trip. Sequencing: (1) add bulk fetch method on `ComicVineClient`; (2) add bulk cache + upsert on `ComicVineCache`; (3) new job `hydrate_character_volumes` / `hydrate_creator_volumes` that batches by 100; (4) character/creator/team routes enqueue *one* bulk job instead of N per-volume revalidates; (5) tests for the batched path. Estimate: half a day. Cuts per-page hydration latency from ~minutes to ~seconds on cold caches.

## Completed (this pass)

The original review's eight quick-win refactors are in. Captured here so the medium/architectural sections above stay focused on what's left:

- ✅ Deleted shadowed `_normalize_letter` in `app/library_browse/routes.py`.
- ✅ Cross-router private imports (`_clean_search_query`, `_publishers_for_volumes`, `_result_facets`, `_shape_volume_results`) hoisted to `app/services/cv_search.py` as public symbols.
- ✅ `app/jobs/noop.py` kept (it backs the `just test-job` diagnostic) with docstring documenting the intent.
- ✅ Three `_safe_int` / `_safe_year` helpers consolidated into canonical `safe_int` in `app/services/cv_helpers.py`.
- ✅ Five `?ids=1,2,3` parse loops → `parse_id_csv` in `cv_helpers`.
- ✅ Two identical `_reschedule_delay` → `reschedule_delay` on `app/comicvine/pacer.py` (next to `DEFAULT_PENALTY_SECONDS`).
- ✅ Two browse-route scrape wrappers → `enqueue_*_scrape_interactive` in `app/jobs/scrape.py`.
- ✅ Three year-lenient + one date-lenient parse blocks → `safe_int` / new `parse_iso_date` in `cv_helpers`.

## Completed (final pre-launch pass)

Findings from the second pre-launch review, all shipped:

### Launch blockers (5)

- ✅ **Worker rename across docs + code + tests.** `worker-non-match` was split into `worker-interactive` (browse hydration, scrape wrappers) and `worker-scan` (scan queue) in `docker-compose.yml` + `deploy/docker-compose.yml`. All references in `docs/src/content/docs/install.md`, `docs/src/content/docs/quick-start.md`, `app/worker.py` docstring, `app/jobs/scan.py` comment, `app/jobs/scrape.py`, `app/jobs/revalidate.py`, `app/library_browse/routes.py`, `app/search/routes.py`, `app/config.py`, and `tests/test_search_routes.py` updated to match.
- ✅ **Setup wizard docstring stale.** `app/templates/setup_comicvine.html` no longer claims first-key-save auto-queues a match-all pass; it now describes the hold-and-resume mechanic via `NO_KEY_RESCHEDULE_SECONDS` reschedule cycles.
- ✅ **Dead `match_all_queued` branches in `admin_home.html`.** The wizard no longer auto-queues match-all, so the nested branches inside the welcome and `cv_key=saved` banners were dead. Welcome banner now reads "Any files already scanned are queued; matching resumes within ~60 seconds. Track progress on Library health." The standalone admin-button `match_all_queued` banner (for the explicit `/admin/match-all` POST) is preserved.
- ✅ **`_reschedule_match` had no Redis-failure handling** in `app/jobs/match_file.py`. Now retries up to `_RESCHEDULE_REDIS_RETRIES = 3` times with linear backoff (`_RESCHEDULE_REDIS_BACKOFF_SECONDS = 2.0`), logs each attempt at WARNING, and re-raises on final failure so RQ marks the job failed and the admin can recover via `/admin/failed-jobs`.
- ✅ **Auto-refresh swap didn't initialize Alpine on the replaced subtree.** `app/static/auto-refresh.js` now calls `window.Alpine.initTree(fresh)` after every `replaceWith`, guarded by try/except. Fixes the publisher + format filter on `/search?kind=volumes` not reacting to volume cards hydrated after page load.

### Polish (6)

- ✅ **Welcome-banner copy mentions the 60-second resume.** Folded into Blocker 3's `admin_home.html` rewrite.
- ✅ **CV rate-limit hint link text.** `_cv_search_error_response` in `app/search/routes.py` now says "click 'Library results' above" — matches the actual back-link label on the error page.
- ✅ **Three SearchHit volume builders consolidated.** `_search_volumes` (library), `_cv_volume_hit` (CV catalogue), and the volume branch of `get_hit_for_hydration` (post-hydrate poll) all funnel through a new `_build_volume_hit(...)` helper in `app/services/search.py`. Single source of truth for the rich-volume `SearchHit` shape — subtitle wording, detail-URL convention, optional-field set stay in lockstep automatically.
- ✅ **Header-dropdown skips the JSONB credits walk.** `search_library(..., include_credits_stubs=False)` threads through `_KIND_FETCHERS`; `/search/live` passes False. The credit stubs (un-hydrated characters / creators / teams / arcs surfaced by JSONB scan of owned issues) still appear on the full `/search` page, where they drive hydration. The dropdown's per-keystroke budget no longer pays for them.
- ✅ **N+1 publisher lookup collapsed.** `get_hit_for_hydration` volume branch used to fire two `db.get()` calls (volume + publisher) per poll tick; now uses the same outerjoin SELECT pattern as `_search_volumes` — one SQL round-trip.
- ✅ **Stale scanner-gate docstrings.** `tests/test_scanner.py::_run_scan` and `tests/test_auth.py` "/setup/comicvine" section comment no longer claim a scanner-side match-enqueue gate or an auto-match-all on first key save. Both behaviors were removed when the match worker started holding-and-rescheduling on missing key.
