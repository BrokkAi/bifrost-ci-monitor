import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (
            id.includes("/node_modules/recharts/") ||
            id.includes("/node_modules/d3-") ||
            id.includes("/node_modules/victory-vendor/")
          )
            return "charts";
          if (id.includes("/node_modules/@tanstack/")) return "data";
          if (
            id.includes("/node_modules/radix-ui/") ||
            id.includes("/node_modules/@radix-ui/")
          )
            return "primitives";
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8770",
      "/auth": "http://127.0.0.1:8770",
    },
  },
});
