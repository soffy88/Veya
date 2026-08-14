from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.loader import load_config
from server.assembly import Infra
from server.routes.advanced_visualization import router as advanced_visualization_router
from server.routes.adversarial import router as adversarial_router
from server.routes.agent import router as agent_router
from server.routes.agent_collaboration import router as agent_collaboration_router
from server.routes.analysis import router as analysis_router
from server.routes.audit import router as audit_router
from server.routes.auth import router as auth_router
from server.routes.auth import key_router as auth_key_router
from server.routes.automata import router as automata_router
from server.routes.automata import webhook_router
from server.routes.autonomous import router as autonomous_router
from server.routes.backends import router as backends_router
from server.routes.board import router as board_router
from server.routes.chat import router as chat_router
from server.routes.cindy_compat import router as cindy_compat_router
from server.routes.closed_loop import router as closed_loop_router
from server.routes.collaboration import router as collaboration_router
from server.routes.cross_language import router as cross_language_router
from server.routes.evolution import router as evolution_router
from server.routes.flow import router as flow_router
from server.routes.init import router as init_router
from server.routes.integrations import router as integrations_router
from server.routes.legacy_agent import router as legacy_agent_router
from server.routes.master import router as master_router
from server.routes.mcp import router as mcp_router
from server.routes.models import router as models_router
from server.routes.multimodal import router as multimodal_router
from server.routes.neuro_symbolic import router as neuro_symbolic_router
from server.routes.notifications import router as notifications_router
from server.routes.fs import router as fs_router
from server.routes.git import router as git_router
from server.routes.graph import router as graph_router
from server.routes.plan import router as plan_router
from server.routes.observer import router as observer_router
from server.routes.omni import router as omni_router
from server.routes.operator import router as operator_router
from server.routes.performance import router as performance_router
from server.routes.permission import router as permission_router
from server.routes.projects import router as projects_router
from server.routes.prompt import router as prompt_router
from server.routes.research import router as research_router
from server.routes.resilient import router as resilient_router
from server.routes.security import router as security_router
from server.routes.semantic_search import router as semantic_search_router
from server.routes.session import router as session_router
from server.routes.sessions import router as sessions_router
from server.routes.static_invariant import router as static_invariant_router
from server.routes.threat_model import router as threat_model_router
from server.routes.tool import router as tool_router
from server.routes.tools import router as tools_router
from server.routes.vault import compat_router as vault_compat_router
from server.routes.vault import router as vault_router
from server.routes.visualization import router as visualization_router
from server.routes.vscode import router as vscode_router
from server.sse import router as sse_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Infra.init(load_config())
    import logging

    _lg = logging.getLogger("veya.lifespan")
    # 启动 Automata 后台守护进程(Agent OS 的"手脚")
    from server.automata import get_automata

    automata = get_automata()
    # MCP 能力面分步装配: 每步独立 try (一处失败不拖累后续 wire),
    # 失败打日志便于排查 (此前整体 try 吞异常 → 容器重启后 mcp_* 全缺)。
    from server.codebase_memory import get_connector, schedule_daily_reindex, wire_master_tools

    try:
        connector = get_connector()
        await connector.start()
        await wire_master_tools()                       # mcp_codebase_* → 主脑工具面
        schedule_daily_reindex(automata.scheduler)      # 每日 03:17 增量索引
    except Exception:
        import logging
        logging.getLogger("veya.lifespan").exception("codebase-memory wire failed")
    # Stratum 知识库 MCP (同 docker 网络, 不可达时优雅降级)
    from server.stratum_memory import get_stratum
    from server.stratum_memory import wire_master_tools as wire_stratum

    try:
        stratum = get_stratum()
        await stratum.start()
        await wire_stratum(stratum)                     # mcp_stratum_* → 主脑工具面
    except Exception:
        import logging
        logging.getLogger("veya.lifespan").exception("stratum wire failed")
    # hevi 视频管线 MCP (同 docker 网络, 不可达/密钥缺失时优雅降级)
    from server.hevi_memory import get_hevi
    from server.hevi_memory import wire_master_tools as wire_hevi

    try:
        hevi = get_hevi()
        await hevi.start()
        await wire_hevi(hevi)                           # mcp_hevi_* → 主脑工具面
    except Exception:
        import logging
        logging.getLogger("veya.lifespan").exception("hevi wire failed")
    # Open Design MCP (设计/渲染层; daemon 不可达/无 token 时优雅降级)
    from server.open_design import get_open_design
    from server.open_design import wire_master_tools as wire_od

    try:
        od = get_open_design()
        await od.start()
        await wire_od(od)                               # mcp_od_* → 主脑工具面
    except Exception:
        import logging
        logging.getLogger("veya.lifespan").exception("open-design wire failed")
    # Agentic HPO 参数优化工具 (3O _hp_search 装配层, 零外部依赖)
    from server.hp_optimizer import wire_master_tools as wire_hp

    try:
        wire_hp()                                   # optimize_parameters → 主脑工具面 (同步)
    except Exception:
        import logging
        logging.getLogger("veya.lifespan").exception("hp wire failed")
    # Hicode 编码执行器 (本地二进制, 缺失时优雅降级; 编程任务 → hicode_run)
    from server.hicode_agent import wire_master_tools as wire_hicode

    try:
        await wire_hicode()                       # hicode_run/status → 主脑工具面
    except Exception:
        import logging
        logging.getLogger("veya.lifespan").exception("hicode wire failed")
    try:
        from server.plan_todo import wire_master_tools as wire_plan
        from server.long_read import wire_master_tools as wire_long

        wire_plan()          # create_plan / plan_status / update_todo → 主脑工具面
        wire_long()          # long_read (长文分块导航) → 主脑工具面
        # loop-plane 3 个新工具 (loop_plan_goal / loop_diagnose / loop_intervene)
        from server.loop_plane_client import wire_loop_tools

        wire_loop_tools()
    except Exception:
        import logging
        logging.getLogger("veya.lifespan").exception("cognition wire failed")
    try:
        from server.tool_registry import master_tools

        _mcp = [n for n in master_tools._functions if n.startswith("mcp_")]
        _lg.warning(
            "wire 汇总: master_tools=%d mcp_*= %d %s",
            len(master_tools._functions), len(_mcp), sorted(_mcp)[:6],
        )
    except Exception:
        _lg.exception("wire 汇总日志失败")
    yield
    try:
        from server.codebase_memory import get_connector

        await get_connector().close()
    except Exception:
        pass
    automata.shutdown()


