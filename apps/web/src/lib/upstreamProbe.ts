/**
 * Upstream gateway probe — 确认 VEYA_GATEWAY 指向的是 veya 服务。
 *
 * 背景: 前端 /api/v1/* 由 SvelteKit 转发到 VEYA_GATEWAY (默认 127.0.0.1:8765)。
 * 8765 若被其他服务占用 (如 helivex api-gateway), 转发会得到无意义 404,
 * 用户看到"插件市场/定时任务加载失败"却不知原因。
 *
 * 本模块: 请求 veya 专属路径 /api/v1/mcp/health 探活; 结果短缓存 (10s)。
 * 仅在转发遇到 404/502 时调用 —— 正常请求零开销。
 */

export interface ProbeResult {
  ok: boolean;
  detail: string;
}

let probeCache: { ok: boolean; detail: string; until: number } | null = null;

export async function probeGateway(base: string): Promise<ProbeResult> {
  const now = Date.now();
  if (probeCache && probeCache.until > now) {
    return { ok: probeCache.ok, detail: probeCache.detail };
  }
  let ok = false;
  let detail = "";
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1500);
    const res = await fetch(`${base}/api/v1/mcp/health`, { signal: ctrl.signal });
    clearTimeout(timer);
    ok = res.ok;
    detail = res.ok ? "ok" : `HTTP ${res.status}`;
  } catch (e) {
    ok = false;
    detail = e instanceof Error ? e.message : String(e);
  }
  probeCache = { ok, detail, until: now + (ok ? 10_000 : 2_000) };
  return { ok, detail };
}

/** 生成带引导的 502 响应 (网关非 veya / 未启动)。 */
export function gatewayGuideResponse(base: string, probeDetail: string): Response {
  const body = {
    error: "gateway_not_veya",
    message:
      `网关 ${base} 不是 veya 服务或未启动 (探活: ${probeDetail})。` +
      `请先运行 \`veya start\` 启动 veya 服务 (若 8765 被占用会自动避让端口, 见其输出), ` +
      `然后在 apps/web/.env 设置 VEYA_GATEWAY=http://127.0.0.1:<veya端口> 并重启前端。`,
  };
  return new Response(JSON.stringify(body, null, 2), {
    status: 502,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
