# Phase 9 — AI-Assisted Enrichment

**Status:** Planned. Not started.
**Depends on:** nothing hard — independent of Phase 8 (Metadata Sync). The
collected-contents structure (workstream C), once built, would feed both the
matcher and Phase 8's desired-metadata builder.
**Relationship to the existing heuristics:** purely additive. The keyword /
count classifier and `comicfn2dict` stay; the LLM layers on top and degrades
gracefully to them.

## Why

The keyword + issue-count volume classifier just showed its limits: a
multi-volume collected-editions line (Vagabond, Viz, 37 collected books)
needed a waterfall reorder and a documented false-positive risk to classify
correctly. That's the signature of a problem that wants reading comprehension,
not pattern-matching. ComicVine stores a paragraph of prose about each volume;
an LLM can read it and form the judgment the heuristic only approximates.

Three jobs for the LLM, in increasing ambition:

1. **Classify volumes** — format (ongoing / limited / one-shot / collection)
   and edition language — by reading the description.
2. **Parse stubborn filenames** the cheap parser can't.
3. **Extract what a collected edition collects** — and turn that into a
   TPB-to-issues link ComicVine's own data model doesn't have.

It also fits the project's direction. "Longboxes understands your library, it
doesn't just pattern-match filenames" is a real differentiator over
ComicTagger's pure heuristics.

## Principles

These hold across every workstream:

- **The LLM is optional, layered on the heuristics.** `comicfn2dict` and the
  keyword classifier stay. With no LLM configured, Longboxes works exactly as
  it does today. With one configured, the answers get better. The heuristic is
  both the instant answer and the graceful-degradation path.
- **All LLM work is cached background enrichment.** One call per entity, the
  result stored, re-run only when the source text changes — the same SWR +
  revalidate-job pattern as cover hydration. Never synchronous, never in a
  request or match hot path. Serve the heuristic instantly; upgrade the stored
  verdict when the job lands.
- **Provider-agnostic.** The LLM client is a single generic OpenAI-compatible
  endpoint (`base_url` + `api_key` + `model`), not a named provider — local and
  hosted are the same code path, distinguished only by config. See *Provider
  model* below for the hosted-vs-local trade and the recommendation.
- **Deterministic and constrained.** Temperature 0, structured output
  (a JSON schema, validated and reprompted on failure — see *Provider
  model*), and the verdict cached so it's stable once computed. Store the raw
  model output so downstream resolution can re-run without re-calling the
  model.
- **The LLM verdict is a soft signal.** It drives badges, facets and display
  freely. Where it feeds something scoring-related or destructive, it's used
  conservatively (see workstream A).

## Provider model

