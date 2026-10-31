import { defineConfig } from "cf/config";

export default defineConfig({
  worker: {
    name: "nutmap",
    compatibilityDate: "2026-10-23",
    observability: {
      enabled: true,
    },
    assets: {
      notFoundHandling: "none",
    },
  },
});
