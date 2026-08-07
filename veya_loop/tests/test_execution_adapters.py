"""执行适配器测试 — 模板 + RestartAdapter 参考实现 (P0)。

门禁:
  1. 模板契约: build_argv / permission_action / describe;
  2. RestartAdapter 两种模式 (systemctl / docker) argv 正确;
  3. dispatch_via_adapter 端到端: 授权 → 硬化执行 → 审计 (nonce 贯穿);
  4. 未授权 → denied + 审计留痕;
  5. probe_first: 服务不可用 → 阻断派发 (denied);
  6. 审计记录含适配器描述 (notes)。
"""

from __future__ import annotations

from typing import Any

import pytest

from veya_loop import (
    AuditEmitter,
    HardenedExecutor,
    MemorySink,
    PermissionContract,
    RestartAdapter,
    dispatch_via_adapter,
)


def test_restart_adapter_argv_modes():
    sys = RestartAdapter(mode="systemctl")
    assert sys.build_argv("api_service") == ["systemctl", "restart", "api_service"]
    assert sys.probe_argv("api_service") == ["systemctl", "is-active", "api_service"]
    assert sys.permission_action("api_service") == "restart:api_service"
    assert "api_service" in sys.describe("api_service")

    docker = RestartAdapter(mode="docker")
    assert docker.build_argv("api_service") == ["docker", "restart", "api_service"]
    with pytest.raises(ValueError):
        RestartAdapter(mode="k8s")


class EchoRestartAdapter(RestartAdapter):
    """测试用假重启: 不真重启系统, 输出确认 (确定性验证派发链路与审计)。"""

    def build_argv(self, target: str, **params: Any) -> list[str]:
        return ["echo", f"restarting:{target}"]


def test_dispatch_via_adapter_authorized_and_audited(tmp_path):
    """授权通过 → approved_executed; 审计 decide+execute, nonce 贯穿, notes 含描述。"""
    contract = PermissionContract()
    contract.grant("restart:*")
    emitter = AuditEmitter(sink=MemorySink())
    adapter = EchoRestartAdapter(mode="systemctl")

    with HardenedExecutor(isolation="netns", base_dir=str(tmp_path / "pool")) as ex:
        result = dispatch_via_adapter(
            adapter,
            "api_service",
            contract=contract,
            executor=ex,
            emitter=emitter,
            actor="operator",
        )
    assert result.status == "approved_executed"
    assert result.nonce and result.nonce.startswith("cap_")
    assert result.audit_id

    chain = emitter.replay()
    assert [e["event_type"] for e in chain] == ["decide", "execute"]
    assert chain[0]["decision"]["chosen_strategy"] == "restart:api_service"
    assert chain[1]["execution"]["capability_nonce"] == result.nonce
    assert chain[1]["execution"]["status"] == "ok"
    # 审计备注含适配器描述
    assert "api_service" in chain[1]["context"]["notes"]


def test_dispatch_via_adapter_denied_without_rule():
    """无规则 → deny-by-default; 审计记录拒绝。"""
    contract = PermissionContract()  # 无规则
    emitter = AuditEmitter(sink=MemorySink())
    adapter = RestartAdapter()

    result = dispatch_via_adapter(
        adapter,
        "db",
        contract=contract,
        emitter=emitter,
    )
    assert result.status == "denied"
    assert result.outcome is None
    chain = emitter.replay()
    assert chain[0]["event_type"] == "decide"
    assert chain[0]["decision"]["denied"] is True
    assert "deny-by-default" in chain[0]["decision"]["reason"]


def test_dispatch_via_adapter_probe_first_blocks(tmp_path):
    """probe_first: 服务不可用 → denied (probe 失败阻断), 不执行。"""
    contract = PermissionContract()
    contract.grant("restart:*")
    emitter = AuditEmitter(sink=MemorySink())
    adapter = RestartAdapter(mode="systemctl")

    with HardenedExecutor(isolation="netns", base_dir=str(tmp_path / "pool")) as ex:
        result = dispatch_via_adapter(
            adapter,
            "ghost_service_xyz",
            contract=contract,
            executor=ex,
            emitter=emitter,
            probe_first=True,
        )
    assert result.status == "denied"
    assert "probe" in result.reason
    chain = emitter.replay()
    # 审计: decide(denied) — 未走到 execute
    assert [e["event_type"] for e in chain] == ["decide"]
    assert chain[0]["decision"]["denied"] is True


def test_hardened_executor_env_injected(tmp_path):
    """HardenedExecutor(env=...) 真正注入自定义环境到沙箱 (清空宿主环境 + 按需注入)。"""
    with HardenedExecutor(
        isolation="none",
        base_dir=str(tmp_path / "pool"),
        env={"PATH": "/usr/bin:/bin", "VEYA_MARKER": "42"},
    ) as ex:
        assert ex.env == {"PATH": "/usr/bin:/bin", "VEYA_MARKER": "42"}
        out = ex.execute(["printenv", "VEYA_MARKER"])
    assert out.ok, out.stderr
    assert out.stdout.strip() == "42"


def test_hardened_executor_default_env_frozen(tmp_path):
    """未给 env → 冻结的确定性默认环境 (PYTHONHASHSEED=0), 且不泄漏宿主自定义变量。"""
    import os

    os.environ["VEYA_HOST_SECRET"] = "leak_me"
    try:
        with HardenedExecutor(isolation="none", base_dir=str(tmp_path / "pool2")) as ex:
            assert ex.env is None
            seed = ex.execute(["printenv", "PYTHONHASHSEED"])
            leaked = ex.execute(["printenv", "VEYA_HOST_SECRET"])
        assert seed.stdout.strip() == "0"
        assert leaked.stdout.strip() == ""  # 宿主变量未泄入沙箱
    finally:
        del os.environ["VEYA_HOST_SECRET"]


def test_dispatch_via_adapter_without_executor_dispatches_only():
    """无执行器 → approved_dispatched (仅授权派发, 调用方自行执行)。"""
    contract = PermissionContract()
    contract.grant("restart:*")
    emitter = AuditEmitter(sink=MemorySink())
    adapter = EchoRestartAdapter(mode="docker")

    result = dispatch_via_adapter(adapter, "worker", contract=contract, emitter=emitter)
    assert result.status == "approved_dispatched"
    assert result.nonce
    chain = emitter.replay()
    assert [e["event_type"] for e in chain] == ["decide", "execute"]
    assert chain[1]["execution"]["status"] == "dispatched"
