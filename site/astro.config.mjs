import { defineConfig } from "astro/config";

export default defineConfig({
  site: process.env.PUBLIC_SITE_URL ?? "http://localhost:4321",
  output: "static",
  // The production CSP (deploy/Caddyfile) allows only same-origin stylesheets and fonts,
  // so nothing may be inlined into the HTML or CSS.
  build: { format: "directory", inlineStylesheets: "never" },
  vite: { build: { assetsInlineLimit: 0 } },
  compressHTML: true,
});
