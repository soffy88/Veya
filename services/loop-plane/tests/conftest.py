"""loop-plane 测试共基（conftest）。

- 临时 LOOP_DATA_DIR（每测试隔离）
- TestClient 用 httpx ASGITransport（不触发 lifespan，手动 configure）
- 进程内复用 server.app 单例（spec: Caller → loop-plane → veya-loop → 3O）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 仓库根 + veya_loop src 入 path（同仓库内运行，不依赖 pip install）
REPO_ROOT = Path(__file__).resolve().parents[3]
VEYA_LOOP_SRC = REPO_ROOT / "veya_loop" / "src"
for p in (REPO_ROOT, VEYA_LOOP_SRC):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
# loop-plane 包目录（app.* 顶层导入）
LP_ROOT = Path(__file__).resolve().parents[1]
if str(LP_ROOT) not in sys.path:
    sys.path.insert(0, str(LP_ROOT))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "loop"
    d.mkdir()
    return d


@pytest.fixture
def store(data_dir: Path):
    from app.infra.event_store import EventStore

    return EventStore(data_dir, tenant_id="default")


@pytest.fixture
def audit(data_dir: Path):
    from app.infra.event_store import AuditLog

    return AuditLog(data_dir, tenant_id="default")


@pytest.fixture
def goal_service(store):
    from app.domain.state.service import GoalService

    return GoalService(store)


@pytest.fixture
def exec_service(tmp_path: Path):
    from app.domain.exec.service import ExecService

    return ExecService(workspace=tmp_path)


@pytest.fixture
async def client(data_dir: Path, tmp_path: Path, store, audit):
    """ASGI 客户端（手动 configure，复用 store/audit 实例共享索引）。"""
    import httpx

    from app.config import Settings
    from app.deps import configure
    from app.domain.causal.service import CausalService
    from app.domain.exec.service import ExecService
    from app.domain.sched.service import SchedService
    from app.domain.state.service import GoalService
    from app.main import build_app

    settings = Settings(data_dir=data_dir, workspace=tmp_path)
    configure(settings, store=store, audit=audit)
    app = build_app(settings)
    app.state.goal_service = GoalService(store)
    app.state.plan_service = CausalService(store=store, audit=audit)
    app.state.exec_service = ExecService(workspace=tmp_path)
    app.state.sched_service = SchedService(app.state.goal_service, jobs_path=data_dir / "jobs.json")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def configure_store():
    from app.deps import get_store

    return get_store()


def configure_audit():
    from app.deps import get_audit

    return get_audit()