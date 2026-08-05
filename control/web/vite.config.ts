import {defineConfig} from "vitest/config";
import react from "@vitejs/plugin-react";
export default defineConfig({plugins: [react()], test: {
  environment: "jsdom",
  environmentOptions: {jsdom: {url: process.env.DGX_LIVE_ORIGIN ?? "http://localhost"}},
  setupFiles: "./src/test-setup.ts",
  globals: true,
  exclude: ["e2e/**", "node_modules/**"],
}});
