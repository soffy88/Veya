import adapter from "@sveltejs/adapter-node";
import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, loadEnv } from "vite";
import { join, relative } from "path";

function veyaSourceStampPlugin() {
  const isDev = process.env.NODE_ENV !== "production";
  if (!isDev) return { name: "veya-source-stamp", enforce: "pre" as const };

  return {
    name: "veya-source-stamp",
    enforce: "pre" as const,
    async transform(code: string, id: string) {
      if (!id.endsWith(".svelte")) return;
      if (id.includes("node_modules")) return;

      const projectRoot = process.cwd();
      const relPath = relative(projectRoot, id);
      const lines = code.split("\n");

      // Find all element opening tags and add data-veya-src
      let transformed = code;
      const tagRegex = /<([a-zA-Z][a-zA-Z0-9:-]*)(\s[^>]*)?>/g;
      let match: RegExpExecArray | null;
      const replacements: { start: number; end: number; replacement: string }[] = [];

      while ((match = tagRegex.exec(code)) !== null) {
        const tagName = match[1];
        const attrs = match[2] || "";
        // Skip void elements and special Svelte elements
        if (
          ["script", "style", "title", "meta", "link", "head", "html", "body", "svelte:head", "svelte:window", "svelte:body", "svelte:options"].includes(tagName)
        ) continue;
        // Skip if already has data-veya-src
        if (attrs.includes("data-veya-src")) continue;
        // Find line number for this tag
        const beforeMatch = code.slice(0, match.index);
        const lineNumber = beforeMatch.split("\n").length;
        const stamp = ` data-veya-src="${relPath}:${lineNumber}"`;
        const replacement = `<${tagName}${attrs}${stamp}>`;
        replacements.push({
          start: match.index,
          end: match.index + match[0].length,
          replacement,
        });
      }

      if (replacements.length === 0) return;

      // Apply replacements from end to start to preserve indices
      let result = code;
      for (const r of replacements.reverse()) {
        result = result.slice(0, r.start) + r.replacement + result.slice(r.end);
      }

      return {
        code: result,
        map: null,
      };
    },
  };
}

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
    veyaSourceStampPlugin(),
  ],
  // workspace-linked Svelte source package → never pre-bundle
  optimizeDeps: {
    exclude: ["@veya/ui"],
  },
  };
});
