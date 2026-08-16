import adapter from "@sveltejs/adapter-node";
import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  // vite dev 不会把非 VITE_ 前缀的变量注入 process.env, 而 SSR 侧的
  // api/v1、api/stream、legacy 路由读的正是 process.env.VEYA_GATEWAY/VEYA_LEGACY。
  // 手动从 .env 回填这两个, 让 apps/web/.env 在 dev 与生产下行为一致。
  const env = loadEnv(mode, process.cwd(), "");
  for (const key of ["VEYA_GATEWAY", "VEYA_LEGACY"] as const) {
    if (process.env[key] === undefined && env[key]) process.env[key] = env[key];
  }
  return {
  plugins: [
    sveltekit({
      compilerOptions: {
        // Force runes mode for the project, except for libraries. Can be removed in svelte 6.
        runes: ({ filename }) => (filename.split(/[/\\]/).includes("node_modules") ? undefined : true),
      },
      adapter: adapter(),
    }),
    tailwindcss(),
  ],
  // workspace-linked Svelte source package → never pre-bundle
  optimizeDeps: {
    exclude: ["@veya/ui"],
  },
  };
});
