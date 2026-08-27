import { json } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";

const UPSTREAM = (process.env.VEYA_GATEWAY ?? "http://127.0.0.1:8765").replace(/\/+$/, "");
const PROBE_TIMEOUT_MS = 5000;

type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown): JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function asRecordOrNull(value: unknown): JsonRecord | null {
  const record = asRecord(value);
  return Object.keys(record).length > 0 ? record : null;
}

function asString(value: unknown, fallback = "unknown"): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

async function probe(path: string): Promise<{ http_status: number | null; body: JsonRecord }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);

  try {
    const response = await fetch(`${UPSTREAM}${path}`, {
      headers: { accept: "application/json" },
      signal: controller.signal,
    });
    const body = asRecordOrNull(await response.json().catch(() => null)) ?? {};
    return { http_status: response.status, body };
  } catch {
    return { http_status: null, body: {} };
  } finally {
    clearTimeout(timer);
  }
}

export const GET: RequestHandler = async () => {
  const [backendProbe, durableProbe, personalProbe] = await Promise.all([
    probe("/health"),
    probe("/health/execution-runtime"),
    probe("/health/personal-runtime"),
  ]);

  const backendStatus = backendProbe.http_status === 200 ? "ok" : "degraded";
  const durableBody = durableProbe.body;
  const personalBody = personalProbe.body;
  const durableHealthy =
    durableProbe.http_status === 200 &&
    durableBody.healthy === true &&
    durableBody.enabled === true &&
    durableBody.authority === "postgresql" &&
    durableBody.db_connected === true;
  const personalHealthy =
    personalProbe.http_status === 200 &&
    personalBody.healthy === true &&
    personalBody.enabled === true &&
    personalBody.authority === "postgresql";
  const personalMetrics = asRecord(personalBody.metrics);
  const goldBenchmark = asRecord(personalMetrics.gold_benchmark);
  const healthy = backendStatus === "ok" && durableHealthy && personalHealthy;

  return json(
    {
      status: healthy ? "ok" : "degraded",
      web: "ok",
      gateway: backendStatus,
      backend: backendStatus,
      durable: durableHealthy ? "ok" : "degraded",
      personal_runtime: personalHealthy ? "ok" : "degraded",
      schema_version: asNumber(durableBody.schema_version) ?? asNumber(personalBody.schema_version),
      gold_gate: asString(goldBenchmark.status),
      details: {
        backend: {
          status: backendStatus,
          http_status: backendProbe.http_status,
          version: asString(backendProbe.body.version),
        },
        durable: {
          status: durableHealthy ? "ok" : "degraded",
          http_status: durableProbe.http_status,
          enabled: durableBody.enabled === true,
          authority: asString(durableBody.authority),
          healthy: durableBody.healthy === true,
          db_connected: durableBody.db_connected === true,
          schema_version: asNumber(durableBody.schema_version),
          queue_depth: asNumber(durableBody.queue_depth),
          active_leases: asNumber(durableBody.active_leases),
          pending_outbox: asNumber(durableBody.pending_outbox),
          quarantined_count: asNumber(durableBody.quarantined_count),
        },
        personal_runtime: {
          status: personalHealthy ? "ok" : "degraded",
          http_status: personalProbe.http_status,
          enabled: personalBody.enabled === true,
          authority: asString(personalBody.authority),
          healthy: personalBody.healthy === true,
          schema_version: asNumber(personalBody.schema_version),
          gold_gate: asString(goldBenchmark.status),
          gold_approved: asNumber(goldBenchmark.approved_count),
          latest_eval_run: asString(goldBenchmark.eval_run_id),
        },
      },
    },
    {
      status: healthy ? 200 : 503,
      headers: { "cache-control": "no-store" },
    },
  );
};
