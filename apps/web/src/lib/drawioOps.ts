/**
 * drawioOps — client-side mxGraph XML helpers for the draw.io artifact type.
 *
 * wrapMxCellsXml mirrors platform/3O/oskill/oskill/drawio_diagram.py's
 * wrap_mxcells_xml() exactly (same boilerplate) — kept in sync manually since
 * oservi (the Master Brain backend) is deliberately host/sibling-module
 * agnostic and must not import drawio-specific code, so the wrapping happens
 * once server-side (for validation) and once here (for rendering).
 *
 * applyDiagramOperations() applies edit_diagram's structured operations to an
 * already-rendered full mxfile document — this is where "current diagram
 * state" lives (client-side), since the Master Brain backend is a shared
 * singleton across sessions and doesn't track per-session diagram state.
 */

export type DiagramOperation =
	| { op: "add_node"; id: string; label?: string; x?: number; y?: number; width?: number; height?: number; style?: string }
	| { op: "add_edge"; id: string; source: string; target: string; label?: string; style?: string }
	| { op: "update_cell"; id: string; label?: string; x?: number; y?: number; width?: number; height?: number; style?: string }
	| { op: "delete_cell"; id: string };

const DEFAULT_NODE_STYLE = "rounded=0;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;fontSize=12;";
const DEFAULT_EDGE_STYLE = "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;fontSize=11;";

/** 把裸 mxCell 片段包成完整 mxfile XML — 与后端 wrap_mxcells_xml() 保持字节级一致。 */
export function wrapMxCellsXml(innerXml: string, pageWidth = 800, pageHeight = 600): string {
	return (
		`<mxfile><diagram name="Page-1">` +
		`<mxGraphModel dx="0" dy="0" grid="1" gridSize="10" ` +
		`pageWidth="${pageWidth}" pageHeight="${pageHeight}">` +
		`<root><mxCell id="0"/><mxCell id="1" parent="0"/>` +
		innerXml +
		`</root></mxGraphModel></diagram></mxfile>`
	);
}

function findCell(root: Element, id: string): Element | null {
	// CSS.escape 保证 id 里含引号等特殊字符时选择器不炸
	return root.querySelector(`mxCell[id="${CSS.escape(id)}"]`);
}

function setGeometry(doc: XMLDocument, cell: Element, attrs: { x?: number; y?: number; width?: number; height?: number }, relative: boolean) {
	let geo = cell.querySelector("mxGeometry");
	if (!geo) {
		geo = doc.createElement("mxGeometry");
		geo.setAttribute("as", "geometry");
		if (relative) geo.setAttribute("relative", "1");
		cell.appendChild(geo);
	}
	if (attrs.x !== undefined) geo.setAttribute("x", String(attrs.x));
	if (attrs.y !== undefined) geo.setAttribute("y", String(attrs.y));
	if (attrs.width !== undefined) geo.setAttribute("width", String(attrs.width));
	if (attrs.height !== undefined) geo.setAttribute("height", String(attrs.height));
}

/** 把 edit_diagram 的结构化操作应用到一份完整 mxfile XML 上, 返回新文档 + 应用失败的操作说明。 */
export function applyDiagramOperations(
	xml: string,
	operations: DiagramOperation[],
): { xml: string; errors: string[] } {
	const errors: string[] = [];
	const doc = new DOMParser().parseFromString(xml, "application/xml");
	if (doc.querySelector("parsererror")) {
		return { xml, errors: ["base diagram XML is not well-formed, cannot apply edits"] };
	}
	const root = doc.querySelector("root");
	if (!root) {
		return { xml, errors: ["base diagram XML has no <root> element"] };
	}

	for (const op of operations) {
		if (op.op === "add_node") {
			if (findCell(root, op.id)) {
				errors.push(`add_node ${op.id}: id already exists`);
				continue;
			}
			const cell = doc.createElement("mxCell");
			cell.setAttribute("id", op.id);
			cell.setAttribute("value", op.label ?? "");
			cell.setAttribute("style", op.style ?? DEFAULT_NODE_STYLE);
			cell.setAttribute("vertex", "1");
			cell.setAttribute("parent", "1");
			root.appendChild(cell);
			setGeometry(
				doc,
				cell,
				{ x: op.x ?? 0, y: op.y ?? 0, width: op.width ?? 120, height: op.height ?? 60 },
				false,
			);
		} else if (op.op === "add_edge") {
			if (findCell(root, op.id)) {
				errors.push(`add_edge ${op.id}: id already exists`);
				continue;
			}
			if (!findCell(root, op.source) || !findCell(root, op.target)) {
				errors.push(`add_edge ${op.id}: source/target cell not found`);
				continue;
			}
			const cell = doc.createElement("mxCell");
			cell.setAttribute("id", op.id);
			cell.setAttribute("value", op.label ?? "");
			cell.setAttribute("style", op.style ?? DEFAULT_EDGE_STYLE);
			cell.setAttribute("edge", "1");
			cell.setAttribute("parent", "1");
			cell.setAttribute("source", op.source);
			cell.setAttribute("target", op.target);
			root.appendChild(cell);
			setGeometry(doc, cell, {}, true);
		} else if (op.op === "update_cell") {
			const cell = findCell(root, op.id);
			if (!cell) {
				errors.push(`update_cell ${op.id}: cell not found`);
				continue;
			}
			if (op.label !== undefined) cell.setAttribute("value", op.label);
			if (op.style !== undefined) cell.setAttribute("style", op.style);
			if (op.x !== undefined || op.y !== undefined || op.width !== undefined || op.height !== undefined) {
				setGeometry(doc, cell, { x: op.x, y: op.y, width: op.width, height: op.height }, cell.getAttribute("edge") === "1");
			}
		} else if (op.op === "delete_cell") {
			const cell = findCell(root, op.id);
			if (!cell) {
				errors.push(`delete_cell ${op.id}: cell not found`);
				continue;
			}
			// 级联删掉引用这个 id 的边, 避免留下指向不存在节点的悬空边
			root.querySelectorAll(`mxCell[source="${CSS.escape(op.id)}"], mxCell[target="${CSS.escape(op.id)}"]`).forEach(
				(e) => e.remove(),
			);
			cell.remove();
		}
	}

	return { xml: new XMLSerializer().serializeToString(doc), errors };
}
