import { createRequire } from "module";
import fs from "fs";
import path from "path";

const require = createRequire(import.meta.url);
const { chromium } = require(path.resolve("apps/web/node_modules/playwright"));

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:3199";
const EVIDENCE_DIR = path.resolve("evidence");
if (!fs.existsSync(EVIDENCE_DIR)) {
	fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
}

async function run() {
	console.log(`Starting AUD1 Playwright verification against ${BASE_URL}...`);
	const browser = await chromium.launch({
		headless: true,
		args: ["--no-sandbox", "--disable-setuid-sandbox"],
	});
	const context = await browser.newContext({
		viewport: { width: 1280, height: 800 },
	});
	const page = await context.newPage();

	// 1. Visit Home Page
	console.log("Navigating to home page...");
	await page.goto(`${BASE_URL}/`, { waitUntil: "networkidle" });

	// 2. Click Login button to open AuthGate
	console.log("Opening login dialog...");
	const loginBtn = page.locator("button.login-btn");
	await loginBtn.waitFor({ state: "visible", timeout: 5000 });
	await loginBtn.click();

	// Switch to Register tab
	const registerTab = page.locator('.tabs button:has-text("注册")');
	await registerTab.waitFor({ state: "visible", timeout: 5000 });
	await registerTab.click();

	const username = `u_${Date.now().toString(36)}`;
	const password = "password123456";

	console.log(`Registering / logging in as ${username}...`);
	await page.locator('input[placeholder*="用户名"]').fill(username);
	await page.locator('input[placeholder*="密码"]').fill(password);
	await page.locator('button.submit:has-text("注册")').click();

	// Wait for login badge
	const badge = page.locator(".auth-badge");
	await badge.waitFor({ state: "visible", timeout: 8000 });
	console.log("Successfully logged in, auth badge visible!");

	// 3. Navigate to /team
	console.log("Navigating to /team...");
	await page.goto(`${BASE_URL}/team`, { waitUntil: "networkidle" });
	await page.waitForTimeout(1000);

	// Check that Team page loads data and does NOT prompt "需要登录"
	const pageText = await page.textContent("body");
	if (pageText.includes("需要登录才能访问 Bot 团队") || pageText.includes("请先登录")) {
		throw new Error("Team page erroneously prompted login while user was logged in!");
	}
	console.log("Team page loaded successfully with no login errors.");
	await page.screenshot({ path: path.join(EVIDENCE_DIR, "01_team_logged_in.png") });
	console.log("Saved screenshot: evidence/01_team_logged_in.png");

	// 4. Click back to home on Team page
	console.log("Clicking '← 返回控制台' on Team page...");
	const backLinkTeam = page.locator('a:has-text("← 返回控制台")');
	await backLinkTeam.waitFor({ state: "visible", timeout: 5000 });
	await backLinkTeam.click();
	await page.waitForURL(`${BASE_URL}/`, { timeout: 5000 });
	console.log("Successfully navigated back to home page from Team!");
	await page.screenshot({ path: path.join(EVIDENCE_DIR, "02_team_back_home.png") });
	console.log("Saved screenshot: evidence/02_team_back_home.png");

	// 5. Navigate to /projects
	console.log("Navigating to /projects...");
	await page.goto(`${BASE_URL}/projects`, { waitUntil: "networkidle" });
	await page.waitForTimeout(1000);

	// Check that Projects page loads data and does NOT prompt "需要登录" or "无法获取项目列表"
	const projectsText = await page.textContent("body");
	if (projectsText.includes("需要登录才能访问") || projectsText.includes("无法获取项目列表")) {
		throw new Error("Projects page erroneously prompted login or failed to fetch projects!");
	}
	console.log("Projects page loaded successfully with no errors.");
	await page.screenshot({ path: path.join(EVIDENCE_DIR, "03_projects_logged_in.png") });
	console.log("Saved screenshot: evidence/03_projects_logged_in.png");

	// 6. Click back to home on Projects page
	console.log("Clicking '← 返回控制台' on Projects page...");
	const backLinkProjects = page.locator('a:has-text("← 返回控制台")');
	await backLinkProjects.waitFor({ state: "visible", timeout: 5000 });
	await backLinkProjects.click();
	await page.waitForURL(`${BASE_URL}/`, { timeout: 5000 });
	console.log("Successfully navigated back to home page from Projects!");
	await page.screenshot({ path: path.join(EVIDENCE_DIR, "04_projects_back_home.png") });
	console.log("Saved screenshot: evidence/04_projects_back_home.png");

	await browser.close();
	console.log("ALL PLAYWRIGHT VERIFICATIONS PASSED!");
}

run().catch((err) => {
	console.error("Playwright verification failed:", err);
	process.exit(1);
});
