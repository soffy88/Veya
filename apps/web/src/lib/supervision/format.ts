/**
 * Display formatting for supervision data.
 *
 * This module formats; it never decides. Every label/tone comes from the literal
 * backend value — `executed` is not rendered as success, `INTERRUPTED` is not
 * hidden, and nothing is collapsed into a synthetic "已完成".
 */

import type { ExecutionReport, MissionStatus, MissionEvent, SupervisorReview } from "./types";

export type Tone = "neutral" | "progress" | "waiting" | "attention" | "ok" | "bad";

const STATUS_LABELS: Record<string, string> = {
  CREATED: "已创建",
  DESIGNING: "设计中",
  EXECUTING: "执行中",
  COLLECTING_EVIDENCE: "收集证据",
  FAST_DECISION: "快速决策",
  REVIEWING: "审查中",
  RETASKING: "返工中",
  WAITING_EXTERNAL_SUPERVISOR: "等待 ChatGPT 审查",
  WAITING_OWNER: "等待你处理",
  ACCEPTED: "已接受",
  DONE: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
  INTERRUPTED: "被中断",
  // report statuses (canonical values, verbatim meaning)
  executed: "已执行（不代表成功）",
};

const STATUS_TONES: Record<string, Tone> = {
  CREATED: "neutral",
  DESIGNING: "progress",
  EXECUTING: "progress",
  COLLECTING_EVIDENCE: "progress",
  FAST_DECISION: "progress",
  REVIEWING: "progress",
  RETASKING: "progress",
  WAITING_EXTERNAL_SUPERVISOR: "waiting",
  WAITING_OWNER: "attention",
  ACCEPTED: "ok",
  DONE: "ok",
  FAILED: "bad",
  CANCELLED: "neutral",
  INTERRUPTED: "attention",
  executed: "neutral",
};

export function statusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return STATUS_LABELS[status] ?? status;
}

export function statusTone(status: string | null | undefined): Tone {
  if (!status) return "neutral";
  return STATUS_TONES[status] ?? "neutral";
}

/** True only when the backend literally reports a terminal-status string. */
export function isTerminal(status: string | null | undefined): boolean {
  return status === "ACCEPTED" || status === "DONE" || status === "FAILED" || status === "CANCELLED";
}

export function isWaitingOnHuman(status: string | null | undefined): boolean {
  return status === "WAITING_OWNER" || status === "WAITING_EXTERNAL_SUPERVISOR";
}

export function supervisionModeLabel(mode: string | null | undefined): string {
  if (mode === "external") return "ChatGPT 监督";
  if (mode === "internal") return "Veya 自主";
  if (mode === "auto") return "自动";
  return mode ?? "—";
}

export function executorLabel(executor: string | null | undefined): string {
  if (!executor) return "自动";
  if (executor === "dsh") return "DSH";
  if (executor === "hicode") return "Hicode";
  return executor;
}

export function formatTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  const date = new Date(ts * 1000);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

export function relativeTime(ts: number | null | undefined, now = Date.now()): string {
  if (!ts) return "—";
  const delta = Math.max(0, now - ts * 1000);
  const minutes = Math.floor(delta / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

/** Short, display-only summary of a report. Never a verdict. */
export function reportHeadline(report: ExecutionReport | null | undefined): string {
  if (!report) return "尚无执行报告";
  const parts = [
    `#${report.iteration}`,
    statusLabel(report.status),
    `${report.changes?.length ?? 0} 变更`,
    `${report.artifacts?.length ?? 0} 产物`,
    `${report.failures?.length ?? 0} 失败`,
  ];
  return parts.join(" · ");
}

export function reviewHeadline(review: SupervisorReview | null | undefined): string {
  if (!review) return "尚无审查";
  return `#${review.iteration} ${review.supervisor} → ${review.decision}${review.reason ? `（${review.reason}）` : ""}`;
}

/** Human-readable topic label for the live event list. */
export function eventLabel(event: MissionEvent): string {
  const topic = event.topic ?? "UNKNOWN";
  const map: Record<string, string> = {
    MISSION_CREATED: "任务创建",
    SUPERVISOR_SELECTED: "选定监督者",
    SUPERVISION_MODE_CHANGED: "监督模式切换",
    EXECUTOR_STARTED: "开始执行",
    EXECUTOR_COMPLETED: "执行结束",
    EXECUTION_INTERRUPTED: "执行中断",
    EXECUTION_PROCESS_GROUP_TERMINATED: "回收残留进程",
    EXCEPTION: "异常",
    EVIDENCE_COLLECTED: "收集证据",
    JEV_DECISION: "Jev 决策",
    ESCALATED: "升级给 owner",
    REVIEW_APPLIED: "应用审查",
    MISSION_CANCELLED: "任务取消",
  };
  return map[topic] ?? topic;
}

/** Fields of an artifact entry that can be opened in the UI. */
export function artifactPath(entry: Record<string, unknown>): string {
  return String(entry.path ?? entry.artifact ?? entry.url ?? "");
}
