import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    plugins: [react()],
    resolve: {
      extensions: [".ts", ".tsx", ".js", ".jsx", ".json"]
    },
    build: {
      chunkSizeWarningLimit: 650,
      rollupOptions: {
        output: {
          manualChunks: {
            react: ["react", "react-dom"],
            markdown: [
              "react-markdown",
              "remark-gfm",
              "remark-math",
              "rehype-katex",
              "rehype-raw",
              "rehype-sanitize"
            ],
            assistant: ["@assistant-ui/react"]
          }
        }
      }
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": {
          target: env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8080",
          changeOrigin: true
        }
      }
    }
  };
});
