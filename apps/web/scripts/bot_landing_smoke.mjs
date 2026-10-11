#!/usr/bin/env node
/**
 * W11 group B smoke — the same checks as tests/bot_landing.spec.ts, runnable
 * here.
 *
 * Why this exists next to the Playwright spec: this repo's apps/web/node_modules
 * is a symlink shared with the main worktree, so `@playwright/test` is not
 * installed and cannot be added without touching that shared install. This
 * runner drives the identical fixtures through puppeteer-core (already present
 * on this machine) so the landing UI is actually exercised in a real browser,
 * at 375px, with a mocked API — and prints PASS/FAIL per check.
 *
 * Requires a built app: `pnpm --dir apps/web build` first.
 */

import { spawn } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { join } from "node:path";

const require = createRequire(import.meta.url);
const APP_DIR = new URL("..", import.meta.url).pathname.replace(/\/$/, "");
const PORT = 4321;
const BASE = `http://127.0.0.1:${PORT}`;

const BOT_ID = "zi-liao-sou-ji-yuan";
const BOT_VIEW = {
	bot_id: BOT_ID,
	name: "资料搜集员",
	description: "负责查资料的助手。",
	instructions: "你是一名研究员。",
	provider: null,
	model: "deepseek-v4.1-flash",
	runner_id: null,
	owner_id: "e2e",
	config_version: 1,
	model_source: "server",
	model_source_config: {},
};

const BOT_ID_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;

// ── environment ───────────────────────────────────────────────────────────

function loadPuppeteer() {
	const roots = [
		join(APP_DIR, "node_modules"),
		"/home/soffy/.nvm/versions/node/v26.4.0/lib/node_modules/hyperframes/node_modules",
	];
	for (const root of roots) {
		try {
			return require(require.resolve("puppeteer-core", { paths: [root] }));
		} catch {
			/* try the next candidate */
		}
	}
	throw new Error("puppeteer-core not found (looked in node_modules and the global install)");
}

function findChrome() {
	const cache = join(homedir(), ".cache", "ms-playwright");
	if (!existsSync(cache)) return "";
	const revisions = readdirSync(cache)
		.filter((name) => name.startsWith("chromium-"))
		.map((name) => Number(name.split("-")[1]))
		.filter((value) => Number.isFinite(value))
		.sort((a, b) => b - a);
	for (const revision of revisions) {
		for (const dir of ["chrome-linux64", "chrome-linux"]) {
			const candidate = join(cache, `chromium-${revision}`, dir, "chrome");
			if (existsSync(candidate)) return candidate;
		}
	}
	return "";
}

// ── mocked API (the same fixtures as the Playwright spec) ──────────────────

function handler(url) {
	const path = new URL(url).pathname;
	if (path === "/api/v1/models") {
		return {
			default_model: "deepseek-v4.1-flash",
			default_provider: "opencode-go",
			models: [
				{ id: "deepseek-v4.1-flash", provider: "opencode-go", provider_label: "opencode-go", default: true },
				{ id: "gpt-4o-mini", provider: "openai", provider_label: "OpenAI", default: false },
			],
		};
	}
	if (path.startsWith("/api/v1/bots/groups")) return { groups: [] };
	if (path === "/api/v1/bots") return { bots: [BOT_VIEW], chef_bot_id: "chef", default_bot_id: BOT_ID };
	if (path === `/api/v1/bots/${BOT_ID}`) return { bot: BOT_VIEW };
	if (path === `/api/v1/bots/${BOT_ID}/messages`) {
		return {
			bot_id: BOT_ID,
			messages: [
				{ id: "m1", role: "user", text: "读取 https://example.com 的标题", ts: 1000 },
				{ id: "m2", role: "bot", text: "标题是：Example Domain", task_id: "task-1", ts: 1010 },
			],
		};
	}
	if (path.startsWith("/api/v1/workbench/")) {
		return {
			state: { status: "completed", version: "1" },
			timeline: [
				{ event_id: "e1", topic: "tool.requested", payload: { tool_name: "fetch_url", tool_args: { url: "https://example.com" } } },
				{ event_id: "e2", topic: "tool.completed", payload: { tool_name: "fetch_url" } },
			],
			approvals: { pending: [] },
		};
	}
	return {};
}