app = FastAPI(title="veya", version="0.6.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3006",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://172.23.229.195:3000",
        # Tauri 桌面版 WebView origin (Linux/macOS/Windows)
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
    ],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(prompt_router)
app.include_router(chat_router)
app.include_router(master_router)
app.include_router(automata_router)
app.include_router(webhook_router)
app.include_router(flow_router)
app.include_router(notifications_router)
app.include_router(fs_router)
app.include_router(graph_router)
app.include_router(git_router)
app.include_router(plan_router)
app.include_router(omni_router)
app.include_router(permission_router)
app.include_router(session_router)
app.include_router(tool_router)
app.include_router(agent_router)
app.include_router(models_router)
app.include_router(security_router)
app.include_router(vscode_router)
# 阶段 5: 3O 统一网关（极简指令 + SSE；新增前缀, 不替换现有路由）
from veya.oservi.gateway import router as gateway_router

app.include_router(gateway_router)
app.include_router(vault_router)
app.include_router(vault_compat_router)
app.include_router(analysis_router)
app.include_router(tools_router)
app.include_router(multimodal_router)
app.include_router(integrations_router)
app.include_router(collaboration_router)
app.include_router(semantic_search_router)
app.include_router(autonomous_router)
app.include_router(visualization_router)
app.include_router(cross_language_router)
app.include_router(performance_router)
app.include_router(advanced_visualization_router)
app.include_router(agent_collaboration_router)
app.include_router(mcp_router)
app.include_router(auth_router)
app.include_router(auth_key_router)
app.include_router(init_router)
app.include_router(research_router)
app.include_router(resilient_router)
app.include_router(sessions_router)
app.include_router(projects_router)
app.include_router(static_invariant_router)
app.include_router(adversarial_router)
app.include_router(evolution_router)
app.include_router(neuro_symbolic_router)
app.include_router(operator_router)
app.include_router(observer_router)
app.include_router(closed_loop_router)
app.include_router(threat_model_router)
app.include_router(audit_router)
app.include_router(backends_router)
app.include_router(board_router)
app.include_router(legacy_agent_router)
app.include_router(cindy_compat_router)
app.include_router(sse_router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.5.1"}
