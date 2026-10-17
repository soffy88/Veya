/**
 * Channels API client — the /settings/channels page talks to the NANO-W7
 * channel endpoints through it.
 *
 * Same rule as `lib/team/api.ts`: one transport ($lib/api.ts, bearer token
 * attached), and the feature degrades rather than errors. Both endpoints are
 * flag-gated server-side and answer 404 while VEYA_CHANNELS / VEYA_MORNING_FEED
 * are off, so the page renders "not enabled" instead of a broken panel.
 */

import { api } from "$lib/api";

// ── types ───────────────────────────────────────────────────────────────

export const CHANNEL_PLATFORMS = ["telegram", "feishu", "dingtalk", "wecom"] as const;
export type ChannelPlatform = (typeof CHANNEL_PLATFORMS)[number];

export interface ChannelBindingView {
	binding_id: string;
	platform: string;
	external_chat_id: string;
	external_user_id: string;
	owner_ref: string;
	target_type: "bot" | "group";
	target_id: string;
	created_at: number;
}

export interface WebChannelStatus {
	channel: string;
	active_backend: string;
	status: "ok" | "broken" | "needs-login";
	fix_hint: string;
	last_probe_time: number;
}

export interface ChannelsStatus {
	channels_enabled: boolean;
	morning_feed_enabled: boolean;
	channel_ladder_enabled?: boolean;
	platforms: string[];
	configured: Record<string, boolean>;
	binding_count: number;
	web_channels?: Record<string, WebChannelStatus>;
}

export interface FeedConfigView {
	owner_ref: string;
	enabled: boolean;
	time: string;
	timezone: string;
	sections: string[];
	rss_urls: string[];
	push_webpush: boolean;
	push_channels: string[];
	max_items_per_section: number;
}

const FEED_SECTIONS = ["routines", "goals", "goalruns", "memory", "rss"] as const;

// ── calls ───────────────────────────────────────────────────────────────

export async function getChannelsStatus(): Promise<ChannelsStatus | null> {
	const res = await api("gateway", "api/v1/channels/status", { method: "GET" });
	return res.ok ? ((res.data as ChannelsStatus) ?? null) : null;
}

export async function listBindings(): Promise<ChannelBindingView[]> {
	const res = await api("gateway", "api/v1/channels/bindings", { method: "GET" });
	return res.ok ? ((res.data as { bindings?: ChannelBindingView[] })?.bindings ?? []) : [];
}

/** Ask the backend for a one-time code the user pastes into the external chat. */
export async function createLinkCode(input: {
	platform: ChannelPlatform;
	target_type: "bot" | "group";
	target_id: string;
}): Promise<{ code: string; expires_in: number; binding_id: string } | null> {
	const res = await api("gateway", "api/v1/channels/bind/code", {
		method: "POST",
		body: input,
	});
	return res.ok ? ((res.data as { code: string; expires_in: number; binding_id: string }) ?? null) : null;
}

export async function unbind(bindingId: string): Promise<boolean> {
	const res = await api("gateway", "api/v1/channels/unbind", {
		method: "POST",
		body: { binding_id: bindingId },
	});
	return res.ok;
}

export async function getFeedConfig(): Promise<FeedConfigView | null> {
	const res = await api("gateway", "api/v1/channels/morning-feed/config", { method: "GET" });
	return res.ok ? ((res.data as FeedConfigView) ?? null) : null;
}

export async function saveFeedConfig(config: Omit<FeedConfigView, "owner_ref">): Promise<boolean> {
	const res = await api("gateway", "api/v1/channels/morning-feed/config", {
		method: "POST",
		body: config,
	});
	return res.ok;
}

export { FEED_SECTIONS };

/** Human label for a platform id. */
export function platformLabel(platform: string): string {
	switch (platform) {
		case "telegram":
			return "Telegram";
		case "feishu":
			return "飞书 / Lark";
		case "dingtalk":
			return "钉钉 DingTalk";
		case "wecom":
			return "企业微信 WeCom";
		default:
			return platform;
	}
}