async function mockApi(page) {
	await page.setRequestInterception(true);
	page.on("request", (request) => {
		if (!request.url().includes("/api/v1/")) {
			void request.continue();
			return;
		}
		void request.respond({
			status: 200,
			contentType: "application/json",
			body: JSON.stringify(handler(request.url())),
		});
	});
}

// ── checks ────────────────────────────────────────────────────────────────

const failures = [];
function check(name, ok, detail = "") {
	console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok || !detail ? "" : ` — ${detail}`}`);
	if (!ok) failures.push(name);
}

async function main() {
	if (!existsSync(join(APP_DIR, "build"))) {
		console.error("FAIL  没有 build/ — 先跑 pnpm --dir apps/web build");
		process.exit(2);
	}
	const puppeteer = loadPuppeteer();
	const executablePath = findChrome();
	if (!executablePath) {
		console.error("FAIL  找不到 chromium (expected under ~/.cache/ms-playwright)");
		process.exit(2);
	}

	const server = spawn(process.execPath, [join(APP_DIR, "build")], {
		env: { ...process.env, PORT: String(PORT) },
		stdio: "ignore",
		detached: true,
	});
	const stop = () => {
		try {
			process.kill(-server.pid, "SIGTERM");
		} catch {
			/* already gone */
		}
	};
	process.on("exit", stop);

	try {
		await new Promise((resolve, reject) => {
			const timer = setInterval(async () => {
				try {
					const res = await fetch(BASE);
					if (res.ok) {
						clearInterval(timer);
						resolve();
					}
				} catch {
					/* not up yet */
				}
			}, 250);
			setTimeout(() => {
				clearInterval(timer);
				reject(new Error("server did not start"));
			}, 20000);
		});

		const browser = await puppeteer.launch({
			executablePath,
			headless: true,
			args: ["--no-sandbox", "--disable-dev-shm-usage"],
		});
		try {
			const page = await browser.newPage();
			await mockApi(page);

			// W-1: create a bot from the Team page.
			await page.goto(`${BASE}/team`, { waitUntil: "networkidle2" });
			check("W-1 Team 页渲染", (await page.$("h1")) !== null);
			await page.evaluate(() => {
				const buttons = [...document.querySelectorAll("button")];
				buttons.find((b) => b.textContent?.trim() === "新建")?.click();
			});
			await new Promise((r) => setTimeout(r, 300));
			const sheet = await page.$('[role="dialog"][aria-label="新建 Bot"]');
			check("W-1 新建 Bot 面板打开", sheet !== null);
			const presets = await page.$$eval('[role="dialog"] button', (nodes) =>
				nodes.map((n) => n.textContent ?? ""),
			);
			for (const label of ["通用助手", "研究员", "每日简报", "代码助手"]) {
				check(`W-1 预设「${label}」`, presets.some((text) => text.includes(label)));
			}
			const modelValue = await page.$eval("#new-bot-model", (el) => el.value);
			check("W-1 模型默认预选", modelValue === "deepseek-v4.1-flash", modelValue);
			const hasIdInput = (await page.$("#new-bot-id")) !== null;
			check("W-1 bot_id 不由用户输入", hasIdInput === false);
			await page.type("#new-bot-name", "资料搜集员");
			await new Promise((r) => setTimeout(r, 200));
			const derived = await page.$$eval('[role="dialog"] p', (nodes) =>
				nodes.map((n) => n.textContent ?? "").join("\n"),
			);
			const derivedId = /bot_id：([^\s（]+)/.exec(derived)?.[1] ?? "";
			check("W-1 bot_id 按名字生成", BOT_ID_RE.test(derivedId), derivedId || derived.slice(0, 120));
			// A Chinese-only name has no latin slug: the id still has to be stable
			// (same name → same id) and different from another name's.
			await page.evaluate(() => {
				const input = document.querySelector("#new-bot-name");
				input.value = "";
				input.dispatchEvent(new Event("input", { bubbles: true }));
			});
			await page.type("#new-bot-name", "另一个助手");
			await new Promise((r) => setTimeout(r, 200));
			const derived2 = await page.$$eval('[role="dialog"] p', (nodes) =>
				nodes.map((n) => n.textContent ?? "").join("\n"),
			);
			const derivedId2 = /bot_id：([^\s（]+)/.exec(derived2)?.[1] ?? "";
			check(
				"W-1 不同名字得到不同 bot_id",
				BOT_ID_RE.test(derivedId2) && derivedId2 !== derivedId,
				`${derivedId} vs ${derivedId2}`,
			);
			const submitDisabled = await page.$eval(
				'[role="dialog"] button[type="submit"]',
				(el) => /** @type {HTMLButtonElement} */ (el).disabled,
			);
			check("W-1 名字填好后可以提交", submitDisabled === false);

			// W-2: a bot chat with its live tool steps.
			await page.goto(`${BASE}/team/bot/${BOT_ID}`, { waitUntil: "networkidle2" });
			await page.waitForFunction(
				() => document.body.innerText.includes("标题是：Example Domain"),
				{ timeout: 8000 },
			);
			const chatBody = await page.evaluate(() => document.body.innerText);
			check("W-2 回复气泡", chatBody.includes("标题是：Example Domain"));
			check("W-2 工具步骤（工作过程）", chatBody.includes("工作过程") && chatBody.includes("2 步"));
			// The step list is history for a finished turn, so it is collapsed
			// until asked for — expand it and read the tool's own label.
			await page.evaluate(() => {
				const summary = [...document.querySelectorAll("summary")].find((node) =>
					node.textContent?.includes("工作过程"),
				);
				summary?.click();
			});
			await new Promise((r) => setTimeout(r, 250));
			const expanded = await page.evaluate(() => document.body.innerText);
			check("W-2 工具步骤可展开看细节", expanded.includes("读取网页"), expanded.slice(0, 200));

			// W-3: a bot card with a two-step delete.
			await page.goto(`${BASE}/team`, { waitUntil: "networkidle2" });
			await new Promise((r) => setTimeout(r, 300));
			const cards = await page.$$eval("li", (nodes) => nodes.map((n) => n.innerText ?? ""));
			check("W-3 机器人卡片显示名字", cards.some((text) => text.includes("资料搜集员")));

			// 375px: no sideways scroll, 44px targets, no zoom-on-focus input.
			await page.setViewport({ width: 375, height: 667 });
			await page.goto(`${BASE}/team`, { waitUntil: "networkidle2" });
			await page.evaluate(() => {
				const buttons = [...document.querySelectorAll("button")];
				buttons.find((b) => b.textContent?.trim() === "新建")?.click();
			});
			await new Promise((r) => setTimeout(r, 400));
			const metrics = await page.evaluate(() => {
				const submit = document.querySelector('[role="dialog"] button[type="submit"]');
				const composer = document.querySelector("#new-bot-name");
				return {
					overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
					submitHeight: submit ? Math.round(submit.getBoundingClientRect().height) : 0,
					fontSize: composer ? Number.parseFloat(getComputedStyle(composer).fontSize) : 0,
				};
			});
			check("375px 无横向溢出", metrics.overflow <= 1, `overflow=${metrics.overflow}`);
			check("375px 主按钮 ≥44px", metrics.submitHeight >= 44, `height=${metrics.submitHeight}`);
			check("输入框字号 ≥16px（不触发 iOS 缩放）", metrics.fontSize >= 16, `fontSize=${metrics.fontSize}`);
		} finally {
			await browser.close();
		}
	} finally {
		stop();
	}

	console.log(`\n${failures.length === 0 ? "ALL PASS" : `${failures.length} FAILED: ${failures.join(", ")}`}`);
	process.exit(failures.length === 0 ? 0 : 1);
}

main().catch((error) => {
	console.error(`FAIL  ${error instanceof Error ? error.message : String(error)}`);
	process.exit(1);
});
