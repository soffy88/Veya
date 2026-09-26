"""SPEC v1.0 §20 — canonical model proxy contract tests.

Covers the four canonical proxies (veya1.2 / veya-free / veya-nim / veya-vl),
the single routing authority, the §5 eligibility conjunction, §8 NIM key
rotation with model/key state separation, §11 legacy alias visibility, and
§13 executor isolation.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC
from pathlib import Path

from veya.obase import canonical_proxies as cp

HOME = Path.home()
ROUTER = HOME / ".veya" / "llm-router.json"
CONFIG = HOME / ".veya" / "config.json"
REGISTRY = HOME / ".veya" / "provider-registry.json"
STATE = HOME / ".veya" / "model-state.json"
NIM_POOL = HOME / ".veya" / "secrets" / "nim-key-pool"

REPO = Path(__file__).resolve().parents[1]


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# §3  ROUTING_AUTHORITY_COUNT=1
# ---------------------------------------------------------------------------


def test_router_single_authority():
    """Business routing lives in exactly one file, and it names only proxies."""
    assert ROUTER.is_file(), "~/.veya/llm-router.json missing"
    router = _json(ROUTER)
    assert router["default_proxy"] == "veya1.2"

    values = set(router["task_class_proxy"].values())
    assert values <= set(cp.CANONICAL_PROXIES), (
        f"task_class_proxy names non-canonical entries: {values - set(cp.CANONICAL_PROXIES)}"
    )

    # every value in the oprim-consumed route table is a canonical proxy too
    for name, target in router["routes"].items():
        assert target["provider"] in cp.CANONICAL_PROXIES, (
            f"routes.{name}.provider={target['provider']!r} is not a canonical proxy"
        )

    # §4.2 the routing authority carries no credentials and no health state
    blob = json.dumps(router).lower()
    for forbidden in ("api_key", "api-key", "nvapi-", "cooldown_until", "healthy"):
        assert forbidden not in blob, f"routing authority must not contain {forbidden!r}"
    # real credential shapes only — prose may legitimately contain "sk-"
    for pattern in (r"sk-[A-Za-z0-9]{16,}", r"nvapi-[A-Za-z0-9]{16,}"):
        assert not re.search(pattern, blob), (
            f"routing authority contains a credential matching {pattern}"
        )

    # provider contracts and model health are separate files
    assert REGISTRY.is_file(), "provider-registry.json missing"
    assert STATE.is_file(), "model-state.json missing"


def test_veya12_default_proxy():
    """DEFAULT_PROXY and the user config both point at the master brain."""
    assert cp.DEFAULT_PROXY == "veya1.2"
    assert cp.DEFAULT_PROXY in cp.CANONICAL_PROXIES

    config = _json(CONFIG)
    assert config["default_proxy"] == "veya1.2"
    # §4.1 a raw provider/model default that bypasses the router is forbidden
    assert "llm" not in config, (
        "config.json must not carry a raw llm.provider/llm.model default; "
        "that bypasses the canonical router"
    )
    assert "router_profile" in config

    # a bare call with no provider/model resolves to the master brain
    from veya.obase.llm import get_provider_config

    provider, model = get_provider_config({})
    assert (provider, model) == ("veya1.2", "veya1.2")


# ---------------------------------------------------------------------------
# §5  eligibility conjunction
# ---------------------------------------------------------------------------

_ELIGIBILITY_FLAGS = (
    "discovered",
    "healthy",
    "credentials_valid",
    "endpoint_available",
    "model_available",
)


def _models() -> dict:
    return _json(STATE).get("models") or {}


def test_free_pool_eligibility():
    """veya-free candidates all satisfy the full §5 conjunction."""
    pool = cp.free_pool_candidates()
    assert pool, "veya-free pool is empty"
    for candidate in pool:
        assert candidate["provider"] and candidate["model"]
    eligible = cp.eligible_free_models("text")
    for entry in eligible:
        for flag in _ELIGIBILITY_FLAGS:
            assert entry.get(flag) is True, f"{entry['model_id']} missing {flag}"
        assert entry["canonical_proxy"] == "veya-free"


def test_no_active_unhealthy():
    """No model may be both eligible and unhealthy (§5)."""
    offenders = [
        key
        for key, entry in _models().items()
        if entry.get("eligible") and not entry.get("healthy")
    ]
    assert not offenders, f"ACTIVE_UNHEALTHY: {offenders}"


def test_no_active_cooldown():
    """No model may be both eligible and inside its cooldown window (§5)."""
    from datetime import datetime

    now = datetime.now(UTC)
    offenders = []
    for key, entry in _models().items():
        if not entry.get("eligible"):
            continue
        until = entry.get("cooldown_until")
        if not until:
            continue
        try:
            when = datetime.strptime(str(until), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            continue
        if when > now:
            offenders.append(f"{key} (cooldown_until={until})")
    assert not offenders, f"ACTIVE_COOLDOWN: {offenders}"


# ---------------------------------------------------------------------------
# §8  NIM key pool
# ---------------------------------------------------------------------------


def test_nim_three_key_rotation(monkeypatch, tmp_path):
    pool_file = tmp_path / "nim-key-pool"
    pool_file.write_text("k1\nk2\nk3\n", encoding="utf-8")
    monkeypatch.setattr(cp, "_NIM_KEY_POOL", pool_file)
    cp.reset_nim_key_pool()
    try:
        pool = cp.nim_key_pool()
        assert pool.size() == 3
        seen = [key for _ in range(6) for _, key in pool.next_keys(1)]
        assert seen == ["k1", "k2", "k3", "k1", "k2", "k3"], seen
    finally:
        cp.reset_nim_key_pool()


def test_nim_key_cooldown(monkeypatch, tmp_path):
    """§8.2: 401/403 disables a key, 429 cools it down, success resets it."""
    pool_file = tmp_path / "nim-key-pool"
    pool_file.write_text("k1\nk2\nk3\n", encoding="utf-8")
    monkeypatch.setattr(cp, "_NIM_KEY_POOL", pool_file)
    cp.reset_nim_key_pool()
    try:
        pool = cp.nim_key_pool()
        assert pool.mark_failure(1, 401) == "key-unhealthy"
        assert pool.states[1].healthy is False
        assert 1 not in [k for k, _ in pool.next_keys(3)]

        assert pool.mark_failure(2, 429) == "key-cooldown"
        assert pool.states[2].cooldown_until > 0
        assert 2 not in [k for k, _ in pool.next_keys(3)]

        # success clears the failure counter (§8.2 "success -> reset")
        pool.states[3].consecutive_failures = 2
        pool.mark_success(3)
        assert pool.states[3].consecutive_failures == 0
    finally:
        cp.reset_nim_key_pool()


def test_nim_model_health_independent_from_key(monkeypatch, tmp_path):
    """§8.3: a model failure must not disable keys, and vice versa.

    ``nvidia-nim:moonshotai/kimi-k2.6`` failed every request with all three
    keys healthy (404 = no entitlement, not an auth problem). Its model entry is
    ineligible while every key is still healthy.
    """
    models = _models()
    kimi = models.get("nvidia-nim:moonshotai/kimi-k2.6")
    assert kimi is not None, "kimi-k2.6 evidence missing from model-state.json"
    assert kimi["eligible"] is False
    assert kimi["credentials_valid"] is True, "auth passed; the failure was model-level"

    nim_keys = _json(STATE).get("nim_keys") or {}
    assert nim_keys, "nim key state missing"
    assert all(state["healthy"] for state in nim_keys.values()), (
        "a model-level failure must not mark keys unhealthy"
    )

    # and the reverse: an unhealthy key does not appear in the eligible model set
    pool_file = tmp_path / "nim-key-pool"
    pool_file.write_text("k1\n", encoding="utf-8")
    monkeypatch.setattr(cp, "_NIM_KEY_POOL", pool_file)
    cp.reset_nim_key_pool()
    try:
        pool = cp.nim_key_pool()
        pool.mark_failure(1, 401)
        assert pool.states[1].healthy is False
        assert cp.eligible_nim_models(), "model eligibility is independent of key health"
    finally:
        cp.reset_nim_key_pool()


# ---------------------------------------------------------------------------
# §9  veya-vl capability filter
# ---------------------------------------------------------------------------


def test_vl_capability_filter():
    """Vision models enter veya-vl only after a real capability probe (§9).

    Also pins the honest current state: exactly one vision model passed the
    probe, so the pool has no failover. SPEC §17 requires UNSUPPORTED rather
    than a FALSE PASS in that situation, which is what is asserted here.
    """
    models = _models()
    vl_entries = [e for e in models.values() if e.get("canonical_proxy") == "veya-vl"]
    assert vl_entries, "no model is tagged veya-vl"

    pool = cp.eligible_vl_models()
    for entry in pool:
        assert entry.get("vision_verified") is True, (
            f"{entry['model_id']} is in the VL pool without a verified vision probe"
        )
        assert entry.get("eligible") is True
    assert pool, "veya-vl pool is empty"

    status = _json(STATE)["veya_vl_status"]
    assert status["vision_capable_models"] == len(pool) == 2
    assert status["pool_formed"] is True
    assert status["spec_target_for_pool"] == 2
    # §17 MODEL_FAILOVER: with 2 probe-verified models the pool has real failover,
    # and reliability is recorded rather than assumed.
    assert "AVAILABLE" in status["failover"]
    verdicts = {v["verdict"] for v in status["reliability"].values()}
    assert "STABLE" in verdicts, f"no stable VL member measured: {verdicts}"
    for key, rel in status["reliability"].items():
        assert rel["ocr_runs"] >= 3, f"{key} reliability measured on too few runs"
        assert 0 <= rel["ocr_pass"] <= rel["ocr_runs"]

    # every ineligible vision candidate carries a probe-derived reason
    ineligible = [e for e in vl_entries if not e.get("eligible")]
    assert ineligible, "ineligible VL candidates should be recorded, not deleted"
    for entry in ineligible:
        assert entry.get("ineligible_reason"), f"{entry['model_id']} has no recorded reason"
        assert entry.get("vision_verified") is not True

    # vision models never leak into the text pool
    text_pool = {e["model_id"] for e in cp.eligible_free_models("text")}
    assert not (text_pool & {e["model_id"] for e in vl_entries})


# ---------------------------------------------------------------------------
# §11  legacy alias mapping
# ---------------------------------------------------------------------------


def test_legacy_alias_mapping():
    """Every legacy alias resolves, and each target is a canonical proxy."""
    assert cp.LEGACY_ALIAS_MAP, "legacy alias table is empty"
    for alias, canonical in cp.LEGACY_ALIAS_MAP.items():
        assert canonical in cp.CANONICAL_PROXIES, f"{alias} -> {canonical} is not canonical"
        resolved = cp.resolve_canonical(alias)
        assert resolved is not None, f"{alias} does not resolve"
        assert resolved.canonical == canonical
        assert resolved.deprecated is True, f"{alias} must be flagged deprecated"
        # deprecation must be visible, never silent
        dep = cp.deprecation_for(resolved)
        assert dep["visible"] is True
        assert dep["silent_substitution"] is False

    # the ordering trap: a config-injected provider must not swallow the model
    assert cp.resolve_canonical("veya1.2-free", "veya1.2").canonical == "veya-free"
    assert cp.resolve_canonical("veya-nim", "veya1.2").canonical == "veya-nim"

    # retired NIM aliases point at the proxy, never at a dead model id
    for alias in (
        "veya-m3-nv",
        "veya-deepseek-v4-flash-nv",
        "veya-qwen3.5-397b-nv",
        "veya-kimi-k2.6-nv",
        "veya-glm5.1-nv",
    ):
        assert cp.resolve_canonical(alias).canonical == "veya-nim"
        assert alias in cp.RETIRED_UPSTREAM

    # a raw upstream id must not resolve to a proxy
    assert cp.resolve_canonical("nex-agi/nex-n2.5-pro:free", "openrouter") is None


# ---------------------------------------------------------------------------
# §10  RAW_UPSTREAM_MODEL_IN_BUSINESS_CODE
# ---------------------------------------------------------------------------

# Raw upstream model ids are permitted only in the model runtime layer: the
# provider registry, the model catalog, the runtime model state, the
# qualification tooling and the tests that assert those invariants (SPEC §10).
_ALLOWED_PREFIXES = (
    "veya/obase/",
    "scripts/",
    "platform/",
    "tests/",
)

_RAW_MODEL_RE = re.compile(
    r"""["'](?:[a-z0-9.-]+/[A-Za-z0-9._:@-]+:(?:free|turbo|exp)"""
    r"""|deepseek-v[0-9][\w.-]*|glm-?5[\w.-]*|kimi-k[\d.]+"""
    r"""|qwen3[\w.-]*|nemotron-3[\w.-]*|minimax-m[\d.]+"""
    r"""|gpt-5-codex|claude-3-7-sonnet|grok-3|gemini-2\.5-pro"""
    r"""|qwen38-9b-q5|deepseek-r1)["']"""
)


