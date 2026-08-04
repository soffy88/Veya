from pathlib import Path

from .utils import get_model_path, list_models, list_versions

# Default model root directory (can be overridden by config)
MODELS_ROOT = Path(__file__).parent.parent / "models"


def load_model(name: str, version: str = "latest") -> dict:
    """Mock loader — returns model config + resolved path"""
    if version == "latest":
        versions = list_versions(name)
        if not versions:
            raise ValueError(f"No versions found for model '{name}'")
        version = sorted(versions, key=lambda v: v.replace("v", ""))[-1]

    model_dir = get_model_path(name, version)
    if not model_dir.exists():
        raise FileNotFoundError(f"Model {name}@{version} not found at {model_dir}")

    config_path = model_dir / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Missing config.json in {model_dir}")

    import json

    with open(config_path) as f:
        config = json.load(f)

    return {
        "name": name,
        "version": version,
        "config": config,
        "path": str(model_dir),
    }


__all__ = ["list_models", "list_versions", "load_model"]
