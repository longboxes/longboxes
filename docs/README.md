# Longboxes documentation site

User-facing documentation for Longboxes, built with [Astro
Starlight](https://starlight.astro.build/). Deploys to
docs.longboxes.app (or wherever you point it).

Internal design docs (phase plans, future-looking notes) live in
`/design/` at the repo root — that material isn't published here.

## Develop

```bash
cd docs
npm install
npm run dev
```

Opens a hot-reloading preview at <http://localhost:4321>.

## Build

```bash
npm run build
```

Writes a static site to `dist/`. Deploy that directory anywhere that
serves static files (Cloudflare Pages, Netlify, GH Pages, S3, …).

## Structure

```
docs/
├── astro.config.mjs     # Starlight config: sidebar, integrations, theme
├── package.json         # npm deps (Astro + Starlight)
├── src/
│   ├── assets/          # Logo + images referenced by content
│   ├── content.config.ts
│   ├── content/docs/    # Every page lives here — one .md/.mdx per route
│   └── styles/
│       └── longboxes.css  # Brand colors, typography, theme overrides
└── tsconfig.json
```

## Writing pages

Each `.md` / `.mdx` in `src/content/docs/` becomes a route. Add to the
`sidebar` array in `astro.config.mjs` to make it appear in the nav.

Frontmatter:

```yaml
---
title: Page title
description: 1-2 sentence summary (used by search + meta tags)
---
```

The full content schema is documented in
[Starlight's frontmatter reference](https://starlight.astro.build/reference/frontmatter/).

## Theme

The custom theme in `src/styles/longboxes.css` overrides Starlight's
default CSS variables to match the landing page palette (paper +
burnt-orange accent, Fraunces + DM Sans). Both light and dark themes
are mapped. To change a brand color, edit the `--lb-*` variables at
the top of that file.
