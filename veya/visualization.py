"""
代码可视化模块 - P3 核心能力
功能：代码图谱、架构图、依赖可视化、交互式调试
"""

from __future__ import annotations

import json

import matplotlib
import networkx as nx

matplotlib.use("Agg")  # 不显示 GUI
import base64
import hashlib
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

import matplotlib.pyplot as plt


@dataclass
class GraphNode:
    """图节点"""

    node_id: str
    label: str
    type: str  # file, function, class, module
    attributes: dict[str, Any] = field(default_factory=dict)
    position: tuple[float, float] | None = None
    size: float = 0.0


@dataclass
class GraphEdge:
    """图边"""

    source: str
    target: str
    type: str  # import, call, inherit, contain
    weight: float = 1.0
    attributes: dict[str, Any] = field(default_factory=dict)


class CodeGraph:
    """
    代码图谱生成器

    功能：
    1. 代码结构可视化
    2. 依赖关系分析
    3. 模块耦合度计算
    4. 架构图生成
    """

    def __init__(self):
        self.graph = nx.Graph()
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []

    def add_node(self, node: GraphNode):
        """添加节点"""
        self.nodes[node.node_id] = node
        self.graph.add_node(node.node_id, **node.__dict__)

    def add_edge(self, edge: GraphEdge):
        """添加边"""
        self.edges.append(edge)
        self.graph.add_edge(edge.source, edge.target, type=edge.type, weight=edge.weight)

    def analyze_from_ast(self, ast_analyzer) -> None:
        """从 AST 分析器构建图谱"""
        # 添加符号节点
        for symbol_id, symbol in ast_analyzer.symbols.items():
            node = GraphNode(
                node_id=symbol_id,
                label=symbol.name,
                type=symbol.type,
                attributes={
                    "file": symbol.file_path,
                    "line": symbol.line,
                    "docstring": symbol.docstring[:100] if symbol.docstring else "",
                },
            )
            self.add_node(node)

        # 添加依赖边
        for dep in ast_analyzer.dependencies:
            edge = GraphEdge(source=dep.source, target=dep.target, type=dep.type)
            self.add_edge(edge)

    def calculate_metrics(self) -> dict[str, Any]:
        """计算图谱指标"""
        try:
            return {
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "average_degree": sum(dict(self.graph.degree()).values()) / len(self.nodes)
                if self.nodes
                else 0,
                "density": nx.density(self.graph),
                "connected_components": nx.number_connected_components(self.graph),
                "centrality": self._calculate_centrality(),
                "coupling": self._calculate_coupling(),
            }
        except Exception:
            return {"error": "Failed to calculate metrics"}

    def _calculate_centrality(self) -> dict[str, list[tuple[str, float]]]:
        """计算中心性指标"""
        centrality = {}

        try:
            # 度中心性
            degree_centrality = nx.degree_centrality(self.graph)
            centrality["degree"] = sorted(
                degree_centrality.items(), key=lambda x: x[1], reverse=True
            )[:10]

            # 介数中心性（如果节点数太多可能计算慢）
            if len(self.nodes) < 100:
                betweenness = nx.betweenness_centrality(self.graph)
                centrality["betweenness"] = sorted(
                    betweenness.items(), key=lambda x: x[1], reverse=True
                )[:10]
        except Exception:
            pass

        return centrality

    def _calculate_coupling(self) -> dict[str, Any]:
        """计算模块耦合度"""
        # 简单的耦合度计算
        coupling = {"high_coupling_nodes": [], "low_coupling_nodes": []}

        for node_id in self.nodes:
            neighbors = list(self.graph.neighbors(node_id))
            degree = len(neighbors)

            if degree > 5:  # 高耦合
                coupling["high_coupling_nodes"].append(
                    {"node": node_id, "degree": degree, "neighbors": neighbors[:5]}
                )
            elif degree <= 1:  # 低耦合
                coupling["low_coupling_nodes"].append({"node": node_id, "degree": degree})

        return coupling

    def generate_image(self, output_path: str | None = None) -> bytes | None:
        """生成图谱图像"""
        if len(self.nodes) == 0:
            return None

        try:
            plt.figure(figsize=(12, 8))
            plt.title(f"Code Graph - {len(self.nodes)} nodes, {len(self.edges)} edges")

            # 布局
            pos = nx.spring_layout(self.graph, k=0.15, iterations=20)

            # 按类型着色
            node_colors = []
            for node in self.graph.nodes():
                node_type = self.nodes.get(node, GraphNode(node, "", "unknown")).type
                if node_type == "function":
                    node_colors.append("lightblue")
                elif node_type == "class":
                    node_colors.append("lightgreen")
                elif node_type == "file":
                    node_colors.append("lightcoral")
                else:
                    node_colors.append("lightgray")

            # 绘制
            nx.draw_networkx_nodes(self.graph, pos, node_color=node_colors, node_size=300)
            nx.draw_networkx_edges(self.graph, pos, alpha=0.3)
            nx.draw_networkx_labels(self.graph, pos, font_size=8)

            plt.axis("off")

            if output_path:
                plt.savefig(output_path, dpi=150, bbox_inches="tight")
                plt.close()
                return None
            else:
                # 保存到内存并返回 base64
                buf = BytesIO()
                plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
                plt.close()
                return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as e:
            print(f"[Visualization] Failed to generate graph: {e}")
            return None

    def export_to_json(self) -> str:
        """导出为 JSON"""
        data = {
            "nodes": [node.__dict__ for node in self.nodes.values()],
            "edges": [edge.__dict__ for edge in self.edges],
            "metrics": self.calculate_metrics(),
        }
        return json.dumps(data, indent=2)

    def export_to_cytoscape(self) -> dict[str, Any]:
        """导出为 Cytoscape 格式"""
        elements = []

        for node in self.nodes.values():
            elements.append(
                {
                    "data": {
                        "id": node.node_id,
                        "label": node.label,
                        "type": node.type,
                        **node.attributes,
                    }
                }
            )

        for edge in self.edges:
            elements.append(
                {
                    "data": {
                        "source": edge.source,
                        "target": edge.target,
                        "type": edge.type,
                        "weight": edge.weight,
                    }
                }
            )

        return {
            "elements": elements,
            "layout": {"name": "cose"},
            "style": self._get_cytoscape_style(),
        }

    def _get_cytoscape_style(self) -> list[dict[str, Any]]:
        """Cytoscape 样式"""
        return [
            {
                "selector": "node",
                "style": {
                    "label": "data(label)",
                    "background-color": "data(color)",
                    "shape": "data(type)",
                },
            },
            {
                "selector": "edge",
                "style": {"line-color": "#666", "width": "data(weight)", "curve-style": "bezier"},
            },
        ]