def test_no_raw_model_business_dependency():
    """No raw upstream model id anywhere outside the model runtime layer (§10).

    Executor and optimizer defaults moved into
    ``~/.veya/provider-registry.json`` (``executor_contracts``), which §4.3
    designates as the provider execution-contract file, so business code reads
    them through :func:`canonical_proxies.executor_model` instead of hardcoding
    a literal.
    """
    scan_targets = [
        p
        for p in REPO.rglob("*.py")
        if not any(
            str(p.relative_to(REPO)).startswith(prefix) or "/." in str(p)
            for prefix in _ALLOWED_PREFIXES
        )
        and "worktrees" not in str(p)
        and "site-packages" not in str(p)
        and not str(p).startswith("venv")
    ]
    assert scan_targets, "no first-party python files discovered to scan"

    offenders: dict[str, list[str]] = {}
    for path in scan_targets:
        rel = str(path.relative_to(REPO))
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = sorted(set(_RAW_MODEL_RE.findall(text)))
        if hits:
            offenders[rel] = hits

    assert not offenders, (
        "raw upstream model dependencies in business code (SPEC §10): "
        f"{offenders}. Move the default into provider-registry.json "
        "executor_contracts and read it via canonical_proxies.executor_model()."
    )


def test_executor_contracts_live_in_the_registry():
    """Every executor default resolves through the registry, and env wins."""
    registry = _json(REGISTRY)
    contracts = registry["executor_contracts"]
    for worker, contract in contracts["workers"].items():
        assert contract.get("provider"), f"{worker} has no provider contract"
        assert contract.get("model"), f"{worker} has no model contract"
        assert cp.executor_provider(worker) == contract["provider"]
        assert cp.executor_model(worker) == contract["model"]

    # §13 executor isolation is asserted in the registry itself
    hicode = contracts["workers"]["hicode"]
    assert hicode["provider"] == "cliproxy-google"
    assert "cliproxy" in hicode["provider"]

    for key in (
        "ollama",
        "opencode_worker_default",
        "hicode_reasonix_cloud",
        "hp_optimizer_sampler",
    ):
        assert cp.executor_model(key), f"{key} contract is unreadable"

    # an unknown contract must degrade to the caller's default, never raise
    assert cp.executor_contract("does-not-exist") == {}
    assert cp.executor_model("does-not-exist", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# §13  executor isolation
# ---------------------------------------------------------------------------


def _executor_routes() -> dict:
    return _json(ROUTER)["executor_isolation"]


def test_hicode_route_isolation():
    iso = _executor_routes()
    assert iso["HICODE"] == "cliproxy-google"
    for forbidden in iso["forbidden_for_executors"]:
        assert forbidden not in str(iso["HICODE"]), f"HICODE must not route through {forbidden}"
    dsh_env = HOME / ".config" / "veya" / "dsh.env"
    if dsh_env.is_file():
        blob = dsh_env.read_text(encoding="utf-8")
        assert "CLIPROXY" in blob, "dsh.env no longer points at CLIProxyAPI"


def test_dsh_route_isolation():
    iso = _executor_routes()
    assert iso["DSH"] == "cliproxy-google"
    for forbidden in iso["forbidden_for_executors"]:
        assert forbidden not in str(iso["DSH"]), f"DSH must not route through {forbidden}"
    settings = HOME / ".dsh" / "settings.yaml"
    if settings.is_file():
        blob = settings.read_text(encoding="utf-8")
        assert "cliproxy" in blob, "dsh settings no longer point at cliproxy-google"
        for forbidden in ("veya1.2", "veya-free", "veya-nim", "veya-vl", "8791"):
            assert forbidden not in blob, f"dsh settings leaked {forbidden}"


# ---------------------------------------------------------------------------
# §11 / §6  no silent substitution, no silent provider fallback
# ---------------------------------------------------------------------------


def test_no_silent_model_substitution():
    """A legacy alias or a pool hop must always be visible in resp['router']."""

    required = {
        "REQUESTED_PROXY",
        "ROUTED_PROXY",
        "SELECTED_PROVIDER",
        "SELECTED_MODEL",
        "RESOLVED_UPSTREAM_MODEL",
        "ROUTING_REASON",
    }

    async def fake_pool_call(messages, kwargs):
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {},
            "router": {"route": "veya-free-rr", "provider": "opencode-go", "model": "m"},
        }

    import veya.obase.llm as llm_mod

    original = llm_mod._veya12_free_call
    llm_mod._veya12_free_call = fake_pool_call
    try:
        for alias, canonical, expect_dep in (
            ("veya1.2-free", "veya-free", True),
            ("veya-free", "veya-free", False),
            ("veya1.1", "veya1.2", True),
            ("veya1.2", "veya1.2", False),
        ):
            resp = asyncio.run(llm_mod.llm_call([], model=alias))
            router = resp["router"]
            missing = required - set(router)
            assert not missing, f"{alias}: router missing {missing}"
            assert router["REQUESTED_PROXY"] == alias
            assert router["ROUTED_PROXY"] == canonical
            # a pool hop that lands on the frontier must say so rather than
            # reporting an empty upstream as if it were a normal selection
            if not router["SELECTED_MODEL"]:
                assert "frontier" in router["ROUTING_REASON"], (
                    f"{alias}: empty upstream model but reason does not mention the "
                    f"frontier path: {router['ROUTING_REASON']!r}"
                )
            if expect_dep:
                assert router["DEPRECATION"]["visible"] is True
                assert router["DEPRECATION"]["silent_substitution"] is False
            else:
                assert "DEPRECATION" not in router
    finally:
        llm_mod._veya12_free_call = original


