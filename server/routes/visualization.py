"""
代码可视化 API - P3 核心能力
提供代码图谱、架构图、依赖可视化等功能
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from veya.visualization import create_code_graph

router = APIRouter(prefix="/visualization", tags=["visualization"])

# 全局代码图谱（实际应用中应使用缓存）
code_graph = create_code_graph()


class GenerateGraphRequest(BaseModel):
    """生成图谱请求"""

    ast_data: dict[str, Any]
    output_format: str = "cytoscape"  # cytoscape, json, image


@router.post("/generate")
async def generate_code_graph(request: GenerateGraphRequest) -> dict[str, Any]:
    """生成代码图谱"""
    try:
        # 简化：直接从 AST 数据构建图谱
        # 实际应用中应使用 AST 分析器

        # 构建节点
        if "symbols" in request.ast_data:
            for symbol in request.ast_data["symbols"]:
                from veya.visualization import GraphNode

                node = GraphNode(
                    node_id=symbol.get("id", ""),
                    label=symbol.get("name", ""),
                    type=symbol.get("type", ""),
                    attributes={"file": symbol.get("file", "")},
                )
                code_graph.add_node(node)

        # 计算指标
        metrics = code_graph.calculate_metrics()

        # 根据格式返回
        if request.output_format == "cytoscape":
            output = code_graph.export_to_cytoscape()
        elif request.output_format == "json":
            output = code_graph.export_to_json()
        elif request.output_format == "image":
            image_data = code_graph.generate_image()
            output = {"image": image_data} if image_data else {"error": "Failed to generate image"}
        else:
            output = {"error": f"Unsupported format: {request.output_format}"}

        return {"status": "success", "metrics": metrics, "output": output}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Graph generation failed: {e!s}")


@router.get("/metrics")
async def get_graph_metrics() -> dict[str, Any]:
    """获取图谱指标"""
    try:
        metrics = code_graph.calculate_metrics()
        return {"status": "success", "metrics": metrics}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get metrics: {e!s}")


@router.post("/architecture")
async def generate_architecture_diagram(components: list[dict[str, Any]]) -> dict[str, Any]:
    """生成架构图"""
    try:
        from veya.visualization import create_architecture_visualizer

        visualizer = create_architecture_visualizer()
        diagram = visualizer.generate_architecture_diagram(components)

        return {"status": "success", "diagram": diagram}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Architecture generation failed: {e!s}")


@router.post("/debugger/breakpoint")
async def add_breakpoint(file_path: str, line: int, condition: str | None = None) -> dict[str, Any]:
    """添加调试断点"""
    try:
        from veya.visualization import create_interactive_debugger

        debugger = create_interactive_debugger()
        breakpoint_id = debugger.add_breakpoint(file_path, line, condition)

        return {
            "status": "success",
            "breakpoint_id": breakpoint_id,
            "message": f"Breakpoint added at {file_path}:{line}",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add breakpoint: {e!s}")


@router.get("/debugger/state")
async def get_debugger_state() -> dict[str, Any]:
    """获取调试器状态"""
    try:
        from veya.visualization import create_interactive_debugger

        debugger = create_interactive_debugger()
        state = debugger.get_debug_state()

        return {"status": "success", "debug_state": state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get debug state: {e!s}")