The LLM client is **not** tied to a named provider. It is one generic
OpenAI-compatible chat client — the settings are just `base_url`, `api_key`
and `model`. That single shape covers a local runtime (Ollama, LM Studio,
llama.cpp's server, vLLM), aggregators (OpenRouter), and the hosted vendors
(including Anthropic via its compatibility endpoint). Local vs hosted
collapses into a config value; Longboxes assumes no particular service.
Structured output is provider-neutral too — request JSON, validate it against
the expected schema, reprompt once on failure — rather than depending on one
vendor's tool-use. The default is **off**: Phase 9 is optional and the
heuristics stand alone.

**Hosted vs local — the economics.** The usual "go local to avoid per-call
cost" logic doesn't apply here. The workload is tiny and one-time-per-entity:
a library is a few hundred to a few thousand volumes, each classified once and
cached, so the lifetime call count is on the order of a thousand. At that
volume a hosted API costs pennies for the whole library. A local model, by
contrast, is a *permanent* resource cost — Ollama plus a model is gigabytes of
RAM, realistically a GPU for tolerable speed, running alongside a
previously-lightweight Docker stack — to serve a bursty, one-time job. On
economics alone, hosted wins.

**Privacy is the real reason to choose local.** Each individual input — a CV
description, a filename — is low-sensitivity on its own; CV descriptions are
public. The *aggregate* is not. Sending Longboxes' classification and parsing
traffic to a hosted provider streams that provider a profile of the user's
library: which comics they own, which they're matching, which they search
for — what they read and have an interest in. For a self-hosted app, where
keeping that picture on your own box is often the whole point, that's a
legitimate reason to prefer a local model — not merely an ethos preference.
Local is therefore a first-class, fully supported path, not a grudging
fallback.

**Per-workstream reliability.** Model strength matters unevenly:

- *Volume classification (A)* and *filename parsing (B)* are constrained
  classification / extraction tasks. A small local model handles them well.
- *Collected-contents extraction (C)* — parsing "Volume 1 (vol. 1-3) Volume 2
  (vol. 4-6)…" into a clean structured breakdown — is more demanding. A small
  local model gets shaky here; a stronger model (a large local one, or a
  hosted one) earns its keep. A user on a small local model should expect A
  and B to be solid and C to be best-effort.

**Recommendation.** Default off; the config is a generic endpoint so the user
points Longboxes at whatever they have or prefer. For users without a strong
privacy preference, a hosted small model is the path of least resistance —
trivially cheap at this scale and reliable across all three workstreams. For
users who'd rather their collection never leave the server, a local model is
fully supported and is the right choice — strongest on workstreams A and B,
with C as best-effort unless the local model is a large one.

## Workstream A — Volume classification (format + language)

A background job per volume: build a prompt from name + deck + description +
count + publisher, ask for a structured verdict — `{format, language,
confidence}` — and store it on the volume.

- Schema: `cv_volumes.llm_format`, `llm_language`, `llm_confidence`,
  `llm_classified_at`, `llm_model` (or a `cv_volume_llm` sidecar table).
- `classify_cv_volume` becomes: return the LLM verdict when present, else the
  heuristic.
- Enqueued on volume ingest; re-enqueued when a revalidate changes the
  description.

**Format** — the LLM resolves exactly the cases the heuristic fumbles: the
Vagabond collected-editions line; an ongoing series whose description merely
mentions its TPBs.

**Language** — this is *not* a job for a statistical language detector.
ComicVine writes every description in English, even for manga, so `langdetect`
would report "English" for the whole catalog. The *edition's* language is a
semantic fact stated in the prose ("English translation of…", a Panini Italian
edition) plus the publisher. Extracting it is reading comprehension — squarely
the LLM's job.

**Conservative matcher integration.** The classification feeds the matcher's
format-mismatch penalty. An LLM is more accurate than the heuristic but also
occasionally confidently wrong, and a hallucinated "collection" would penalize
a correct single-issue match. So: the LLM verdict drives the badge / facet /
display freely, but the scoring penalty fires only when the LLM and the
heuristic **agree** (or only on high-confidence verdicts). A hallucination
must not be able to quietly corrupt matching.

Lowest-risk workstream; ship it first.

## Workstream B — Filename-parse fallback

**Decision: the LLM parses filenames as a fallback, not for every file.**

`comicfn2dict` (plus the `long_series` extractor) stays as the primary parser.
The LLM is escalated only when:

1. the cheap parser yields **no usable series** (nothing to search on), or
2. a file came back **UNMATCHED** and a re-match is requested — a bad filename
   parse is a prime suspect.

Why not run the LLM on every filename:

- **Files vastly outnumber volumes.** Volumes are a bounded set
  (hundreds–thousands); files can be tens of thousands. Per-file LLM calls are
  a far larger cost surface than per-volume — and most of it would be wasted,
  because most files are named sanely and `comicfn2dict` already nails them.
- The cheap parser is **free, fast, deterministic, and re-runnable** — the
  review queue re-parses on every load to pick up parser improvements. An LLM
  parse can't be re-run that cheaply, so it has to be cached on the file /
  match row.
- The LLM's value is the *messy minority* — scanlator naming, truncated
  subtitles, non-standard formats — which is exactly the "parser came up with
  nothing" set.

The escalation result is cached on the file so it isn't re-charged, and it's
wired into the review queue: an UNMATCHED file gets an "AI re-parse &
re-match" affordance, closing the loop — failed match → LLM re-reads the
filename → better signals → re-run the matcher.

## Workstream C — Collected-contents extraction (the net-new structure)

This is the part with no precedent. ComicVine has no first-class "this
collected edition reprints these issues" relationship — a TPB is just another
volume with its own issues. If the LLM reads a collected edition's description
and extracts what it collects, Longboxes can build that link itself.

The Vagabond description is the perfect example — it literally enumerates
"Volume 1 (vol. 1-3) Volume 2 (vol. 4-6) Volume 3 (vol. 7-9)…". An LLM turns
that into a per-collected-book breakdown: collected book 1 → source vol. 1–3,
book 2 → 4–6, and so on.

**Two steps:**

1. **Extraction.** The LLM reads the collected edition's description and
   returns a structured list: for each collected book, the range it reprints
   (issue numbers, or — for manga — original volume numbers) and the series it
   belongs to.
2. **Resolution.** A non-LLM step maps each extracted range to actual
   `cv_issues` / `cv_volumes` rows — series name + issue number → cv_id,
   reusing the matcher's `find_issue_by_number`. Best-effort: ranges that
   can't be resolved (uncached series, ambiguity) go to an unresolved bucket
   rather than being dropped.

**The structure** — a `collection_contents` table: rows linking a
collected-edition book to each original issue it reprints, with a confidence
and a source (LLM-extracted vs human-confirmed). The raw LLM extraction is
stored alongside, so resolution can re-run as more series get cached without
re-calling the model.

A design question to settle: Western TPBs collect *issues*; manga omnibuses
collect *volumes*. The extraction and the link table need to represent both —
either a polymorphic "source" reference or two link types.

**What the structure unlocks** (each is arguably its own follow-on feature):

- **Precise TPB matching.** Knowing "Vol. 2 collects #7–12" lets the matcher
  confirm a collected-edition *file* to the exact right volume — and the issue
  count in the range corroborates the file's page count. TPB matching is
  currently the matcher's weakest area; this addresses it head-on.
- **Collection coverage.** Owning a TPB that collects #1–6 means you own that
  content. The volume page's completeness ring could count collected editions
  toward coverage — "you have this arc, via the trade." A genuine
  library-management capability.
- **Bidirectional navigation.** From an issue: "collected in [Vol. 2]." From a
  collected edition: "contains #7–12 → [links]." Walking between the floppy
  series and the collected editions.
- **Duplicate-content detection.** Owning both the singles #1–6 and the
  "Vol. 1" TPB that collects them — Longboxes can surface that you hold the
  same content twice.

## Data model

- `cv_volumes` LLM fields (or a `cv_volume_llm` sidecar): `llm_format`,
  `llm_language`, `llm_confidence`, `llm_classified_at`, `llm_model`.
- A filename-parse cache field on `files` / `file_matches`: the LLM-derived
  parse, so escalation isn't re-charged.
- A `collection_contents` table: collected book → original issue / volume,
  with confidence + source, plus a raw-extraction store and an unresolved
  bucket.
- Alembic migrations for each.

## Settings (admin)

- LLM endpoint: `base_url`, `api_key`, `model` — a single OpenAI-compatible
  config (see *Provider model*). Empty / off by default; the user points it at
  a local runtime or a hosted provider.
- Per-workstream toggles (classification / filename fallback /
  collected-contents) so a user can enable just the cheap, safe one.

## Risks

- **Hallucination.** Structured output, temperature 0, heuristic cross-check.
  The verdict stays a soft signal; the matcher penalty requires LLM/heuristic
  agreement.
- **Cost surface.** Volumes and collection volumes are bounded — fine.
  Filenames are not — hence fallback-only.
- **Resolution failure** (workstream C). Links are best-effort; unresolved
  ranges stay visible rather than being silently dropped, and re-resolve as
  the cache fills.
- **Exposure of the library profile.** A hosted provider sees Longboxes'
  classification / parsing traffic — in aggregate, a picture of the user's
  collection and reading interests. The phase is optional and degrades to the
  heuristics; the local path (see *Provider model*) keeps that picture on the
  user's own server.
- **Determinism.** Cache every verdict; store raw output so re-resolution
  doesn't re-call the model.

## Sub-phases

- **9A — Provider plumbing.** The LLM-client abstraction, the settings, a
  structured-call helper. Provider-agnostic; no user-facing feature yet.
- **9B — Volume classification (format + language).** Background job, schema,
  `classify_cv_volume` made LLM-aware, conservative matcher integration. The
  first and lowest-risk win.
- **9C — Filename-parse fallback.** Matcher "Stage 2b" — escalation-only,
  cached, wired to the UNMATCHED review path.
- **9D — Collected-contents extraction + resolution + the
  `collection_contents` table.**
- **9E — The payoffs.** Precise TPB matching, collection coverage on the
  completeness ring, issue-to-collection navigation, duplicate detection. Big
  enough to be its own phase later.

## Out of scope

- **AI writing or editing metadata tags.** That's Phase 8's job, and it's
  deterministic — derived from confirmed CV data. The LLM informs
  *classification and extraction*; it does not freelance the tags written
  into archives.
- **AI generating descriptions or cover art.** Longboxes mirrors ComicVine; it
  doesn't invent metadata.
- **AI on every filename.** See workstream B.
