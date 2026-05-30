// Generic auto-refresh helper for pages that background-hydrate
// entities (volumes, issues, arcs, etc.) via revalidate jobs.
//
// Server-side, the page renders each unhydrated entity with two
// attributes on its outermost element:
//   data-pending-id="<entity cv_id>"
//   data-hydrated="false"
// Hydrated entries set ``data-hydrated="true"`` (or omit the
// attributes entirely).
//
// Server-side, a hydration endpoint accepts ?ids=N,M,O and returns:
//   {
//     "swaps":         [{target_id: "...", html: "..."}, ...],
//     "completed_ids": [N, M, ...]
//   }
// Each ``swap`` describes one DOM replacement: find the element by
// id, swap in the provided HTML. ``completed_ids`` lists the entity
// IDs the client can drop from its pending set; usually equal to
// the IDs that produced at least one swap, but the server is free
// to mark an entity completed without emitting swaps (e.g., when
// the entity's hydration failed and we want the client to stop
// asking).
//
// Alpine usage:
//
//   x-data="{
//     ...setupAutoRefresh({ endpoint: '/foo/hydration' }),
//     /* page-specific state + methods */
//     init() {
//       this.scanPending();   // pick up initial pending IDs
//       /* other init steps */
//     }
//   }"
//
// After appending new items to the page (infinite scroll / load
// more), call ``this.scanPending()`` again to capture the new
// pending IDs.
window.setupAutoRefresh = function ({
  endpoint,
  pollIntervalMs = 3000,
  maxAttempts = 40,
} = {}) {
  return {
    pendingIds: new Set(),
    // ``pendingCount`` shadows ``pendingIds.size`` as a plain
    // integer so Alpine's reactivity picks up changes — Set
    // mutations don't reliably trigger DOM updates through
    // Alpine's proxy layer. Anything that mutates ``pendingIds``
    // must also call ``_arSyncPendingCount()`` to keep the two
    // in sync.
    pendingCount: 0,
    // Optional ``queue_status`` snapshot returned by some endpoints
    // (e.g. the Confirm Volume covers endpoint) describing where
    // the responsible background job currently sits — running,
    // queued at position N, scheduled with a cooldown, etc. Pages
    // that surface this via the hydration toast pick it up
    // reactively; endpoints that don't return it leave this null
    // and the toast falls back to the plain "N items loading"
    // form.
    queueStatus: null,
    _arPollHandle: null,
    _arPollAttempts: 0,

    // Sync ``pendingCount`` with the underlying Set. Called after
    // every mutation; callers from setupPagination and from each
    // page's ``init()`` also call this so DOM bindings
    // (toast badge, etc.) see the change.
    _arSyncPendingCount() {
      this.pendingCount = this.pendingIds.size;
    },

    // Rebuild ``pendingIds`` from whatever's currently in the DOM
    // marked ``data-hydrated='false'``. Cheap (handful of nodes
    // per page) and idempotent — safe to call after any DOM
    // mutation that might have added or removed pending entries.
    //
    // IDs are stored as raw strings so callers can use composite
    // tokens ("character:21599") for pages where the cv_id alone
    // isn't enough to identify the entity (e.g. /search renders
    // characters / creators / teams whose cv_id namespaces overlap).
    // The library page uses plain integer strings ("12345") and its
    // endpoint returns ints in ``completed_ids``; we coerce both
    // sides via ``String(...)`` below so the comparison still
    // matches.
    scanPending() {
      const els = document.querySelectorAll(
        "[data-pending-id][data-hydrated='false']"
      );
      const next = new Set();
      els.forEach((el) => {
        const raw = el.dataset.pendingId;
        if (raw) next.add(String(raw));
      });
      this.pendingIds = next;
      this._arSyncPendingCount();
      if (this.pendingIds.size > 0 && !this._arPollHandle) {
        this._arStartPolling();
      } else if (this.pendingIds.size === 0) {
        this._arStopPolling();
      }
    },

    _arStartPolling() {
      this._arPollAttempts = 0;
      this._arPollHandle = setInterval(
        () => this._arPollOnce(),
        pollIntervalMs
      );
    },

    _arStopPolling() {
      if (this._arPollHandle) {
        clearInterval(this._arPollHandle);
        this._arPollHandle = null;
      }
    },

    // Fire ``auto-refresh-complete`` exactly once when the pending
    // set has fully drained (NOT on max-attempts timeout — that's a
    // give-up, not a completion). Pages that need a full re-render
    // for views the per-element swap can't reach (e.g., the Arcs
    // view of the volume page, which intentionally renders with
    // duplicate cv_ids across per-arc shelves) listen via
    // ``x-on:auto-refresh-complete``.
    _arFireComplete() {
      if (this._arCompleteFired) return;
      this._arCompleteFired = true;
      const root = this.$el;
      if (root && typeof root.dispatchEvent === "function") {
        root.dispatchEvent(
          new CustomEvent("auto-refresh-complete", { bubbles: true })
        );
      }
    },

    async _arPollOnce() {
      this._arPollAttempts += 1;
      if (this.pendingIds.size === 0) {
        this._arStopPolling();
        this._arFireComplete();
        return;
      }
      if (this._arPollAttempts > maxAttempts) {
        this._arStopPolling();
        return;
      }
      try {
        // encodeURIComponent each token so composite IDs like
        // "character:21599" survive the trip through reserved-URL
        // characters; integer IDs come through untouched.
        const ids = [...this.pendingIds].map(encodeURIComponent).join(",");
        const sep = endpoint.includes("?") ? "&" : "?";
        const url = `${endpoint}${sep}ids=${ids}`;
        const res = await fetch(url);
        if (!res.ok) return;
        const data = await res.json();
        // Apply swaps. Wrap-parent trick handles <tr> needing
        // tbody, <li> needing ul, etc. without configuration: we
        // just inspect the existing element's parentNode.
        //
        // querySelectorAll instead of getElementById: the Confirm
        // Volume page can render the same matched-issue cv_id in
        // multiple file rows (two files matched to the same issue),
        // which produces duplicate ids in the DOM. getElementById
        // only returns the first such element, leaving every other
        // row's placeholder forever pending. With querySelectorAll
        // + cloneNode, every occurrence of an id is swapped, and
        // pages with unique ids (the vast majority) see identical
        // behaviour — a single-element NodeList replaced once.
        for (const swap of data.swaps || []) {
          if (!swap || !swap.target_id) continue;
          const selector = `[id='${(window.CSS && CSS.escape)
            ? CSS.escape(swap.target_id)
            : swap.target_id}']`;
          const oldEls = document.querySelectorAll(selector);
          if (oldEls.length === 0) continue;
          const parentTag =
            oldEls[0].parentNode && oldEls[0].parentNode.tagName
              ? oldEls[0].parentNode.tagName.toLowerCase()
              : "div";
          const wrap = document.createElement(parentTag);
          wrap.innerHTML = swap.html || "";
          const template = wrap.firstElementChild;
          if (!template) continue;
          oldEls.forEach((el) => {
            const fresh = template.cloneNode(true);
            el.replaceWith(fresh);
            // Initialise Alpine on the newly-swapped subtree. Without
            // this, x-show / x-data / x-bind directives on the swapped
            // markup never become reactive, so e.g. the publisher and
            // format filters on /search?kind=volumes don't hide rows
            // that were hydrated after the page first rendered.
            // initTree is safe to call on nodes that contain no Alpine
            // directives (no-op) and idempotent on already-initialised
            // trees.
            if (window.Alpine && typeof window.Alpine.initTree === "function") {
              try {
                window.Alpine.initTree(fresh);
              } catch (e) {
                // Don't let a bad swap kill the rest of the tick.
                if (window.console && console.warn) {
                  console.warn("auto-refresh: Alpine.initTree failed", e);
                }
              }
            }
          });
        }
        // Drop completed entity IDs from the pending set. Server
        // values may arrive as either ints (legacy /library/hydration)
        // or strings (/search/hydration's composite "kind:cv_id"
        // tokens); String() normalizes both to match the strings
        // stored by scanPending().
        const completedThisTick = (data.completed_ids || []).length;
        for (const completedId of data.completed_ids || []) {
          this.pendingIds.delete(String(completedId));
        }
        this._arSyncPendingCount();
        // Capture the optional queue_status snapshot if the
        // endpoint returns one. Cleared back to null when the
        // server omits it (don't leak stale state across polls).
        this.queueStatus = data.queue_status || null;
        // Reset the attempt counter when productive work happened
        // this tick. maxAttempts is intended as a "stop after a
        // long stretch of nothing" guard, not "give up after a
        // fixed wall-clock window" — and on a rate-limited library
        // covers can take many minutes to drain even though the
        // hydration is actively making forward progress. Without
        // this reset, an interactive volume-confirm page with 20
        // covers + a 13k-job backlog hits the original maxAttempts
        // cap (~2 minutes) after only a couple of covers have
        // landed and silently stops polling.
        if (completedThisTick > 0) {
          this._arPollAttempts = 0;
        }
        if (this.pendingIds.size === 0) {
          this._arStopPolling();
          this._arFireComplete();
        }
      } catch (e) {
        // Transient failures fine — keep pending, retry next tick.
        // A consistently-failing endpoint will eventually hit the
        // attempt cap and stop polling.
      }
    },
  };
};
