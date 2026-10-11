/** Route param handoff: the page reads data.groupId (was missing, so the page loaded "undefined"). */
export function load({ params }: { params: { group_id: string } }) {
	return { groupId: params.group_id };
}
