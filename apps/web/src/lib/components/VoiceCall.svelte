<script lang="ts">
	/**
	 * VoiceCall — 连续可打断的实时语音对话浮层。
	 *
	 * 麦克风 → AudioWorklet (原生采样率 → 16kHz/16-bit PCM) → 二进制帧发到
	 * /api/v1/voice/ws → 后端 VoiceAgent.run_streaming() 做 VAD/断句/STT/
	 * (走 Master Brain 同一个 session_id)/TTS → 收到的二进制帧原样是 TTS PCM
	 * 音频块, 排队播放; JSON 控制消息 (state/transcript) 驱动状态显示 + 把这轮
	 * 写进 sessionStore, 跟文字聊天记录合流。
	 *
	 * 打断: 后端一旦检测到用户在 Agent 说话时开口, 会推一条
	 * {type:"state", state:"interrupted"} — 收到就立刻停播已排队的音频。
	 */
	import { onDestroy } from "svelte";
	import { Loader2, Mic, PhoneOff } from "lucide-svelte";
	import { API_BASE } from "$lib/api";
	import { sessionStore } from "$lib/sessionStore.svelte";

	interface Props {
		sid: string;
		onClose: () => void;
	}

	let { sid, onClose }: Props = $props();

	type CallState =
		| "connecting"
		| "listening"
		| "thinking"
		| "speaking"
		| "interrupted"
		| "error"
		| "ended";

	let callState = $state<CallState>("connecting");
	let liveTranscript = $state("");
	let errorMsg = $state("");

	let ws: WebSocket | null = null;
	let micStream: MediaStream | null = null;
	let captureCtx: AudioContext | null = null;
	let workletNode: AudioWorkletNode | null = null;
	let playCtx: AudioContext | null = null;
	let playSampleRate = 24000;
	let playQueueTime = 0;
	let playingSources: AudioBufferSourceNode[] = [];
	let ended = false;

	const STATE_LABEL: Record<CallState, string> = {
		connecting: "正在连接…",
		listening: "聆听中…",
		thinking: "思考中…",
		speaking: "回答中…",
		interrupted: "已打断",
		error: "出错了",
		ended: "已挂断",
	};

	function buildWsUrl(): string {
		const base = API_BASE || `${location.protocol}//${location.host}`;
		const wsBase = base.replace(/^http/, "ws");
		return `${wsBase}/api/v1/voice/ws?session_id=${encodeURIComponent(sid)}`;
	}

	function scheduleAudioChunk(buf: ArrayBuffer) {
		if (!playCtx) return;
		const int16 = new Int16Array(buf);
		const float32 = new Float32Array(int16.length);
		for (let i = 0; i < int16.length; i++) {
			const s = int16[i];
			float32[i] = s < 0 ? s / 0x8000 : s / 0x7fff;
		}
		const buffer = playCtx.createBuffer(1, float32.length, playSampleRate);
		buffer.copyToChannel(float32, 0);
		const src = playCtx.createBufferSource();
		src.buffer = buffer;
		src.connect(playCtx.destination);
		const startAt = Math.max(playQueueTime, playCtx.currentTime);
		src.start(startAt);
		playQueueTime = startAt + buffer.duration;
		playingSources.push(src);
		src.onended = () => {
			playingSources = playingSources.filter((s) => s !== src);
		};
	}

	function stopPlayback() {
		for (const src of playingSources) {
			try {
				src.stop();
			} catch {
				/* 已经放完/已经停过 — 忽略 */
			}
		}
		playingSources = [];
		if (playCtx) playQueueTime = playCtx.currentTime;
	}

	function handleControlMessage(raw: string) {
		let msg: Record<string, unknown>;
		try {
			msg = JSON.parse(raw);
		} catch {
			return;
		}
		if (msg.type === "state") {
			const state = String(msg.state ?? "");
			if (state in STATE_LABEL) callState = state as CallState;
			if (state === "speaking") {
				if (typeof msg.sample_rate === "number") playSampleRate = msg.sample_rate;
				const text = typeof msg.text === "string" ? msg.text : "";
				if (text) sessionStore.append(sid, { role: "assistant", text, status: "done", steps: [] });
			}
			if (state === "interrupted") stopPlayback();
		} else if (msg.type === "transcript") {
			const text = typeof msg.text === "string" ? msg.text : "";
			if (msg.final && text) {
				liveTranscript = "";
				sessionStore.append(sid, { role: "user", text, status: "done", steps: [] });
			} else {
				liveTranscript = text;
			}
		}
	}

	async function startCall() {
		try {
			micStream = await navigator.mediaDevices.getUserMedia({
				audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
			});
		} catch (e) {
			errorMsg = `无法访问麦克风: ${e instanceof Error ? e.message : String(e)}`;
			callState = "error";
			return;
		}

		ws = new WebSocket(buildWsUrl());
		ws.binaryType = "arraybuffer";

		ws.onopen = async () => {
			try {
				captureCtx = new AudioContext();
				await captureCtx.audioWorklet.addModule("/audio/pcm-downsampler-worklet.js");
				const source = captureCtx.createMediaStreamSource(micStream as MediaStream);
				workletNode = new AudioWorkletNode(captureCtx, "pcm-downsampler");
				workletNode.port.onmessage = (ev) => {
					if (ws && ws.readyState === WebSocket.OPEN) ws.send(ev.data as ArrayBuffer);
				};
				source.connect(workletNode);

				playCtx = new AudioContext();
				playQueueTime = 0;
				callState = "listening";
			} catch (e) {
				errorMsg = `初始化音频失败: ${e instanceof Error ? e.message : String(e)}`;
				callState = "error";
			}
		};

		ws.onmessage = (ev) => {
			if (ev.data instanceof ArrayBuffer) {
				scheduleAudioChunk(ev.data);
			} else if (typeof ev.data === "string") {
				handleControlMessage(ev.data);
			}
		};

		ws.onerror = () => {
			if (!ended) {
				errorMsg = "语音连接出错";
				callState = "error";
			}
		};
		ws.onclose = () => {
			if (!ended) callState = "ended";
		};
	}

	function endCall() {
		ended = true;
		callState = "ended";
		ws?.close();
		ws = null;
		workletNode?.port.close();
		workletNode?.disconnect();
		workletNode = null;
		micStream?.getTracks().forEach((t) => t.stop());
		micStream = null;
		void captureCtx?.close();
		captureCtx = null;
		stopPlayback();
		void playCtx?.close();
		playCtx = null;
		onClose();
	}

	onDestroy(() => {
		if (!ended) endCall();
	});

	void startCall();
