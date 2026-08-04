"""
高级可视化 API - P4 核心能力
提供3D图谱、交互式调试增强、架构图扩展等功能
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hicode.advanced_visualization import (
    create_architecture_visualizer_enhanced,
    create_interactive_debugger_enhanced,
    create_three_d_graph,
)

router = APIRouter(prefix="/advanced-visualization", tags=["advanced-visualization"])

# 全局实例
three_d_graph = create_three_d_graph()
interactive_debugger = create_interactive_debugger_enhanced()
architecture_visualizer = create_architecture_visualizer_enhanced()


class Generate3DGraphRequest(BaseModel):
    """生成3D图谱请求"""

    ast_data: dict[str, Any]
    output_format: str = "json"  # json, image, html


class DebugExpressionRequest(BaseModel):
    """调试表达式请求"""

    expression: str
    context: dict[str, Any]


class StepDebugRequest(BaseModel):
    """步进调试请求"""

    step_mode: str = "step_over"  # step_over, step_in, step_out


@router.post("/3d-graph")
async def generate_3d_graph(request: Generate3DGraphRequest) -> dict[str, Any]:
    """生成3D图谱"""
    try:
        # 从 AST 数据构建图谱
        if "symbols" in request.ast_data:
            for symbol in request.ast_data["symbols"]:
                three_d_graph.add_node(
                    node_id=symbol.get("id", ""),
                    label=symbol.get("name", ""),
                    type=symbol.get("type", ""),
                    attributes={"file": symbol.get("file", "")},
                )

        # 添加依赖边
        if "dependencies" in request.ast_data:
            for dep in request.ast_data["dependencies"]:
                three_d_graph.add_edge(
                    source=dep.source, target=dep.target, type=dep.type, weight=1.0
                )

        # 生成3D图谱
        result = three_d_graph.generate_3d_plot(request.output_format)

        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"3D graph generation failed: {e!s}")


@router.post("/debug/expression")
async def debug_expression(request: DebugExpressionRequest) -> dict[str, Any]:
    """在调试上下文中评估表达式"""
    try:
        result = interactive_debugger.evaluate_expression(request.expression, request.context)
        return {"status": "success", "expression": request.expression, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Expression evaluation failed: {e!s}")


@router.post("/debug/step")
async def step_debug(request: StepDebugRequest) -> dict[str, Any]:
    """执行调试步进"""
    try:
        interactive_debugger.set_step_mode(request.step_mode)
        result = interactive_debugger.step()

        return {"status": "success", "step_result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Step debug failed: {e!s}")


@router.post("/debug/edit-variable")
async def edit_variable(variable_name: str, new_value: Any) -> dict[str, Any]:
    """编辑变量值"""
    try:
        success = interactive_debugger.edit_variable(variable_name, new_value)
        return {
            "status": "success" if success else "error",
            "variable_name": variable_name,
            "new_value": new_value,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Edit variable failed: {e!s}")


@router.get("/debug/state")
async def get_debug_state() -> dict[str, Any]:
    """获取调试状态"""
    try:
        state = interactive_debugger.get_debug_state()
        return {"status": "success", "debug_state": state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get debug state: {e!s}")


@router.post("/deployment-topology")
async def generate_deployment_topology(services: list[dict[str, Any]]) -> dict[str, Any]:
    """生成部署拓扑图"""
    try:
        topology = architecture_visualizer.generate_deployment_topology(services)
        return {"status": "success", "topology": topology}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Deployment topology generation failed: {e!s}")


@router.post("/data-flow-diagram")
async def generate_data_flow_diagram(components: list[dict[str, Any]]) -> dict[str, Any]:
    """生成数据流图"""
    try:
        diagram = architecture_visualizer.generate_data_flow_diagram(components)
        return {"status": "success", "diagram": diagram}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Data flow diagram generation failed: {e!s}")
