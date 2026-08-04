from pathlib import Path

MODELS_ROOT = Path(__file__).parent.parent / "models"  # ← now inside hicode/ package


def _safe_resolve(path: Path) -> Path:
    """Resolve path and ensure it's under MODELS_ROOT"""
    resolved = path.resolve()
    try:
        resolved.relative_to(MODELS_ROOT)
        return resolved
    except ValueError:
        raise ValueError(f"Path traversal attempt: {path} is outside {MODELS_ROOT}")


def get_model_path(name: str, version: str) -> Path:
    """Get absolute path to model version dir (e.g., models/test-llm/v0.1)"""
    model_dir = MODELS_ROOT / name / version
    return _safe_resolve(model_dir)


def list_models() -> list[str]:
    """List all model names (subdirs of MODELS_ROOT with config.json)"""
    models = []
    for p in MODELS_ROOT.iterdir():
        if p.is_dir() and (p / "config.json").exists():
            models.append(p.name)
    return sorted(models)


def list_versions(name: str) -> list[str]:
    """List all versions (subdirs of MODELS_ROOT/name with config.json)"""
    model_root = MODELS_ROOT / name
    if not model_root.exists():
        return []
    versions = []
    for p in model_root.iterdir():
        if p.is_dir() and (p / "config.json").exists():
            versions.append(p.name)
    return sorted(versions)
