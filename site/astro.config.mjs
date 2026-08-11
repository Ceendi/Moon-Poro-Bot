import { defineConfig } from "astro/config";

export default defineConfig({
  site: process.env.PUBLIC_SITE_URL ?? "http://localhost:4321",
  output: "static",
  build: { format: "directory" },
  compressHTML: true,
});
