// Cover-fit — letterbox landscape covers.
//
// Comic covers are portrait (~2:3), and the UI renders every cover in a
// 2:3 box with `object-fit: cover`, which center-crops the image to
// fill. That's right for a normal portrait cover, but a landscape cover
// — common for digital-first comics — would be cropped to a useless
// vertical sliver.
//
// This finds covers whose *loaded* image is wider than tall and flips
// them to `object-fit: contain`, so the whole image shows, letterboxed
// against the cover box's own background. A cover's true shape can't be
// known ahead of time (ComicVine image payloads carry no dimensions),
// so detection is per-image, on load — and re-runs for covers that
// arrive later via infinite scroll or stub-hydration swaps.
//
// Cover <img>s are identified structurally: they sit inside an element
// with an inline `aspect-ratio` style (the 2:3 cover box). No per-
// template markup is needed.

(function () {
  "use strict";

  const COVER_IMG_SELECTOR = '[style*="aspect-ratio"] img';

  // Switch a cover to letterbox mode once it has loaded, if its natural
  // dimensions are landscape (wider than tall). Inline `object-fit`
  // overrides the element's `object-cover` utility class.
  function adjust(img) {
    const w = img.naturalWidth;
    const h = img.naturalHeight;
    if (w > 0 && h > 0 && w > h) {
      img.style.objectFit = "contain";
    }
  }

  // Process one cover <img>: adjust now if it's already decoded,
  // otherwise once it loads. `coverFitDone` keeps a re-scan idempotent.
  function process(img) {
    if (img.dataset.coverFitDone) return;
    img.dataset.coverFitDone = "1";
    if (img.complete) {
      adjust(img);
    } else {
      img.addEventListener("load", function () { adjust(img); }, {
        once: true,
      });
    }
  }

  function scan(root) {
    if (!root.querySelectorAll) return;
    root.querySelectorAll(COVER_IMG_SELECTOR).forEach(process);
  }

  function init() {
    scan(document);
    // Cards appended by infinite scroll, or swapped in when a stub
    // volume hydrates, bring their own cover <img>s — pick those up as
    // they're inserted. `addedNodes` is empty for the attribute-only
    // mutations Alpine makes constantly, so the callback is cheap.
    const observer = new MutationObserver(function (mutations) {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node.nodeType !== 1) continue;
          if (node.matches && node.matches(COVER_IMG_SELECTOR)) {
            process(node);
          }
          scan(node);
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
