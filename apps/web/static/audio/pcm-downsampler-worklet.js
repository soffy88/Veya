// pcm-downsampler-worklet.js — AudioWorklet processor for VoiceCall.svelte.
//
// Runs off the main thread. Takes the browser's native-rate Float32 mic
// samples (48kHz/44.1kHz typically) and decimates them down to the fixed
// 16kHz/16-bit PCM the veya-backend voice pipeline expects
// (veya/oprim/types.py::AudioConfig defaults), 20ms frames at a time.
class PCMDownsamplerProcessor extends AudioWorkletProcessor {
	constructor() {
		super();
		this.targetRate = 16000;
		this.frameSamples = 320; // 20ms @ 16kHz
		this.acc = []; // int16 samples pending flush
		this.pos = 0; // fractional read position into the native-rate stream
	}

	process(inputs) {
		const channelData = inputs[0] && inputs[0][0];
		if (!channelData || channelData.length === 0) return true;

		const ratio = sampleRate / this.targetRate; // `sampleRate` = AudioWorkletGlobalScope native rate
		let i = this.pos;
		while (i < channelData.length) {
			const idx = Math.floor(i);
			const s = Math.max(-1, Math.min(1, channelData[idx]));
			this.acc.push(s < 0 ? s * 0x8000 : s * 0x7fff);
			if (this.acc.length >= this.frameSamples) {
				const frame = new Int16Array(this.acc.splice(0, this.frameSamples));
				this.port.postMessage(frame.buffer, [frame.buffer]);
			}
			i += ratio;
		}
		this.pos = i - channelData.length;

		return true;
	}
}

registerProcessor("pcm-downsampler", PCMDownsamplerProcessor);
