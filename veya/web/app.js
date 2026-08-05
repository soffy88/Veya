// veya — Agent Console
// A minimal, practical chat frontend in the spirit of Claude Code / Codex / Cline.
// Talks to the veya gateway:  POST /api/v1/agent/stream (SSE)  with a
// one-shot POST /api/v1/agent/run fallback, and GET /api/v1/agent/history/{sid}.

"use strict";

// ── backend resolution ────────────────────────────────────────────
const params = new URLSearchParams(location.search);
const BACKEND = (
  params.get("backend") || window.VEYA_BACKEND || location.origin
).replace(/\/+$/, "");

// ── state ─────────────────────────────────────────────────────────
const state = {
  sid: "",
  messages: [], // {role, text, steps:[], status, cost, error}
  running: false,
  abort: null,
  provider: localStorage.getItem("veya.provider") || "dashscope",
  model: localStorage.getItem("veya.model") || "",
};

const LS_SESSIONS = "veya.sessions"; // [{sid, title, ts, cost, messages[]}]

// ── dom helpers ───────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function toast(msg, isErr) {
  let t = document.querySelector(".toast");
  if (!t) {
    t = el("div", "toast");
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.className = "toast " + (isErr ? "toast-err" : "toast-ok");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.remove(), 3500);
}

// ── markdown-lite (code blocks / inline code / bold) ─────────────
function renderMarkdown(text) {
  const parts = String(text).split(/(```[\s\S]*?```)/g);
  const out = document.createElement("div");
  for (const part of parts) {
    if (!part) continue;
    if (part.startsWith("```")) {
      const body = part.slice(3);
      const nl = body.indexOf("\n");
      const code = nl >= 0 ? body.slice(nl + 1) : body;
      const pre = el("pre");
      const codeEl = el("code");
      codeEl.textContent = code.replace(/\n$/, "");
      pre.appendChild(codeEl);
      out.appendChild(pre);
    } else {
      out.appendChild(renderInline(part));
    }
  }
  return out;
}

function renderInline(text) {
  const p = el("p");
  const safe = esc(text);
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)/g;
  let last = 0;
  let m;
  while ((m = re.exec(safe)) !== null) {
    if (m.index > last) p.appendChild(document.createTextNode(safe.slice(last, m.index)));
    if (m[1]) {
      const c = el("code");
      c.textContent = m[1].slice(1, -1);
      p.appendChild(c);
    } else if (m[2]) {
      const b = el("strong");
      b.textContent = m[2].slice(2, -2);
      p.appendChild(b);
    }
    last = re.lastIndex;
  }
  if (last < safe.length) p.appendChild(document.createTextNode(safe.slice(last)));
  if (!p.childNodes.length) p.appendChild(document.createTextNode(text));
  return p;
}

// ── step rendering ────────────────────────────────────────────────
function stepLine(ev) {
  const step = ev.step || {};
  const action = step.action || ev.event || "step";
  const detail =
    step.detail ||
    step.data ||
    step.tool ||
    (step.tool_args && JSON.stringify(step.tool_args)) ||
    "";
  return { action, detail };
}

const STEP_CLS = {
  llm_call: "llm_call",
  thinking: "thinking",
  tool_call: "tool_call",
  tool_result: "tool_result",
  error: "error",
};

function appendStep(container, ev) {
  const { action, detail } = stepLine(ev);
  const row = el("div", "step " + (STEP_CLS[action] || ""));
  const caret = el("span", "caret", detail ? "▾" : "▸");
  const t = el("span", "t", action);
  row.appendChild(caret);
  row.appendChild(t);
  if (detail) {
    const d = el("span", "d", String(detail).slice(0, 160));
    caret.title = "点击展开/收起";
    caret.addEventListener("click", () => {
      const expanded = d.classList.toggle("expanded");
      d.textContent = expanded ? String(detail) : String(detail).slice(0, 160);
    });
    row.appendChild(d);
  }
  container.appendChild(row);
}

