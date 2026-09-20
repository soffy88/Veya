/** Route param handoff (same pattern as routes/workbench/[task_id]). */
export function load({ params }: { params: { mission_id: string } }) {
	return { missionId: params.mission_id };
}