</script>

<div class="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm">
	<div class="flex w-80 flex-col items-center gap-4 rounded-2xl border border-white/10 bg-[#0d0d0d] p-6 shadow-2xl">
		<div
			class="flex size-20 items-center justify-center rounded-full transition {callState === 'speaking'
				? 'bg-sky-500/20 text-sky-400'
				: callState === 'listening'
					? 'bg-emerald-500/20 text-emerald-400'
					: callState === 'error'
						? 'bg-rose-500/20 text-rose-400'
						: 'bg-white/10 text-white/60'}"
		>
			{#if callState === "connecting" || callState === "thinking"}
				<Loader2 class="size-8 animate-spin" />
			{:else}
				<Mic class="size-8 {callState === 'listening' ? 'animate-pulse' : ''}" />
			{/if}
		</div>
		<div class="font-mono text-sm text-white/70">{STATE_LABEL[callState]}</div>
		{#if liveTranscript}
			<div class="max-h-16 overflow-y-auto text-center text-xs text-white/40">{liveTranscript}</div>
		{/if}
		{#if errorMsg}
			<div class="text-center text-xs text-rose-400">{errorMsg}</div>
		{/if}
		<button
			type="button"
			onclick={endCall}
			title="挂断"
			class="flex size-12 items-center justify-center rounded-full bg-rose-500 text-white transition hover:bg-rose-400"
		>
			<PhoneOff class="size-5" />
		</button>
	</div>
</div>