// ── session persistence ───────────────────────────────────────────
function loadSessions() {
  try {
    return JSON.parse(localStorage.getItem(LS_SESSIONS) || "[]");
  } catch {
    return [];
  }
}
function saveSessions(list) {
  try {
    localStorage.setItem(LS_SESSIONS, JSON.stringify(list.slice(0, 50)));
  } catch {}
}

function currentSessionTitle() {
  const first = state.messages.find((m) => m.role === "user");
  if (!first) return "新会话";
  const t = first.text.replace(/\s+/g, " ").trim();
  return t.length > 40 ? t.slice(0, 40) + "…" : t;
}

function persistCurrent() {
  if (!state.sid) return;
  const sessions = loadSessions().filter((s) => s.sid !== state.sid);
  sessions.unshift({
    sid: state.sid,
    title: currentSessionTitle(),
    ts: Date.now(),
    cost: state.messages.reduce((a, m) => a + (m.cost || 0), 0),
    messages: state.messages.map((m) => ({ ...m, steps: [...(m.steps || [])] })),
  });
  saveSessions(sessions);
  renderSessions();
}

// ── session rail ──────────────────────────────────────────────────
function renderSessions() {
  const list = loadSessions();
  const box = $("session-list");
  box.innerHTML = "";
  if (!list.length) {
    const empty = el("div", "t-empty", "（暂无历史会话）");
    box.appendChild(empty);
  }
  for (const s of list) {
    const item = el("div", "session-item" + (s.sid === state.sid ? " active" : ""));
    item.appendChild(el("div", "t", s.title || "会话"));
    const meta = el("div", "m");
    const when = new Date(s.ts).toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
    meta.appendChild(el("span", null, when));
    if (s.cost) meta.appendChild(el("span", null, "$" + s.cost.toFixed(4)));
    item.appendChild(meta);
    item.addEventListener("click", () => openSession(s));
    box.appendChild(item);
  }
  $("rail-foot").textContent = state.sid ? "session " + state.sid.slice(0, 8) : "";
}

function openSession(s) {
  if (state.running) {
    toast("正在运行，先停止当前任务", true);
    return;
  }
  state.sid = s.sid;
  state.messages = (s.messages || []).map((m) => ({ ...m, steps: [...(m.steps || [])] }));
  renderAll();
  renderSessions();
}

// ── chat rendering ────────────────────────────────────────────────
function renderAll() {
  const log = $("log");
  log.innerHTML = "";
  if (!state.messages.length) {
    renderEmpty(log);
    return;
  }
  const inner = el("div", "log-inner");
  state.messages.forEach((m, i) => {
    const node = renderMessage(m);
    node.dataset.idx = String(i);
    inner.appendChild(node);
  });
  log.appendChild(inner);
  scrollBottom();
}

function renderEmpty(log) {
  const empty = el("div");
  empty.id = "empty";
  empty.appendChild(el("div", "big", "⚡"));
  const h = el("h2");
  h.textContent = "veya agent console";
  empty.appendChild(h);
  const p = el("p");
  p.textContent = "直接描述任务，veya 会实时执行工具并流式返回结果。会话记录保存在本地浏览器。";
  empty.appendChild(p);

  const ex = el("div", "examples");
  for (const t of [
    "分析一下当前项目的目录结构",
    "跑一下 pytest 看测试是否通过",
    "写一个快速排序并附上测试",
    "解释 git rebase 和 merge 的区别",
  ]) {
    const b = el("button", null, t);
    b.addEventListener("click", () => {
      $("input").value = t;
      $("input").focus();
    });
    ex.appendChild(b);
  }
  empty.appendChild(ex);
  empty.appendChild(el("div", "warn",
    "提示：未配置 LLM API Key 时回复为占位内容。" +
    "设置 DASHSCOPE_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY 环境变量后重启网关即可接入真实模型。"));
  log.appendChild(empty);
}

