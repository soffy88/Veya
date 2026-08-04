from veya.models import list_models
from veya.models.utils import MODELS_ROOT, list_versions

# Global registry — loaded on import
MODEL_REGISTRY = {}


def load_models():
    """Scan and register all models from veya/models/"""
    global MODEL_REGISTRY
    MODEL_REGISTRY.clear()
    for name in list_models():
        # Load model config (top-level)
        config_path = MODELS_ROOT / name / "config.json"
        try:
            import json

            with open(config_path) as f:
                config = json.load(f)
            MODEL_REGISTRY[name] = {
                "config": config,
                "versions": list_versions(name),
            }
        except Exception as e:
            print(f"[WARN] Failed to load model '{name}': {e}")
    return MODEL_REGISTRY


# Auto-load on module import
load_models()

__all__ = ["MODEL_REGISTRY", "load_models"]
