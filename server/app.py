from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.loader import load_config
from server.assembly import Infra
from server.routes.advanced_visualization import router as advanced_visualization_router
from server.routes.agent import router as agent_router
from server.routes.agent_collaboration import router as agent_collaboration_router
from server.routes.analysis import router as analysis_router
from server.routes.auth import router as auth_router
from server.routes.autonomous import router as autonomous_router
from server.routes.collaboration import router as collaboration_router
from server.routes.cross_language import router as cross_language_router
from server.routes.init import router as init_router
from server.routes.integrations import router as integrations_router
from server.routes.mcp import router as mcp_router
from server.routes.models import router as models_router
from server.routes.multimodal import router as multimodal_router
from server.routes.performance import router as performance_router
from server.routes.permission import router as permission_router
from server.routes.projects import router as projects_router
from server.routes.prompt import router as prompt_router
from server.routes.research import router as research_router
from server.routes.security import router as security_router
from server.routes.semantic_search import router as semantic_search_router
from server.routes.session import router as session_router
from server.routes.sessions import router as sessions_router
from server.routes.tool import router as tool_router
from server.routes.tools import router as tools_router
from server.routes.visualization import router as visualization_router
from server.routes.vscode import router as vscode_router
from server.sse import router as sse_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Infra.init(load_config())
    yield


app = FastAPI(title="veya", version="0.5.1", lifespan=lifespan)

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
app.include_router(permission_router)
app.include_router(session_router)
app.include_router(tool_router)
app.include_router(agent_router)
app.include_router(models_router)
app.include_router(security_router)
app.include_router(vscode_router)
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
app.include_router(sessions_router)
app.include_router(projects_router)
app.include_router(sse_router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.5.1"}
