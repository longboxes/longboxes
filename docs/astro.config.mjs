// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

// Longboxes documentation site.
//
// Starlight provides the navigation, search (Pagefind), dark/light toggle,
// and content collection wiring. We override the default theme with
// `src/styles/longboxes.css` to bring the paper-and-burnt-orange palette
// from the landing page into the docs surface.
//
// To preview locally: `cd docs && npm install && npm run dev`.
// To build static output for deploy: `npm run build` (writes `dist/`).
export default defineConfig({
  site: "https://docs.longboxes.app",
  integrations: [
    starlight({
      title: "Longboxes",
      description:
        "Self-hosted comic library that reads your collection by story, not by file.",
      logo: {
        // The light/dark variants live next to this config; for now we
        // use one mark for both. Drop a dark variant into /docs/public
        // and split via `light` / `dark` later if needed.
        src: "./src/assets/longboxes-icon.svg",
        replacesTitle: false,
      },
      customCss: ["./src/styles/longboxes.css"],
      // Favicons + web-app manifest mirror the app's `<head>` (see
      // app/templates/base.html). Files live in `docs/public/` so Astro
      // serves them at root paths, matching what `site.webmanifest`
      // references.
      favicon: "/favicon.ico",
      head: [
        {
          tag: "link",
          attrs: {
            rel: "icon",
            type: "image/png",
            href: "/favicon-96x96.png",
            sizes: "96x96",
          },
        },
        {
          tag: "link",
          attrs: {
            rel: "icon",
            type: "image/svg+xml",
            href: "/favicon.svg",
          },
        },
        {
          tag: "link",
          attrs: {
            rel: "apple-touch-icon",
            sizes: "180x180",
            href: "/apple-touch-icon.png",
          },
        },
        {
          tag: "meta",
          attrs: {
            name: "apple-mobile-web-app-title",
            content: "Longboxes",
          },
        },
        {
          tag: "link",
          attrs: {
            rel: "manifest",
            href: "/site.webmanifest",
          },
        },
      ],
      social: {
        github: "https://github.com/longboxes/longboxes",
      },
      // Sidebar mirrors the user's journey: try → install → understand →
      // troubleshoot. The conceptual page ("How matching works") is the
      // hinge between "I have it running" and "I know why it does what
      // it does".
      sidebar: [
        {
          label: "Start here",
          items: [
            { label: "What is Longboxes?", slug: "index" },
            { label: "Quick start", slug: "quick-start" },
          ],
        },
        {
          label: "Setup",
          items: [
            { label: "Install", slug: "install" },
            { label: "First scan", slug: "first-scan" },
          ],
        },
        {
          label: "Understanding",
          items: [
            { label: "How matching works", slug: "matching" },
            { label: "Reading comics", slug: "reading" },
          ],
        },
        {
          label: "Help",
          items: [{ label: "Troubleshooting & FAQ", slug: "troubleshooting" }],
        },
      ],
      // Disable the built-in "edit this page" link; users aren't editing
      // the site, and the GH link in the social bar covers contributors.
      editLink: undefined,
      lastUpdated: true,
    }),
  ],
});
