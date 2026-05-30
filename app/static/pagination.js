// Pagination state helper for entity pages that page through a long
// flat list of items (the volume and arc pages, currently). Returns
// an object intended to be spread into an Alpine ``x-data`` next to
// ``setupAutoRefresh`` — the two helpers compose:
//
//   setupAutoRefresh    drives the poll-and-swap hydration loop.
//   setupPagination     manages start / count window + page-size
//                       arithmetic + background-hydration on each
//                       page change.
//
// Dependencies (provided by setupAutoRefresh; spread it FIRST so
// these names exist on ``this``):
//   * ``pendingIds: Set<number>``
//   * ``_arCompleteFired: boolean``
//   * ``_arPollHandle: number | null``
//   * ``_arStartPolling()``
//
// Alpine usage:
//
//   x-data="{
//     ...setupAutoRefresh({ endpoint: '/.../issues/hydration' }),
//     ...setupPagination({
//       initialStart: 0,
//       pageSize: 15,
//       total: 100,
//       issueIds: [1, 2, 3, ...],
//       hydrateEndpoint: '/.../hydrate-issues',
//       onPaginate() { this.fetchRail(); }   // optional
//     }),
//     view: 'list',
//     init() {
//       this.pendingIds = new Set([/* server-provided pending IDs */]);
//       if (this.pendingIds.size > 0) this._arStartPolling();
//     }
//   }"
//
// Exposed state:
//   start, count, pageSize, total, issueIds (all from the config).
//
// Exposed methods:
//   loadEarlier()  — slide the window back by ``pageSize`` rows.
//   loadMore()     — extend the window forward by ``pageSize`` rows.
//   showAll()      — open the window to the full ``total``.
//   visibleEnd()   — clamped ``start + count``.
//   hydrateIds(ids)      — fire-and-forget POST to ``hydrateEndpoint``.
//   hydrateRange(lo, hi) — slice ``issueIds[lo:hi]``, post, and add
//                          to ``pendingIds`` so auto-refresh picks
//                          them up. Restarts the poller if it had
//                          drained, and resets the
//                          ``auto-refresh-complete`` one-shot so a
//                          fresh expansion gets its own polling
//                          budget and completion event.
//
// ``onPaginate`` is called after each load* / showAll mutation, with
// ``this`` bound to the Alpine component — pages use it to refresh
// view-scoped chrome that isn't reactive to start/count alone (the
// volume page's rail SVG fragment, for example).
window.setupPagination = function ({
  initialStart = 0,
  initialCount = null,    // defaults to pageSize
  pageSize,
  total,
  issueIds,
  hydrateEndpoint,
  onPaginate = null,
} = {}) {
  return {
    start: initialStart,
    count: initialCount !== null ? initialCount : pageSize,
    pageSize: pageSize,
    total: total,
    issueIds: issueIds,

    async hydrateIds(ids) {
      // Fire-and-forget. Server filters to stubs / bulk-only rows,
      // so callers can oversend; already-full issues skip the
      // enqueue. Swallowed errors mean the visible window stays
      // un-hydrated this cycle — the page still renders, just
      // without the per-row swaps that polling produces.
      if (!ids || !ids.length) return;
      try {
        await fetch(hydrateEndpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ issue_cv_ids: ids }),
        });
      } catch (e) {
        /* swallow */
      }
    },

    hydrateRange(from, to) {
      if (from >= to) return;
      const ids = this.issueIds.slice(from, to);
      this.hydrateIds(ids);
      // Track the newly-enqueued IDs in the pending set so polling
      // picks up their hydration. Restart polling if it had
      // drained. Reset the per-cycle attempt counter and the
      // one-shot complete flag so a fresh expansion gets its own
      // ~2-minute polling budget AND can fire its OWN
      // ``auto-refresh-complete`` event when the new batch drains.
      for (const id of ids) this.pendingIds.add(id);
      this._arSyncPendingCount();
      if (this.pendingIds.size > 0) {
        this._arCompleteFired = false;
        if (!this._arPollHandle) this._arStartPolling();
      }
    },

    loadEarlier() {
      const newStart = Math.max(0, this.start - this.pageSize);
      const oldStart = this.start;
      this.count += oldStart - newStart;
      this.start = newStart;
      this.hydrateRange(newStart, oldStart);
      if (onPaginate) onPaginate.call(this);
    },

    loadMore() {
      const prevEnd = this.visibleEnd();
      this.count = Math.min(
        this.total - this.start,
        this.count + this.pageSize
      );
      this.hydrateRange(prevEnd, this.visibleEnd());
      if (onPaginate) onPaginate.call(this);
    },

    showAll() {
      const prevStart = this.start;
      const prevEnd = this.visibleEnd();
      this.start = 0;
      this.count = this.total;
      this.hydrateRange(0, prevStart);
      this.hydrateRange(prevEnd, this.total);
      if (onPaginate) onPaginate.call(this);
    },

    visibleEnd() {
      return Math.min(this.start + this.count, this.total);
    },

    // ---- Range-tab navigation -------------------------------------
    // Used by the volume page's divider-tab strip (``range_tabs`` in
    // ``_pagination.html``). All are methods, not getters: the helper
    // object is spread into an Alpine ``x-data`` and a spread would
    // evaluate getters once into stale static values.

    // Total pages, and the page the current window sits on.
    pageCount() {
      return Math.max(1, Math.ceil(this.total / this.pageSize));
    },
    currentPage() {
      return Math.floor(this.start / this.pageSize);
    },
    // True once the window has been opened past a single page — i.e.
    // the "Show all" tab is active. A jumped-to page keeps
    // ``count === pageSize``; ``showAll()`` sets it to the full total.
    showingAll() {
      return this.count > this.pageSize;
    },
    // Human label for page p, e.g. "16-30" (last page clamps to total).
    pageLabel(p) {
      return (p * this.pageSize + 1) + "-"
        + Math.min((p + 1) * this.pageSize, this.total);
    },
    // Jump straight to page p — REPLACES the window with that page's
    // single ``pageSize`` slice (unlike loadMore/loadEarlier, which
    // accrete). Hydrates the new slice and repaints via ``onPaginate``.
    jumpToPage(p) {
      const target = Math.max(0, Math.min(p, this.pageCount() - 1));
      this.start = target * this.pageSize;
      this.count = this.pageSize;
      this.hydrateRange(this.start, this.visibleEnd());
      if (onPaginate) onPaginate.call(this);
    },
    // "Show More" tab — flip the window a chunk (5 pages) in ``dir``
    // ('left' | 'right') and land there. ``jumpToPage`` clamps.
    flipPages(dir) {
      this.jumpToPage(this.currentPage() + (dir === "left" ? -5 : 5));
    },
    // Windowed tab descriptors for the divider strip:
    //   { kind: 'page', page: N }   — a pageSize-issue range tab
    //   { kind: 'more', dir: ... }  — a 'Show More' flip tab
    // Page 0 is always present; a 4-wide window tracks the current
    // page; 'more' tabs mark the hidden gaps. <= 5 pages → all shown.
    pageTabs() {
      const total_pages = this.pageCount();
      if (total_pages <= 5) {
        return Array.from(
          { length: total_pages },
          (_, p) => ({ kind: "page", page: p })
        );
      }
      const c = this.currentPage();
      const tabs = [{ kind: "page", page: 0 }];
      const ws = Math.max(1, Math.min(c - 1, total_pages - 4));
      const we = ws + 3;
      if (ws > 1) tabs.push({ kind: "more", dir: "left" });
      for (let p = ws; p <= we; p++) tabs.push({ kind: "page", page: p });
      if (we < total_pages - 1) tabs.push({ kind: "more", dir: "right" });
      return tabs;
    },
  };
};
