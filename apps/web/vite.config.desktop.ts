/**
 * Desktop static build config — Tauri 桌面版使用。
 *
 * 与网页版 (vite.config.ts, adapter-node) 同一份源码, 区别:
 *   - adapter-static: 产出纯静态文件, 由 Tauri WebView 直接加载
 *   - server 路由 (+server.ts) 在构建前由 scripts/build-desktop-web.sh 临时移走
 *     (adapter-static 不允许 server 路由; 桌面版 API 走 VITE_VEYA_ENDPOINT 直连后端)
 *   - 构建注入 VITE_VEYA_ENDPOINT=http://127.0.0.1:8767 (桌面后端默认端口)
 */
import adapter from "@sveltejs/adapter-static";
import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [
    sveltekit({
      compilerOptions: {
        runes: ({ filename }) =>
          filename.split(/[/\\]/).includes("node_modules") ? undefined : true,
      },
      adapter: adapter({ fallback: "index.html", pages: "build-desktop" }),
    }),
    tailwindcss(),
  ],
  optimizeDeps: { exclude: ["@veya/ui"] },
  define: {
    "import.meta.env.VITE_VEYA_ENDPOINT": JSON.stringify(
      process.env.VITE_VEYA_ENDPOINT ?? "http://127.0.0.1:8767",
    ),
  },
});
