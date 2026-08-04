"""
高级可视化模块 - P4 核心能力
功能：3D图谱、交互式调试增强、架构图扩展
"""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any

import networkx as nx
import plotly.graph_objects as go


class ThreeDGraph:
    """
    3D 图谱生成器

    功能：
    1. 三维节点布局
    2. 交互式旋转/缩放
    3. 节点属性显示
    4. 多视图支持
    """

    def __init__(self):
        self.graph = nx.Graph()
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []

    def add_node(
        self, node_id: str, label: str, type: str, attributes: dict[str, Any] | None = None
    ):
        """添加节点"""
        if attributes is None:
            attributes = {}

        self.nodes[node_id] = {
            "id": node_id,
            "label": label,
            "type": type,
            "attributes": attributes,
        }
        self.graph.add_node(node_id, **self.nodes[node_id])

    def add_edge(
        self,
        source: str,
        target: str,
        type: str,
        weight: float = 1.0,
        attributes: dict[str, Any] | None = None,
    ):
        """添加边"""
        if attributes is None:
            attributes = {}

        edge_data = {
            "source": source,
            "target": target,
            "type": type,
            "weight": weight,
            "attributes": attributes,
        }
        self.edges.append(edge_data)
        self.graph.add_edge(source, target, **edge_data)

    def generate_3d_plot(self, output_format: str = "json") -> dict[str, Any]:
        """生成3D图谱"""
        try:
            # 创建3D图
            fig = go.Figure()

            # 计算3D布局
            pos_3d = nx.spring_layout(self.graph, dim=3, k=0.15, iterations=20)

            # 添加节点
            x_nodes = [pos_3d[node][0] for node in self.graph.nodes()]
            y_nodes = [pos_3d[node][1] for node in self.graph.nodes()]
            z_nodes = [pos_3d[node][2] for node in self.graph.nodes()]

            # 按类型着色
            colors = []
            for node in self.graph.nodes():
                node_type = self.nodes.get(node, {}).get("type", "unknown")
                if node_type == "function":
                    colors.append("lightblue")
                elif node_type == "class":
                    colors.append("lightgreen")
                elif node_type == "file":
                    colors.append("lightcoral")
                else:
                    colors.append("lightgray")

            # 创建散点图
            scatter = go.Scatter3d(
                x=x_nodes,
                y=y_nodes,
                z=z_nodes,
                mode="markers+text",
                marker=dict(size=8, color=colors, opacity=0.8),
                text=[self.nodes[node]["label"] for node in self.graph.nodes()],
                hoverinfo="text",
                textposition="top center",
            )

            fig.add_trace(scatter)

            # 添加边
            for edge in self.edges:
                source = edge["source"]
                target = edge["target"]

                x_edges = [pos_3d[source][0], pos_3d[target][0], None]
                y_edges = [pos_3d[source][1], pos_3d[target][1], None]
                z_edges = [pos_3d[source][2], pos_3d[target][2], None]

                line_color = "gray"
                line_width = edge.get("weight", 1.0) * 2

                fig.add_trace(
                    go.Scatter3d(
                        x=x_edges,
                        y=y_edges,
                        z=z_edges,
                        mode="lines",
                        line=dict(color=line_color, width=line_width),
                        hoverinfo="none",
                        showlegend=False,
                    )
                )

            # 设置布局
            fig.update_layout(
                title=f"3D Code Graph - {len(self.graph.nodes())} nodes",
                scene=dict(
                    xaxis=dict(showticklabels=False),
                    yaxis=dict(showticklabels=False),
                    zaxis=dict(showticklabels=False),
                    aspectmode="cube",
                ),
                margin=dict(l=0, r=0, b=0, t=50),
                height=700,
                width=900,
            )

            # 导出为JSON或图像
            if output_format == "json":
                return {
                    "status": "success",
                    "graph": {
                        "nodes": list(self.nodes.values()),
                        "edges": self.edges,
                        "layout": pos_3d,
                    },
                    "plotly_json": fig.to_json(),
                }
            elif output_format == "image":
                # 将图表保存为图像
                buf = BytesIO()
                fig.write_image(buf, format="png", width=900, height=700)
                image_data = base64.b64encode(buf.getvalue()).decode("utf-8")
                return {"status": "success", "image": image_data}
            else:
                return {"status": "error", "message": f"Unsupported format: {output_format}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def export_to_html(self, output_path: str = "3d_graph.html") -> None:
        """导出为HTML文件"""
        try:
            fig = go.Figure()

            # 计算3D布局
            pos_3d = nx.spring_layout(self.graph, dim=3, k=0.15, iterations=20)

            # 添加节点
            x_nodes = [pos_3d[node][0] for node in self.graph.nodes()]
            y_nodes = [pos_3d[node][1] for node in self.graph.nodes()]
            z_nodes = [pos_3d[node][2] for node in self.graph.nodes()]

            # 按类型着色
            colors = []
            for node in self.graph.nodes():
                node_type = self.nodes.get(node, {}).get("type", "unknown")
                if node_type == "function":
                    colors.append("lightblue")
                elif node_type == "class":
                    colors.append("lightgreen")
                elif node_type == "file":
                    colors.append("lightcoral")
                else:
                    colors.append("lightgray")

            # 创建散点图
            scatter = go.Scatter3d(
                x=x_nodes,
                y=y_nodes,
                z=z_nodes,
                mode="markers+text",
                marker=dict(size=8, color=colors, opacity=0.8),
                text=[self.nodes[node]["label"] for node in self.graph.nodes()],
                hoverinfo="text",
                textposition="top center",
            )

            fig.add_trace(scatter)

            # 添加边
            for edge in self.edges:
                source = edge["source"]
                target = edge["target"]

                x_edges = [pos_3d[source][0], pos_3d[target][0], None]
                y_edges = [pos_3d[source][1], pos_3d[target][1], None]
                z_edges = [pos_3d[source][2], pos_3d[target][2], None]

                line_color = "gray"
                line_width = edge.get("weight", 1.0) * 2

                fig.add_trace(
                    go.Scatter3d(
                        x=x_edges,
                        y=y_edges,
                        z=z_edges,
                        mode="lines",
                        line=dict(color=line_color, width=line_width),
                        hoverinfo="none",
                        showlegend=False,
                    )
                )

            # 设置布局
            fig.update_layout(
                title=f"3D Code Graph - {len(self.graph.nodes())} nodes",
                scene=dict(
                    xaxis=dict(showticklabels=False),
                    yaxis=dict(showticklabels=False),
                    zaxis=dict(showticklabels=False),
                    aspectmode="cube",
                ),
                margin=dict(l=0, r=0, b=0, t=50),
                height=700,
                width=900,
            )

            # 导出到HTML
            fig.write_html(output_path)
            print(f"3D graph exported to {output_path}")
        except Exception as e:
            print(f"Failed to export to HTML: {e}")


class InteractiveDebuggerEnhanced:
    """
    增强版交互式调试器

    功能：
    1. 变量编辑
    2. 表达式求值
    3. 步进/步过/步入
    4. 断点管理
    """

    def __init__(self):
        self.breakpoints = {}
        self.watch_expressions = []
        self.call_stack = []
        self.variables = {}
        self.current_frame = 0
        self.step_mode = "step_over"  # step_over, step_in, step_out

    def set_step_mode(self, mode: str):
        """设置步进模式"""
        if mode in ["step_over", "step_in", "step_out"]:
            self.step_mode = mode

    def evaluate_expression(self, expression: str, context: dict[str, Any]) -> Any:
        """在当前上下文中评估表达式"""
        try:
            # 简化实现：直接使用 eval
            result = eval(expression, context)
            return result
        except Exception as e:
            return str(e)

    def edit_variable(self, variable_name: str, new_value: Any):
        """编辑变量值"""
        if variable_name in self.variables:
            self.variables[variable_name] = new_value
            return True
        return False

    def get_current_variables(self) -> dict[str, Any]:
        """获取当前帧的变量"""
        if self.call_stack and self.current_frame < len(self.call_stack):
            frame = self.call_stack[self.current_frame]
            return frame.get("variables", {})
        return {}

    def step(self) -> dict[str, Any]:
        """执行一步操作"""
        if self.step_mode == "step_over" or self.step_mode == "step_in":
            self.current_frame += 1
        elif self.step_mode == "step_out":
            # 找到最近的调用栈返回点
            while self.current_frame > 0 and not self.call_stack[self.current_frame]["is_return"]:
                self.current_frame -= 1
            self.current_frame += 1

        result = {
            "status": "success",
            "action": self.step_mode,
            "current_frame": self.current_frame,
        }

        # 更新变量
        if self.call_stack and self.current_frame < len(self.call_stack):
            frame = self.call_stack[self.current_frame]
            self.variables = frame.get("variables", {})

        return result

    def get_debug_state(self) -> dict[str, Any]:
        """获取调试状态"""
        state = {
            "breakpoints": list(self.breakpoints.values()),
            "watch_expressions": self.watch_expressions,
            "call_stack": self.call_stack[:10],
            "variables": {k: str(v) for k, v in list(self.variables.items())[:50]},
            "current_frame": self.current_frame,
            "step_mode": self.step_mode,
            "status": "debugging",
        }
        return state


class ArchitectureVisualizerEnhanced:
    """
    增强版架构可视化器

    功能：
    1. 部署拓扑图
    2. 数据流图
    3. 微服务架构
    4. 容器化部署
    """

    def generate_deployment_topology(self, services: list[dict[str, Any]]) -> dict[str, Any]:
        """生成部署拓扑图"""
        topology = {"nodes": [], "edges": [], "layers": {}}

        # 按服务分组
        for service in services:
            service_id = service.get("id", f"service_{len(topology['nodes'])}")
            topology["nodes"].append(
                {
                    "id": service_id,
                    "name": service.get("name", ""),
                    "type": service.get("type", "service"),
                    "container": service.get("container", "docker"),
                    "instances": service.get("instances", 1),
                    "ports": service.get("ports", []),
                    "dependencies": service.get("dependencies", []),
                }
            )

            layer = service.get("layer", "unknown")
            if layer not in topology["layers"]:
                topology["layers"][layer] = []
            topology["layers"][layer].append(service_id)

        # 生成边
        for i, node in enumerate(topology["nodes"]):
            for j, other in enumerate(topology["nodes"]):
                if i != j and self._should_connect(node, other):
                    topology["edges"].append(
                        {
                            "source": node["id"],
                            "target": other["id"],
                            "type": self._determine_connection_type(node, other),
                        }
                    )

        return topology

    def generate_data_flow_diagram(self, components: list[dict[str, Any]]) -> dict[str, Any]:
        """生成数据流图"""
        diagram = {"nodes": [], "edges": [], "data_types": {}}

        # 添加组件
        for component in components:
            diagram["nodes"].append(
                {
                    "id": component.get("id", f"component_{len(diagram['nodes'])}"),
                    "name": component.get("name", ""),
                    "type": component.get("type", "component"),
                    "input_ports": component.get("input_ports", []),
                    "output_ports": component.get("output_ports", []),
                }
            )

        # 生成数据流边
        for component in components:
            for output_port in component.get("output_ports", []):
                for dep in component.get("dependencies", []):
                    # 查找目标组件
                    target_component = next((c for c in components if c.get("id") == dep), None)
                    if target_component:
                        for input_port in target_component.get("input_ports", []):
                            diagram["edges"].append(
                                {
                                    "source": component["id"],
                                    "target": target_component["id"],
                                    "source_port": output_port,
                                    "target_port": input_port,
                                    "data_type": output_port.get("data_type", "unknown"),
                                }
                            )

                            # 记录数据类型
                            data_type = output_port.get("data_type", "unknown")
                            if data_type not in diagram["data_types"]:
                                diagram["data_types"][data_type] = 0
                            diagram["data_types"][data_type] += 1

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


# 便捷函数
def create_three_d_graph() -> ThreeDGraph:
    """创建3D图谱"""
    return ThreeDGraph()


def create_interactive_debugger_enhanced() -> InteractiveDebuggerEnhanced:
    """创建增强版交互式调试器"""
    return InteractiveDebuggerEnhanced()


def create_architecture_visualizer_enhanced() -> ArchitectureVisualizerEnhanced:
    """创建增强版架构可视化器"""
    return ArchitectureVisualizerEnhanced()


if __name__ == "__main__":
    # 测试3D图谱
    print("=== Testing 3D Graph ===")
    graph = create_three_d_graph()

    # 添加测试节点
    graph.add_node("main.py", "main.py", "file", {"lines": 50})
    graph.add_node("utils.py", "utils.py", "file", {"lines": 100})
    graph.add_node("load_data", "load_data", "function", {"file": "utils.py"})

    graph.add_edge("main.py", "utils.py", "import", weight=1.0)
    graph.add_edge("main.py", "load_data", "call", weight=2.0)

    # 生成3D图谱
    result = graph.generate_3d_plot("json")
    print(f"Generated 3D graph with {len(result['graph']['nodes'])} nodes")

    # 导出为HTML
    graph.export_to_html("3d_graph.html")

    # 测试增强版调试器
    print("\n=== Testing Enhanced Debugger ===")
    debugger = create_interactive_debugger_enhanced()

    # 设置断点
    debugger.add_breakpoint("main.py", 42)

    # 设置变量
    debugger.variables = {"user_count": 100, "status": "active"}

    # 评估表达式
    result = debugger.evaluate_expression("user_count + 5", debugger.variables)
    print(f"Expression evaluation: {result}")

    # 步进
    step_result = debugger.step()
    print(f"Step result: {step_result}")

    # 获取调试状态
    state = debugger.get_debug_state()
    print(f"Debug state: {state.keys()}")

    # 测试架构可视化
    print("\n=== Testing Enhanced Architecture Visualizer ===")
    visualizer = create_architecture_visualizer_enhanced()

    services = [
        {
            "name": "User Service",
            "type": "service",
            "layer": "service",
            "container": "docker",
            "instances": 3,
        },
        {
            "name": "Auth Service",
            "type": "service",
            "layer": "service",
            "container": "docker",
            "instances": 2,
        },
        {
            "name": "Database",
            "type": "database",
            "layer": "database",
            "container": "postgres",
            "instances": 1,
        },
    ]

    topology = visualizer.generate_deployment_topology(services)
    print(f"Deployment topology: {len(topology['nodes'])} nodes, {len(topology['edges'])} edges")

    data_flow = visualizer.generate_data_flow_diagram(
        [
            {
                "name": "API Gateway",
                "type": "gateway",
                "input_ports": [{"name": "request", "data_type": "json"}],
                "output_ports": [{"name": "response", "data_type": "json"}],
            },
            {
                "name": "UserService",
                "type": "service",
                "input_ports": [{"name": "user_request", "data_type": "json"}],
                "output_ports": [{"name": "user_response", "data_type": "json"}],
            },
            {
                "name": "AuthService",
                "type": "service",
                "input_ports": [{"name": "auth_request", "data_type": "json"}],
                "output_ports": [{"name": "auth_response", "data_type": "json"}],
            },
        ]
    )
    print(f"Data flow diagram: {len(data_flow['nodes'])} nodes, {len(data_flow['edges'])} edges")

    # 输出数据类型统计
    print(f"Data types: {data_flow['data_types']}")
