from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from config.loader import load_config
from server.assembly import Infra
from server.routes.prompt import router as prompt_router
from server.routes.session import router as session_router
from server.routes.tool import router as tool_router
from server.routes.agent import router as agent_router
from server.routes.models import router as models_router
from server.routes.mcp import router as mcp_router
from server.routes.auth import router as auth_router
from server.routes.init import router as init_router
from server.routes.research import router as research_router
from server.routes.sessions import router as sessions_router
from server.routes.projects import router as projects_router
from fastapi.middleware.cors import CORSMiddleware
from server.sse import router as sse_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Infra.init(load_config())
    yield


app = FastAPI(title="hicode", version="0.2.0", lifespan=lifespan)

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
app.include_router(session_router)
app.include_router(tool_router)
app.include_router(agent_router)
app.include_router(models_router)
app.include_router(mcp_router)
app.include_router(auth_router)
app.include_router(init_router)
app.include_router(research_router)
app.include_router(sessions_router)
app.include_router(projects_router)
app.include_router(sse_router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.2.0"}
