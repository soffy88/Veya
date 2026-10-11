"""server/routes/bot_models.py — the model catalog the bot UI picks from.

W11 (bot landing): the Team page's "new bot" sheet has a model picker, and a
picker needs a list. ``/api/v1/providers`` already projects the provider
registry, but it sits behind ``VEYA_PROVIDER_REGISTRY`` (off by default), so on
a default deployment the picker would have nothing to choose from.

This route re-projects what already exists — the provider registry's builtin
and custom specs, plus the LLM layer's provider→default-model table. It
registers nothing, stores nothing and resolves no key, so it is not the
provider-registry feature and does not need its flag. ``GET /models/catalog``
already answers the same question unauthenticated; this is the same shape under
``/api/v1`` so the frontend's one proxy path reaches it.

Read-only, so there is nothing to own: the one gate that matters is that
creating a bot is a logged-in action (W11 D2), which is why this is
``require_user`` rather than public.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from server import auth as auth_mod

router = APIRouter(prefix="/api/v1/models", tags=["models"])


def _catalog() -> dict[str, Any]:
    """The selectable models, projected from the two tables that already exist."""

    # The provider registry: builtin specs (name/models/default_model) plus any
    # custom provider the user registered there.
    specs: dict[str, Any] = {}
    try:
        from veya.obase.provider_registry import get_veya_provider_registry

        specs = get_veya_provider_registry().get_all_providers()
    except Exception:  # pragma: no cover — the registry is import-safe by design
        specs = {}

    # The LLM layer's own table. Not every provider a turn can use is in the
    # registry (veya1.2, opencode-go, inferera), and leaving them out would hide
    # the server's actual default from the picker.
    defaults: dict[str, str] = {}
    default_provider = ""
    try:
        from veya.obase._llm_config import _DEFAULT_MODELS, _DEFAULT_PROVIDER

        defaults = dict(_DEFAULT_MODELS)
        default_provider = str(_DEFAULT_PROVIDER or "")
    except Exception:  # pragma: no cover
        defaults = {}

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(model_id: str, provider: str, *, is_default: bool) -> None:
        model_id = str(model_id or "").strip()
        provider = str(provider or "").strip()
        if not model_id or not provider or (provider, model_id) in seen:
            return
        seen.add((provider, model_id))
        spec = specs.get(provider)
        rows.append(
            {
                "id": model_id,
                "provider": provider,
                "provider_label": (spec.display_name if spec is not None else provider),
                "default": bool(is_default),
            }
        )

    # Providers in the LLM table first: their default model is always selectable
    # even when the registry has no entry for that provider.
    for provider, model_id in sorted(defaults.items()):
        add(model_id, provider, is_default=provider == default_provider)

    for provider, spec in sorted(specs.items()):
        spec_default = str(getattr(spec, "default_model", "") or "")
        registry_models = list(getattr(spec, "models", []) or [])
        for model_id in [*registry_models, spec_default]:
            add(model_id, provider, is_default=bool(spec_default) and model_id == spec_default)

    default_model = defaults.get(default_provider) or None
    return {
        "default_model": default_model,
        "default_provider": default_provider or None,
        "models": rows,
    }


@router.get("")
async def list_models(
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Models a new bot may be pointed at (read-only catalog for the picker)."""

    del user  # authentication only: a catalog is not per-user data
    return _catalog()
