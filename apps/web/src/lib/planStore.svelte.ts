/**
 * planStore — 计划状态共享 store (P5: ChatConsole 活跃计划条)。
 *
 * 订阅对话流的 plan_update SSE 事件聚合; PlanBoard 看板走独立 REST。
 * 活跃计划 = 最近更新的、且存在未完成 todo 的计划。
 */

export interface PlanTodoSnap {
	id: string;
	title: string;
	status: string;
	depends_on?: string[];
}

export interface PlanSnap {
	plan_id: string;
	objective: string;
	updated_at: string;
	todos: PlanTodoSnap[];
}

export const planStore = (() => {
	let plans = $state<Record<string, PlanSnap>>({});
	let order = $state<string[]>([]);

	function apply(ev: { plan_id: string; objective: string; todos: PlanTodoSnap[] }) {
		if (!ev.plan_id) return;
		plans[ev.plan_id] = {
			plan_id: ev.plan_id,
			objective: ev.objective ?? "",
			updated_at: new Date().toISOString(),
			todos: Array.isArray(ev.todos) ? ev.todos : [],
		};
		order = [ev.plan_id, ...order.filter((x) => x !== ev.plan_id)];
	}

	function clear() {
		plans = {};
		order = [];
	}

	/** 最近更新的、有未完成 todo 的计划 (活跃计划条用)。 */
	function active(): PlanSnap | null {
		for (const id of order) {
			const p = plans[id];
			if (p && p.todos.some((t) => t.status !== "done")) return p;
		}
		return null;
	}

	return { plans, order, apply, clear, active };
})();
