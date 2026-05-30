/**
 * Header search dropdown — debounced live search backed by /search/live.
 *
 * Returns an Alpine x-data object. Attach with `x-data="setupSearchDropdown()"`
 * on the form that wraps the input + dropdown panel. The panel reads
 * `groups`, `total`, `loading`, `open`, and `activeKey` from this state.
 *
 * Behavior:
 * - `q` is the input value; @input.debounce.200ms calls fetchResults().
 * - Requests under MIN_QUERY_LENGTH never hit the network.
 * - In-flight requests are aborted when a newer keystroke comes in, so
 *   a slow earlier response can't clobber a fast later one.
 * - Down/Up arrow walks the flattened hit list; Enter on a highlighted
 *   row navigates to its detail URL; Enter with no highlight lets the
 *   wrapping <form action="/search"> submit naturally (full results
 *   page).
 * - Escape closes; click-outside (wired on the form) closes.
 */
function setupSearchDropdown() {
  // Group order in the dropdown — mirrors the /search page so users
  // see the same shape in both surfaces. Each tuple is [label, key].
  // Keep in sync with the JSON shape /search/live returns.
  const GROUPS = [
    ["Volumes", "volumes"],
    ["Local volumes", "local_volumes"],
    ["Issues", "issues"],
    ["Characters", "characters"],
    ["Creators", "creators"],
    ["Teams", "teams"],
    ["Story arcs", "arcs"],
  ];

  return {
    q: "",
    open: false,
    loading: false,
    total: 0,
    // Default min length matches MIN_QUERY_LENGTH in the service.
    // /search/live also returns the real value in `min_query_length`
    // which we adopt on the first response.
    minLength: 2,
    groups: {
      volumes: [],
      local_volumes: [],
      issues: [],
      characters: [],
      creators: [],
      teams: [],
      arcs: [],
    },
    // Index into the flattened hit list for keyboard nav. -1 means
    // "no highlight; Enter submits the form to /search".
    activeIndex: -1,
    GROUPS,
    _controller: null,

    /** Flattened hit list — used by keyboard nav + Enter handling. */
    get flatHits() {
      const out = [];
      for (const [, key] of GROUPS) {
        for (const hit of this.groups[key] || []) {
          out.push({ ...hit, _groupKey: key });
        }
      }
      return out;
    },

    /** True when the current view should show any hits at all. */
    get hasResults() {
      return this.total > 0;
    },

    /** Stable highlight identifier — kind + key. Reset to '' when no
     *  active row, so the template's :class checks fail cleanly. */
    get activeHitKey() {
      const h = this.flatHits[this.activeIndex];
      return h ? `${h._groupKey}:${h.key}` : "";
    },

    /** Debounce target. Aborts any in-flight request first. */
    async fetchResults() {
      const q = (this.q || "").trim();
      if (q.length < this.minLength) {
        this._reset();
        this.open = true; // show the "type N chars" hint
        return;
      }
      if (this._controller) this._controller.abort();
      this._controller = new AbortController();
      this.loading = true;
      this.open = true;
      try {
        const resp = await fetch(
          "/search/live?q=" + encodeURIComponent(q),
          { signal: this._controller.signal },
        );
        if (!resp.ok) return;
        const data = await resp.json();
        this.groups = data.groups || this.groups;
        this.total = data.total || 0;
        if (typeof data.min_query_length === "number") {
          this.minLength = data.min_query_length;
        }
        this.activeIndex = -1;
      } catch (e) {
        // Abort errors are expected when typing fast. Anything else
        // we swallow rather than surfacing an alert — the input still
        // works for the full-page submit.
      } finally {
        this.loading = false;
      }
    },

    /** Focus handler — reopens the panel if there's already content. */
    onFocus() {
      if (this.q && this.q.trim().length >= this.minLength) {
        this.open = true;
        if (this.total === 0) this.fetchResults();
      } else if (this.q) {
        this.open = true;
      }
    },

    /** Hide the dropdown without clearing the query. Used by Escape +
     *  click-outside; the input stays populated so the user can resume. */
    close() {
      this.open = false;
      this.activeIndex = -1;
    },

    moveDown() {
      if (!this.open) {
        this.open = true;
        return;
      }
      const n = this.flatHits.length;
      if (n === 0) return;
      this.activeIndex = (this.activeIndex + 1) % n;
    },

    moveUp() {
      if (!this.open) return;
      const n = this.flatHits.length;
      if (n === 0) return;
      this.activeIndex = this.activeIndex <= 0 ? n - 1 : this.activeIndex - 1;
    },

    /** Enter — navigate the highlighted row if there is one; otherwise
     *  the wrapping form submits to /search?q=... (default behavior). */
    onEnter(e) {
      const h = this.flatHits[this.activeIndex];
      if (h) {
        e.preventDefault();
        window.location.href = h.detail_url;
      }
    },

    _reset() {
      this.total = 0;
      this.groups = {
        volumes: [],
        local_volumes: [],
        issues: [],
        characters: [],
        creators: [],
        teams: [],
        arcs: [],
      };
      this.activeIndex = -1;
    },
  };
}

// Expose globally so x-data can find it. (Same pattern as
// setupAutoRefresh / setupPagination / setupTabs.)
window.setupSearchDropdown = setupSearchDropdown;
