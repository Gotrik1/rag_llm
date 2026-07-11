import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    extensions: [".ts", ".tsx", ".js", ".jsx", ".json"]
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts"]
  }
});