function renderMessage(m) {
  const row = el("div", "msg " + (m.role === "user" ? "user" : "assistant"));
  row.appendChild(el("div", "avatar", m.role === "user" ? "🧑" : "🤖"));

  const body = el("div", "body");
  let b;
  if (m.role === "user") {
    b = el("div", "bubble", m.text);
  } else {
    b = el("div", "bubble");
    if (m.steps && m.steps.length) {
      const steps = el("div", "steps");
      for (const ev of m.steps) appendStep(steps, ev);
      b.appendChild(steps);
    }
    const content = el("div", "content");
    if (m.status === "streaming") {
      const p = el("p");
      p.textContent = m.text || "";
      content.appendChild(p);
      content.appendChild(el("span", "cursor"));
    } else if (m.text) {
      content.appendChild(renderMarkdown(m.text));
    }
    b.appendChild(content);

    const foot = el("div", "foot");
    const st = el("span", "st");
    if (m.status === "streaming") st.textContent = "运行中…";
    else if (m.status === "error") { st.className = "st err"; st.textContent = "出错"; }
    else if (m.status === "stopped") { st.className = "st stopped"; st.textContent = "已停止"; }
    else st.textContent = "✓ 完成";
    foot.appendChild(st);
    if (m.error) foot.appendChild(el("span", "st err", String(m.error).slice(0, 200)));
    if (typeof m.cost === "number" && m.cost > 0) {
      foot.appendChild(el("span", "cost", "$" + m.cost.toFixed(6)));
    }
    b.appendChild(foot);
  }
  body.appendChild(b);
  row.appendChild(body);
  return row;
}

function scrollBottom() {
  const log = $("log");
  log.scrollTop = log.scrollHeight;
}

// ── connection health ─────────────────────────────────────────────
async function checkHealth() {
  const dot = $("conn-dot");
  const text = $("conn-text");
  try {
    const r = await fetch(`${BACKEND}/api/v1/mcp/health`, { signal: AbortSignal.timeout(4000) });
    if (!r.ok) throw new Error("http " + r.status);
    const j = await r.json();
    dot.className = "dot ok";
    text.textContent = "已连接 · " + (j.tools_count != null ? j.tools_count + " tools" : "");
  } catch {
    dot.className = "dot err";
    text.textContent = "后端不可达";
  }
}

// ── sending ───────────────────────────────────────────────────────
function config() {
  const c = { provider: state.provider };
  if (state.model) c.model = state.model;
  return c;
}

function setRunning(v) {
  state.running = v;
  $("btn-send").textContent = v ? "停止 ■" : "发送 ↵";
  $("btn-send").classList.toggle("primary", !v);
  $("input").disabled = v;
  $("hint-status").textContent = v ? "agent 运行中…" : "";
}

async function send() {
  const input = $("input");
  const text = input.value.trim();
  if (!text || state.running) return;
  input.value = "";
  input.style.height = "auto";

  if (!state.sid) state.sid = crypto.randomUUID();
  state.messages.push({ role: "user", text, steps: [], status: "done" });
  const aidx = state.messages.push({
    role: "assistant", text: "", steps: [], status: "streaming", cost: 0,
  }) - 1;

  renderAll();
  setRunning(true);

  const ab = new AbortController();
  state.abort = ab;

  // live DOM handles for the streaming message
  let liveContent = null;
  let liveP = null;
  let liveSteps = null;

  const ensureLive = () => {
    const node = document.querySelector(`.msg[data-idx="${aidx}"] .bubble`);
    if (!node) return;
    if (!liveContent) {
      liveContent = node.querySelector(".content");
      liveP = liveContent ? liveContent.querySelector("p") : null;
    }
    if (!liveSteps) liveSteps = node.querySelector(".steps");
  };

  const onDelta = (d) => {
    state.messages[aidx].text += d;
    ensureLive();
    if (!liveP) {
      liveP = el("p");
      if (liveContent) liveContent.prepend(liveP);
    }
    liveP.textContent = state.messages[aidx].text;
    scrollBottom();
  };

  const onStep = (ev) => {
    state.messages[aidx].steps.push(ev);
    ensureLive();
    if (!liveSteps) {
      liveSteps = el("div", "steps");
      const node = document.querySelector(`.msg[data-idx="${aidx}"] .bubble`);
      if (node) node.insertBefore(liveSteps, node.firstChild);
    }
    appendStep(liveSteps, ev);
    scrollBottom();
  };

  try {
    await streamOrFallback(text, aidx, { onDelta, onStep, signal: ab.signal });
  } catch (e) {
    const m = state.messages[aidx];
    if (ab.signal.aborted) {
      m.status = "stopped";
    } else {
      m.status = "error";
      m.error = e.message;
    }
  } finally {
    state.abort = null;
    setRunning(false);
    const m = state.messages[aidx];
    if (m && m.status === "streaming") m.status = "done";
    renderAll();
    persistCurrent();
  }
}

