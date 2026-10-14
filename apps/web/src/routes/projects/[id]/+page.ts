/** Route param handoff for Project Brain detail page */
export function load({ params }: { params: { id: string } }) {
	return { projectId: params.id };
}
