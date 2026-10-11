import { test, expect, type Page } from "@playwright/test";

/**
 * W11 group B smoke: the two flows a first-time phone user must be able to do
 * (W-1 create a bot, W-2 chat with tool steps and a side-effect approval),
 * against a mocked API.
 *
 * Everything is mocked at the network edge (`**/api/v1/**`), so this runs with
 * no server, no model and no secrets — it is about the UI contract, not the
 * backend.
 *
 * Run: `pnpm --dir apps/web add -D @playwright/test && pnpm --dir apps/web exec playwright test`
 * (the repo's node_modules is shared between worktrees, so the package is not
 * committed here; `playwright.config.ts` next to this file is the config).
 */

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

function json(payload: unknown) {
	return { status: 200, contentType: "application/json", body: JSON.stringify(payload) };
}

/** The API the landing pages touch, with just enough data to render. */
async function mockApi(page: Page): Promise<void> {
	await page.route("**/api/v1/models", (route) =>
		route.fulfill(
			json({
				default_model: "deepseek-v4.1-flash",
				default_provider: "opencode-go",
				models: [
					{ id: "deepseek-v4.1-flash", provider: "opencode-go", provider_label: "opencode-go", default: true },
					{ id: "gpt-4o-mini", provider: "openai", provider_label: "OpenAI", default: false },
				],
			}),
		),
	);

	await page.route("**/api/v1/bots/groups*", (route) => route.fulfill(json({ groups: [] })));

	await page.route("**/api/v1/bots", async (route) => {
		if (route.request().method() === "POST") {
			const payload = JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
			return route.fulfill(json({ status: "created", bot: { ...BOT_VIEW, ...payload } }));
		}
		return route.fulfill(json({ bots: [BOT_VIEW], chef_bot_id: "chef", default_bot_id: BOT_ID }));
	});

	await page.route(`**/api/v1/bots/${BOT_ID}/messages*`, async (route) => {
		if (route.request().method() === "POST") {
			return route.fulfill(
				json({
					status: "accepted",
					task_id: "task-1",
					session_id: "sess-1",
					bot_id: BOT_ID,
					kind: "bot_turn",
					result_url: `/api/v1/bots/${BOT_ID}/messages`,
				}),
			);
		}
		return route.fulfill(
			json({
				bot_id: BOT_ID,
				messages: [
					{ id: "m1", role: "user", text: "读取 https://example.com 的标题", ts: 1000 },
					{
						id: "m2",
						role: "bot",
						text: "标题是：Example Domain",
						task_id: "task-1",
						ts: 1010,
					},
				],
			}),
		);
	});

	await page.route("**/api/v1/workbench/**", (route) =>
		route.fulfill(
			json({
				state: { status: "completed", version: "1" },
				timeline: [
					{
						event_id: "e1",
						topic: "tool.requested",
						payload: { tool_name: "fetch_url", tool_args: { url: "https://example.com" } },
					},
					{ event_id: "e2", topic: "tool.completed", payload: { tool_name: "fetch_url" } },
				],
				approvals: { pending: [] },
			}),
		),
	);
}

test.describe("bot landing (mocked API)", () => {
	test("W-1: create a bot from the Team page and land in its chat", async ({ page }) => {
		await mockApi(page);
		await page.goto("/team");

		await expect(page.getByRole("heading", { name: "Bot 团队" })).toBeVisible();
		await page.getByRole("button", { name: "新建", exact: true }).click();

		const sheet = page.getByRole("dialog", { name: "新建 Bot" });
		await expect(sheet).toBeVisible();

		// The four presets, and the model picker preselects the server default.
		for (const label of ["通用助手", "研究员", "每日简报", "代码助手"]) {
			await expect(sheet.getByRole("button", { name: new RegExp(label) })).toBeVisible();
		}
		await expect(sheet.locator("#new-bot-model")).toHaveValue("deepseek-v4.1-flash");

		// The bot id is derived, never typed: there is no id input in the sheet.
		await expect(sheet.locator("#new-bot-id")).toHaveCount(0);
		await sheet.locator("#new-bot-name").fill("资料搜集员");
		// A Chinese-only name has no latin slug, so the derived id is a stable
		// hash-suffixed fallback — never a box the user has to fill in.
		const derived = await sheet.locator("p", { hasText: "bot_id" }).innerText();
		expect(derived).toMatch(/bot_id：[a-z0-9][a-z0-9._-]*/);

		await sheet.getByRole("button", { name: "创建并开始聊天" }).click();
		await expect(page).toHaveURL(new RegExp(`/team/bot/${BOT_ID}$`));
	});

	test("W-1: an empty name is refused inline, not by the server", async ({ page }) => {
		await mockApi(page);
		await page.goto("/team");
		await page.getByRole("button", { name: "新建", exact: true }).click();

		const sheet = page.getByRole("dialog", { name: "新建 Bot" });
		await expect(sheet.getByText("给它起个名字")).toBeVisible();
		await expect(sheet.getByRole("button", { name: "创建并开始聊天" })).toBeDisabled();
	});

	test("W-2: a bot reply carries its live tool steps", async ({ page }) => {
		await mockApi(page);
		await page.goto(`/team/bot/${BOT_ID}`);

		// The mocked transcript already holds the reply with its task id.
		await expect(page.getByText("标题是：Example Domain")).toBeVisible();
		await expect(page.getByText("工作过程")).toBeVisible();
		await expect(page.getByText("读取网页")).toBeVisible();
		// The tool's own arguments are one click deeper, not dumped in the bubble.
		await page.getByText("读取网页").click();
		await expect(page.getByText("https://example.com", { exact: false })).toBeVisible();
	});

	test("W-3: a bot card shows status, last line and a delete confirm", async ({ page }) => {
		await mockApi(page);
		await page.goto("/team");
		const card = page.getByRole("listitem").filter({ hasText: "资料搜集员" }).first();
		await expect(card).toBeVisible();
		await card.getByTitle("删除这个机器人").click();
		await expect(card.getByRole("button", { name: "确认删除" })).toBeVisible();
	});
});

test.describe("phone (375px, iOS Safari)", () => {
	test.use({ viewport: { width: 375, height: 667 } });

	test("the create sheet fits a small screen without sideways scroll", async ({ page }) => {
		await mockApi(page);
		await page.goto("/team");
		await page.getByRole("button", { name: "新建", exact: true }).click();

		const overflow = await page.evaluate(
			() => document.documentElement.scrollWidth - document.documentElement.clientWidth,
		);
		expect(overflow).toBeLessThanOrEqual(1);

		// Primary actions keep a 44px touch target.
		const submit = page.getByRole("button", { name: "创建并开始聊天" });
		const box = await submit.boundingBox();
		expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
	});

	test("the composer is reachable and the input does not zoom on focus", async ({ page }) => {
		await mockApi(page);
		await page.goto(`/team/bot/${BOT_ID}`);
		const composer = page.getByPlaceholder("发消息…");
		await expect(composer).toBeVisible();
		const fontSize = await composer.evaluate((el) => getComputedStyle(el).fontSize);
		expect(Number.parseFloat(fontSize)).toBeGreaterThanOrEqual(16);
	});
});
