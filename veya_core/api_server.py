"""Veya Core: 3O 引擎常驻 FastAPI 服务端."""

# 导入底层 3O 包装脚本
from dag_workflow_scheduler import run_dag_workflow
from fastapi import FastAPI, HTTPException
from pipeline_snapshot_rollback import run_pipeline_with_rollback
from pydantic import BaseModel
from single_agent_pandas import run_complex_pandas_task

app = FastAPI(
    title="Veya 3O Core Engine",
    description="提供确定性防御、因果诊断、形式化验证及隔离沙箱调度的重工业级后端计算集群。",
    version="3.0.0",
)


class SandboxRequest(BaseModel):
    data_path: str | None = "dummy_data.csv"


class PipelineRequest(BaseModel):
    workspace_dir: str | None = "/tmp/veya_workspace"


@app.get("/health")
def health_check() -> dict[str, str]:
    """健康检查，供上层 Veya 主程序或容器编排探测."""
    return {"status": "healthy", "engine": "3O-Core"}


@app.post("/v1/dag/schedule")
def api_dag_schedule():
    """触发 O1/O2 DAG 拓扑与匈牙利最优资源分配."""
    try:
        return run_dag_workflow()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/sandbox/pandas")
def api_sandbox_pandas(payload: SandboxRequest):
    """将高风险 Pandas 逻辑隔离至 O3 沙箱执行."""
    try:
        return run_complex_pandas_task(payload.data_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/pipeline/rollback")
def api_pipeline_rollback(payload: PipelineRequest):
    """触发 3O 快照与故障瞬间回滚通道."""
    try:
        return run_pipeline_with_rollback(payload.workspace_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