class ArchitectureVisualizer:
    """
    架构可视化器

    功能：
    1. 系统架构图
    2. 组件关系图
    3. 数据流图
    4. 部署拓扑图
    """

    def generate_architecture_diagram(self, components: list[dict[str, Any]]) -> dict[str, Any]:
        """生成架构图"""
        diagram = {"nodes": [], "edges": [], "layers": {}}

        # 按层级组织
        for component in components:
            layer = component.get("layer", "unknown")
            if layer not in diagram["layers"]:
                diagram["layers"][layer] = []

            node_id = component.get("name", f"node_{len(diagram['nodes'])}")
            diagram["nodes"].append(
                {
                    "id": node_id,
                    "name": component.get("name", ""),
                    "type": component.get("type", ""),
                    "layer": layer,
                    "description": component.get("description", ""),
                }
            )
            diagram["layers"][layer].append(node_id)

        # 生成边（基于依赖关系）
        for i, node in enumerate(diagram["nodes"]):
            for j, other in enumerate(diagram["nodes"]):
                if i != j and self._should_connect(node, other):
                    diagram["edges"].append(
                        {
                            "source": node["id"],
                            "target": other["id"],
                            "type": self._determine_connection_type(node, other),
                        }
                    )

        return diagram

    def _should_connect(self, node: dict[str, Any], other: dict[str, Any]) -> bool:
        """判断是否应该连接两个节点"""
        node_layer = node.get("layer")
        other_layer = other.get("layer")

        # 简化逻辑：上层依赖下层
        layer_order = ["frontend", "api", "service", "repository", "database"]
        if node_layer in layer_order and other_layer in layer_order:
            node_idx = layer_order.index(node_layer)
            other_idx = layer_order.index(other_layer)
            # 只允许上层到下层或同层的连接
            return node_idx <= other_idx + 1

        return False

    def _determine_connection_type(self, node: dict[str, Any], other: dict[str, Any]) -> str:
        """确定连接类型"""
        if node.get("layer") == other.get("layer"):
            return "peer"
        elif node.get("layer") == "database" or other.get("layer") == "database":
            return "data_access"
        else:
            return "dependency"


