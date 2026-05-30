// setupTabs — Alpine x-data factory for a tabbed entity-page section.
//
// Tabs switch client-side (instant), but the active tab is mirrored to
// the URL hash so it survives the full-page reloads that in-tab
// pagination triggers. `tabIds` is the list of tab ids; the first is
// the default. Panels are `<div x-show="tab === '<id>'">` siblings of
// the tab bar; tab buttons call `select('<id>')`.
function setupTabs(tabIds) {
  return {
    tabs: tabIds,
    tab: tabIds[0],
    init() {
      const h = (window.location.hash || "").replace("#", "");
      if (this.tabs.includes(h)) {
        this.tab = h;
      }
      // Land on the hash target once the panel is visible — covers a
      // pagination reload whose links carry the panel's anchor.
      if (h) {
        this.$nextTick(() => {
          const el = document.getElementById(h);
          if (el) { el.scrollIntoView(); }
        });
      }
    },
    select(name) {
      this.tab = name;
      history.replaceState(null, "", "#" + name);
    },
  };
}
