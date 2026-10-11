/** Route param handoff: the page reads data.botId (was missing, so the page loaded "undefined"). */
export function load({ params }: { params: { bot_id: string } }) {
	return { botId: params.bot_id };
}
