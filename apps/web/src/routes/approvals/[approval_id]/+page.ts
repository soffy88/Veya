/** Route param handoff (same pattern as routes/workbench/[task_id]). */
export function load({ params }: { params: { approval_id: string } }) {
	return { approvalId: params.approval_id };
}
