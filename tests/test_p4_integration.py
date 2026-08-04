# P4 Integration Tests


import pytest

from veya.advanced_visualization import (
    create_architecture_visualizer_enhanced,
    create_interactive_debugger_enhanced,
    create_three_d_graph,
)


class Test3DGraph:
    def test_3d_graph_creation(self):
        graph = create_three_d_graph()
        graph.add_node("main.py", "main.py", "file", {"lines": 50})
        graph.add_node("utils.py", "utils.py", "file", {"lines": 100})
        graph.add_edge("main.py", "utils.py", "import")
        result = graph.generate_3d_plot("json")
        assert result["status"] == "success"
        assert len(result["graph"]["nodes"]) == 2


class TestDebugger:
    def test_debugger_evaluation(self):
        debugger = create_interactive_debugger_enhanced()
        debugger.variables = {"x": 5, "y": 10}
        result = debugger.evaluate_expression("x + y", debugger.variables)
        assert result == 15

    def test_step_debug(self):
        debugger = create_interactive_debugger_enhanced()
        result = debugger.step()
        assert result["current_frame"] == 1


class TestArchitecture:
    def test_deployment_topology(self):
        visualizer = create_architecture_visualizer_enhanced()
        services = [{"name": "Service1", "type": "service", "layer": "service"}]
        topology = visualizer.generate_deployment_topology(services)
        assert len(topology["nodes"]) == 1

    def test_data_flow_diagram(self):
        visualizer = create_architecture_visualizer_enhanced()
        components = [{"name": "Component1", "type": "component"}]
        diagram = visualizer.generate_data_flow_diagram(components)
        assert len(diagram["nodes"]) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