async function streamOrFallback(text, aidx, { onDelta, onStep, signal }) {
  try {
    await streamRun(text, aidx, { onDelta, onStep, signal });
  } catch (err) {
    if (signal.aborted) throw err;
    // SSE failed — fall back to the one-shot endpoint.
    onStep({ event: "step", step: { action: "fallback", detail: "SSE 不可用，改用一次性请求：" + err.message } });
    const res = await fetch(`${BACKEND}/api/v1/agent/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: text, session_id: state.sid, config: config() }),
      signal,
    });
    if (!res.ok) throw new Error("HTTP " + res.status + " " + (await res.text()).slice(0, 200));
    const j = await res.json();
    if (j.session_id) state.sid = j.session_id;
    const result = typeof j.result === "string" ? j.result : JSON.stringify(j.result ?? j, null, 2);
    onDelta(result);
    if (typeof j.cost_usd === "number") state.messages[aidx].cost = j.cost_usd;
  }
}

async function streamRun(task, aidx, { onDelta, onStep, signal }) {
  const res = await fetch(`${BACKEND}/api/v1/agent/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task, session_id: state.sid, config: config() }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error("HTTP " + res.status + " — " + (await res.text()).slice(0, 200));

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  const handleFrame = (data) => {
    if (data === "[DONE]") return;
    let ev;
    try {
      ev = JSON.parse(data);
    } catch {
      return;
    }
    switch (ev.event) {
      case "session":
        if (ev.session_id) state.sid = ev.session_id;
        break;
      case "step":
        onStep(ev);
        break;
      case "text_delta":
        if (typeof ev.delta === "string") onDelta(ev.delta);
        break;
      case "session_done":
        const m = state.messages[aidx];
        if (m && typeof ev.cost === "number") m.cost = ev.cost;
        break;
      case "error":
        throw new Error(ev.error || "agent error");
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("data:")) handleFrame(line.slice(5).trim());
      }
    }
  }
}

// ── new session ───────────────────────────────────────────────────
function newSession() {
  if (state.running) {
    toast("正在运行，先停止当前任务", true);
    return;
  }
  state.sid = "";
  state.messages = [];
  renderAll();
  renderSessions();
  $("input").focus();
}

// ── init ──────────────────────────────────────────────────────────
function init() {
  $("provider").value = state.provider;
  $("model").value = state.model;

  $("provider").addEventListener("change", (e) => {
    state.provider = e.target.value;
    localStorage.setItem("veya.provider", state.provider);
  });
  $("model").addEventListener("change", (e) => {
    state.model = e.target.value.trim();
    localStorage.setItem("veya.model", state.model);
  });

  // backend url click-to-edit
  $("conn").addEventListener("click", () => {
    const url = prompt("后端地址 (含协议与端口):", BACKEND);
    if (!url) return;
    location.href = location.pathname + "?backend=" + encodeURIComponent(url);
  });
  $("conn").title = "后端: " + BACKEND;

  const input = $("input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    } else if (e.key === "Escape" && state.running) {
      e.preventDefault();
      if (state.abort) state.abort.abort();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 180) + "px";
  });

  $("btn-send").addEventListener("click", () => {
    if (state.running) {
      if (state.abort) state.abort.abort();
    } else {
      send();
    }
  });
  $("btn-new").addEventListener("click", newSession);

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
      e.preventDefault();
      newSession();
    }
  });

  renderSessions();
  renderAll();
  checkHealth();
  setInterval(checkHealth, 15000);
}

init();