def test_no_silent_provider_fallback():
    """A frontier fallback must be labelled, never returned as a plain answer."""
    from veya.obase.llm import llm_call

    async def failing_pool(messages, kwargs):
        return {
            "choices": [{"message": {"role": "assistant", "content": "pool empty"}}],
            "usage": {},
            "error": True,
        }

    import veya.obase.llm as llm_mod

    original_free = llm_mod._veya12_free_call
    original_fb = llm_mod._frontier_fallback

    async def fake_frontier(messages, kwargs, *, reason):
        return {
            "choices": [{"message": {"role": "assistant", "content": "frontier answer"}}],
            "usage": {},
            "router": {"route": "frontier_fallback", "reason": reason},
        }

    llm_mod._veya12_free_call = failing_pool
    llm_mod._frontier_fallback = fake_frontier
    try:
        resp = asyncio.run(llm_call([], model="veya-free"))
        router = resp["router"]
        # the caller's requested proxy is still recorded even though the pool failed
        assert router["REQUESTED_PROXY"] == "veya-free"
        # the failed pool left no upstream selection, so the substitution is
        # not silent: routing reason records the failure path
        assert router["ROUTED_PROXY"] == "veya-free"
        assert router["SELECTED_PROVIDER"] == "" and router["SELECTED_MODEL"] == ""
        assert router["RESOLVED_UPSTREAM_MODEL"] == ""
        assert "frontier" in router["ROUTING_REASON"]
        assert router["route"] == "veya-free-frontier"
    finally:
        llm_mod._veya12_free_call = original_free
        llm_mod._frontier_fallback = original_fb


def test_frontier_endpoint_authority():
    """§12: exactly one frontier endpoint, and it is the verified one."""
    router = _json(ROUTER)
    endpoint = router["frontier"]["endpoint"]
    assert endpoint == "http://127.0.0.1:10100/v1"
    assert router["upgrade_target"]["endpoint"] == endpoint
    assert router["frontier_authority"]["authority_count"] == 1
    assert router["frontier_authority"]["endpoint"] == endpoint
    # the 502 endpoint must not appear as a live target anywhere
    assert "192.168.16.1:10101" not in json.dumps(
        {k: v for k, v in router.items() if k != "frontier_authority"}
    )
    retired = _json(REGISTRY)["retired_endpoints"]
    assert "192.168.16.1:10101" in retired
