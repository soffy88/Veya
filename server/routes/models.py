"""
Model Management API - Enhanced model registry and loading
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/models", tags=["models"])


class ModelConfig(BaseModel):
    name: str
    description: str
    author: str
    default_version: str


class ModelVersion(BaseModel):
    version: str
    path: str
    config: ModelConfig


class ModelInfo(BaseModel):
    name: str
    description: str
    versions: list[str]
    default_version: str
    path: str


class LoadModelRequest(BaseModel):
    name: str
    version: str | None = "latest"


class LoadModelResponse(BaseModel):
    name: str
    version: str
    config: dict[str, Any]
    path: str


@router.get("", response_model=list[ModelInfo])
async def list_models():
    """List all available models"""
    try:
        from hicode.models import list_models as get_models
        from hicode.models.utils import MODELS_ROOT

        models = []
        for name in get_models():
            model_path = MODELS_ROOT / name
            config_path = model_path / "config.json"

            if config_path.exists():
                import json

                with open(config_path) as f:
                    config = json.load(f)

                # Get versions
                versions = []
                for p in model_path.iterdir():
                    if p.is_dir() and (p / "config.json").exists():
                        versions.append(p.name)

                models.append(
                    ModelInfo(
                        name=name,
                        description=config.get("description", ""),
                        versions=sorted(versions),
                        default_version=config.get(
                            "default_version", versions[0] if versions else ""
                        ),
                        path=str(model_path),
                    )
                )

        return models
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list models: {e!s}")


@router.get("/{model_name}/versions", response_model=list[str])
async def list_versions(model_name: str):
    """List all versions of a specific model"""
    try:
        from hicode.models import list_versions

        versions = list_versions(model_name)
        if not versions:
            raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")
        return sorted(versions)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list versions: {e!s}")


@router.post("/load", response_model=LoadModelResponse)
async def load_model(req: LoadModelRequest):
    """Load a specific model version"""
    try:
        from hicode.models import load_model

        result = load_model(req.name, req.version)
        return LoadModelResponse(
            name=result["name"],
            version=result["version"],
            config=result["config"],
            path=result["path"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load model: {e!s}")


@router.get("/registry")
async def get_registry():
    """Get full model registry from registries"""
    try:
        from registries.models import MODEL_REGISTRY

        return {"models": MODEL_REGISTRY}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get registry: {e!s}")


@router.post("/register")
async def register_model(name: str, path: str, versions: list[str]):
    """Register a new model manually"""
    try:
        import json
        from pathlib import Path

        from registries.models import load_models

        model_path = Path(path)
        if not model_path.exists():
            raise HTTPException(status_code=400, detail="Path does not exist")

        config_file = model_path / "config.json"
        if not config_file.exists():
            # Create default config
            config = {
                "name": name,
                "description": f"Registered model: {name}",
                "author": "hicode",
                "default_version": versions[0] if versions else "",
            }
            with open(config_file, "w") as f:
                json.dump(config, f, indent=2)

        # Reload registry
        load_models()
        return {"status": "success", "name": name, "versions": versions}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to register model: {e!s}")
