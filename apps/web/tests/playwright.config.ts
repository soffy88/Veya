// @ts-nocheck — @playwright/test is installed only for the e2e run, not for svelte-check.
import { defineConfig, devices } from "@playwright/test";

/**
 * Config for the W11 group B smoke test (see ./bot_landing.spec.ts).
 *
 * The app is built and served by adapter-node, so the test drives the real
 * bundle rather than a dev server — the same artifact production runs.
 */
export default defineConfig({
	testDir: ".",
	timeout: 30_000,
	expect: { timeout: 5_000 },
	fullyParallel: true,
	reporter: [["list"]],
	use: {
		baseURL: "http://127.0.0.1:4174",
		trace: "on-first-retry",
	},
	webServer: {
		command: "pnpm run build && PORT=4174 node build",
		cwd: "..",
		url: "http://127.0.0.1:4174",
		reuseExistingServer: !process.env.CI,
		timeout: 180_000,
	},
	projects: [
		{
			name: "mobile-chromium",
			use: { ...devices["Pixel 5"] },
		},
	],
});
