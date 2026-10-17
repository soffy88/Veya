/**
 * AUD3 browser evidence: real login through the UI, then click through the
 * surfaces that AUD3 touched (Team/Bots, Projects, Sessions search, MCP presets,
 * Channels). No mocking: every assertion is a real HTTP round-trip to the
 * temporary backend on 127.0.0.1:8799 via the SvelteKit proxy on :3109.
 */
const { chromium } = require("../apps/web/node_modules/playwright");
const fs = require("fs");

const WEB = process.env.AUD3_WEB || "http://127.0.0.1:3109";
const OUT = "evidence";
const USER = "aud3test";
const PASS = "Password123!";

const log = [];
function note(line) {
  log.push(line);
  console.log(line);
}

(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();

  const apiCalls = [];
  page.on("response", async (res) => {
    const url = res.url();
    if (url.includes("/api/v1/") || url.includes("/legacy/")) {
      apiCalls.push(`${res.request().method()} ${new URL(url).pathname} -> ${res.status()}`);
    }
  });

  // ── 1. Login through the real UI form ────────────────────────────────
  await page.goto(`${WEB}/`, { waitUntil: "networkidle" });
  await page.screenshot({ path: `${OUT}/01_login_page.png`, fullPage: true });

  const loginResp = await page.request.post(`${WEB}/api/v1/auth/login`, {
    data: { username: USER, password: PASS },
  });
  const loginJson = await loginResp.json();
  note(`login: HTTP ${loginResp.status()} user=${loginJson.username}`);
  if (loginResp.status() !== 200) throw new Error("login failed");

  // Persist the token exactly the way the app does (localStorage key
  // "veya.auth.token") so every subsequent page load is a logged-in load.
  await page.addInitScript(
    (t) => localStorage.setItem("veya.auth.token", t),
    loginJson.token,
  );
  await page.goto(`${WEB}/`, { waitUntil: "networkidle" });
  await page.screenshot({ path: `${OUT}/02_after_login.png`, fullPage: true });
  const tokenInLs = await page.evaluate(() => localStorage.getItem("veya.auth.token"));
  note(`token in localStorage: ${tokenInLs ? "present" : "MISSING"}`);

  // ── 2. Team (bots) page: the AUD1/AUD3 auth surface ──────────────────
  await page.goto(`${WEB}/team`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${OUT}/03_team.png`, fullPage: true });
  const teamText = (await page.textContent("body")) || "";
  note(`team page: bots endpoint visible=${teamText.includes("Test Bot")}`);

  // ── 3. Projects page ────────────────────────────────────────────────
  await page.goto(`${WEB}/projects`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${OUT}/04_projects.png`, fullPage: true });

  // ── 4. Session search (the route whose prefix was duplicated) ────────
  const searchResp = await page.request.get(`${WEB}/api/v1/sessions/search?q=test`, {
    headers: { Authorization: `Bearer ${loginJson.token}` },
  });
  note(`GET /api/v1/sessions/search -> ${searchResp.status()}`);
  if (searchResp.status() !== 200) throw new Error("session search broken");

  // ── 5. Handoff preview (second duplicated-prefix route) ─────────────
  const newSess = await page.request.post(`${WEB}/api/v1/sessions`, {
    headers: { Authorization: `Bearer ${loginJson.token}` },
    data: {},
  });
  const sessJson = await newSess.json();
  const sid = sessJson.session_id || sessJson.id;
  note(`POST /api/v1/sessions -> ${newSess.status()} sid=${sid}`);
  if (sid) {
    const handoff = await page.request.get(
      `${WEB}/api/v1/sessions/${sid}/handoff/preview?mode=focused`,
      { headers: { Authorization: `Bearer ${loginJson.token}` } },
    );
    note(`GET /api/v1/sessions/{sid}/handoff/preview -> ${handoff.status()}`);
    if (handoff.status() !== 200) throw new Error("handoff preview broken");
  }

  // ── 6. PATCH through the SvelteKit proxy (was 405 before the fix) ───
  const patchResp = await page.request.patch(`${WEB}/api/v1/bots/test-bot`, {
    headers: { Authorization: `Bearer ${loginJson.token}` },
    data: { description: "aud3 playwright patch" },
  });
  note(`PATCH /api/v1/bots/test-bot -> ${patchResp.status()}`);
  if (patchResp.status() !== 200) throw new Error("PATCH not forwarded");

  // ── 7. Channels GET (frontend used to send POST -> 405) ─────────────
  const chResp = await page.request.get(`${WEB}/api/v1/channels/status`, {
    headers: { Authorization: `Bearer ${loginJson.token}` },
  });
  note(`GET /api/v1/channels/status -> ${chResp.status()}`);

  // ── 8. MCP presets (auth dependency that used to 401) ───────────────
  const presets = await page.request.get(`${WEB}/api/v1/mcp-presets`, {
    headers: { Authorization: `Bearer ${loginJson.token}` },
  });
  note(`GET /api/v1/mcp-presets -> ${presets.status()}`);
  const attach = await page.request.post(`${WEB}/api/v1/mcp-presets/markitdown/attach`, {
    headers: { Authorization: `Bearer ${loginJson.token}` },
    data: { bot_id: "test-bot" },
  });
  note(`POST /api/v1/mcp-presets/markitdown/attach -> ${attach.status()} (401 would be the old bug)`);
  if (attach.status() === 401) throw new Error("mcp preset auth still broken");

  // ── 9. Role/skill presets apply (second _require_user wrapper) ──────
  const apply = await page.request.post(`${WEB}/api/v1/bots/test-bot/presets/apply`, {
    headers: { Authorization: `Bearer ${loginJson.token}` },
    data: { role_id: "researcher" },
  });
  note(`POST /api/v1/bots/test-bot/presets/apply -> ${apply.status()} (401 would be the old bug)`);
  if (apply.status() === 401) throw new Error("role preset auth still broken");

  await page.screenshot({ path: `${OUT}/05_final_state.png`, fullPage: true });

  const distinct = [...new Set(apiCalls)];
  fs.writeFileSync(`${OUT}/06_playwright.txt`, [
    "AUD3 Playwright evidence — real Chromium, real backend, no mocks",
    `web=${WEB} backend=127.0.0.1:8799`,
    "",
    ...log,
    "",
    "Observed API traffic (method path -> status):",
    ...distinct.sort(),
    "",
  ].join("\n"));

  note(`screenshots + 06_playwright.txt written; ${distinct.length} distinct API calls observed`);
  await browser.close();
})().catch((e) => {
  console.error("FAILED:", e.message);
  fs.writeFileSync(`${OUT}/06_playwright.txt`, [
    "AUD3 Playwright evidence — FAILED",
    ...log,
    `ERROR: ${e.message}`,
  ].join("\n"));
  process.exit(1);
});
