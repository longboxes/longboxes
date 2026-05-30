// Starlight uses Astro's content collections to load MDX/MD files.
// The default schema is fine for our needs; if we want custom
// frontmatter fields later (e.g. a "phase" tag), this is the seam.
import { defineCollection } from "astro:content";
import { docsLoader } from "@astrojs/starlight/loaders";
import { docsSchema } from "@astrojs/starlight/schema";

export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
};
