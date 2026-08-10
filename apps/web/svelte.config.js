import adapter from "@sveltejs/adapter-node";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

/** @type {import('@sveltejs/kit').Config} */
export default {
	preprocess: vitePreprocess(),
	kit: {
		adapter: adapter(),
		// 内部工具: 大文件上传 (≤100MB) 经 SvelteKit 代理。默认 body 上限 512KB
		// → 413; 放宽到 200MB。后端 FastAPI 有 auth + 100MB 硬上限。
		bodySizeLimit: "200mb",
		// 文件上传用 octet-stream (非 form) 已绕开 CSRF; 关闭兜底防其他 form 场景
		csrf: { checkOrigin: false },
	},
};
