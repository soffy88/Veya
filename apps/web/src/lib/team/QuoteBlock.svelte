<script lang="ts">
	/**
	 * One quoted message, rendered above the message that quotes it
	 * (Codync `replyTo` method, adapted). The quote is a snapshot the
	 * server built at send time: author + the first 500 characters.
	 */
	let {
		quote,
		authorName = null,
	}: {
		quote: { id: string; author: string; text: string };
		authorName?: string | null;
	} = $props();
</script>

<div
	class="mb-1.5 rounded-lg border-l-2 border-sky-400/60 bg-black/25 px-2.5 py-1.5 text-xs leading-snug"
>
	<p class="font-medium opacity-70">{authorName ?? quote.author}</p>
	<p class="line-clamp-3 whitespace-pre-wrap break-words opacity-80">{quote.text}</p>
</div>