class InteractiveDebugger:
    """
    交互式调试器

    功能：
    1. 实时变量查看
    2. 调用栈可视化
    3. 断点管理
    4. 步进调试
    """

    def __init__(self):
        self.breakpoints = {}
        self.watch_expressions = []
        self.call_stack = []
        self.variables = {}

    def add_breakpoint(self, file_path: str, line: int, condition: str | None = None) -> str:
        """添加断点"""
        breakpoint_id = hashlib.md5(f"{file_path}:{line}".encode()).hexdigest()
        self.breakpoints[breakpoint_id] = {
            "file": file_path,
            "line": line,
            "condition": condition,
            "hit_count": 0,
        }
        return breakpoint_id

    def remove_breakpoint(self, breakpoint_id: str) -> bool:
        """移除断点"""
        if breakpoint_id in self.breakpoints:
            del self.breakpoints[breakpoint_id]
            return True
        return False

    def add_watch_expression(self, expression: str) -> str:
        """添加监视表达式"""
        watch_id = hashlib.md5(expression.encode()).hexdigest()
        self.watch_expressions.append({"id": watch_id, "expression": expression, "value": None})
        return watch_id

    def update_call_stack(self, stack_frames: list[dict[str, Any]]):
        """更新调用栈"""
        self.call_stack = stack_frames

    def update_variables(self, variables: dict[str, Any]):
        """更新变量"""
        self.variables.update(variables)

    def get_debug_state(self) -> dict[str, Any]:
        """获取调试状态"""
        return {
            "breakpoints": list(self.breakpoints.values()),
            "watch_expressions": self.watch_expressions,
            "call_stack": self.call_stack[:10],  # 只保留最近 10 帧
            "variables": {k: str(v) for k, v in list(self.variables.items())[:50]},  # 限制变量数量
            "status": "debugging",
        }


# 便捷函数
def create_code_graph() -> CodeGraph:
    """创建代码图谱"""
    return CodeGraph()


def create_architecture_visualizer() -> ArchitectureVisualizer:
    """创建架构可视化器"""
    return ArchitectureVisualizer()


def create_interactive_debugger() -> InteractiveDebugger:
    """创建交互式调试器"""
    return InteractiveDebugger()


if __name__ == "__main__":
    # 测试代码图谱
    print("=== Testing Code Graph ===")
    graph = create_code_graph()

    # 添加测试节点
    graph.add_node(GraphNode("main.py", "main.py", "file", {"lines": 50}))
    graph.add_node(GraphNode("utils.py", "utils.py", "file", {"lines": 109}))
    graph.add_node(GraphNode("load_data", "load_data", "function", {"file": "utils.py"}))

    graph.add_edge(GraphEdge("main.py", "utils.py", "import"))
    graph.add_edge(GraphEdge("main.py", "load_data", "call"))

    # 计算指标
    metrics = graph.calculate_metrics()
    print(f"Graph metrics: {metrics}")

    # 导出 JSON
    json_output = graph.export_to_json()
    print(f"JSON size: {len(json_output)} bytes")

    # 生成图像（如果安装了 matplotlib）
    try:
        image_data = graph.generate_image()
        if image_data:
            print(f"Generated graph image (base64): {len(image_data)} chars")
    except Exception as e:
        print(f"Image generation skipped: {e}")

    # 测试架构可视化
    print("\n=== Testing Architecture Visualizer ===")
    visualizer = create_architecture_visualizer()
    components = [
        {"name": "frontend", "type": "react", "layer": "frontend", "description": "用户界面"},
        {"name": "api_server", "type": "fastapi", "layer": "api", "description": "API 服务"},
        {"name": "auth_service", "type": "service", "layer": "service", "description": "认证服务"},
        {"name": "user_db", "type": "postgresql", "layer": "database", "description": "用户数据库"},
    ]
    diagram = visualizer.generate_architecture_diagram(components)
    print(f"Architecture diagram: {len(diagram['nodes'])} nodes, {len(diagram['edges'])} edges")

    # 测试交互式调试器
    print("\n=== Testing Interactive Debugger ===")
    debugger = create_interactive_debugger()
    debugger.add_breakpoint("main.py", 42)
    debugger.add_watch_expression("user_count")
    debugger.update_variables({"user_count": 100, "status": "active"})

    state = debugger.get_debug_state()
    print(f"Debug state: {state.keys()}")
