from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.loader import load_config
from server.assembly import Infra
from server.routes.advanced_visualization import router as advanced_visualization_router
from server.routes.agent import router as agent_router
from server.routes.agent_collaboration import router as agent_collaboration_router
from server.routes.adversarial import router as adversarial_router
from server.routes.analysis import router as analysis_router
from server.routes.auth import router as auth_router
from server.routes.automata import router as automata_router
from server.routes.automata import webhook_router
from server.routes.autonomous import router as autonomous_router
from server.routes.chat import router as chat_router
from server.routes.collaboration import router as collaboration_router
from server.routes.cross_language import router as cross_language_router
from server.routes.flow import router as flow_router
from server.routes.init import router as init_router
from server.routes.integrations import router as integrations_router
from server.routes.master import router as master_router
from server.routes.mcp import router as mcp_router
from server.routes.models import router as models_router
from server.routes.multimodal import router as multimodal_router
from server.routes.notifications import router as notifications_router
from server.routes.omni import router as omni_router
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
from server.routes.tool import router as tool_router
from server.routes.tools import router as tools_router
from server.routes.vault import compat_router as vault_compat_router
from server.routes.vault import router as vault_router
from server.routes.visualization import router as visualization_router
from server.routes.vscode import router as vscode_router
from server.routes.static_invariant import router as static_invariant_router
from server.routes.evolution import router as evolution_router
from server.routes.neuro_symbolic import router as neuro_symbolic_router
from server.routes.operator import router as operator_router
from server.routes.observer import router as observer_router
from server.routes.closed_loop import router as closed_loop_router
from server.routes.threat_model import router as threat_model_router
from server.routes.audit import router as audit_router
from server.routes.legacy_agent import router as legacy_agent_router
from server.sse import router as sse_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Infra.init(load_config())
    # 启动 Automata 后台守护进程(Agent OS 的"手脚")
    from server.automata import get_automata

    automata = get_automata()
    yield
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
    ],
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
app.include_router(omni_router)
app.include_router(permission_router)
app.include_router(session_router)
app.include_router(tool_router)
app.include_router(agent_router)
app.include_router(models_router)
app.include_router(security_router)
app.include_router(vscode_router)
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
app.include_router(legacy_agent_router)
app.include_router(sse_router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.5.1"}
