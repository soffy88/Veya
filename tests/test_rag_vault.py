"""Workspace RAG + Zero-Trust Vault 测试 — 最后两块拼图。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.coordinator_master import MASTER_SYSTEM_PROMPT, MasterCoordinator
from server.memory_bank import VeyaMemoryBank
from server.workspace_rag import WorkspaceRAGEngine
from server.zero_trust_vault import VeyaVault

# =========================================================================
# 一、Workspace Context RAG
# =========================================================================


def _write_project(root: Path) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "indicators.py").write_text(
        '"""Technical indicators package."""\n'
        "import numpy as np\n\n"
        "def calculate_vwap(data):\n"
        '    """Compute volume-weighted average price."""\n'
        "    v = data['volume']\n"
        "    p = data['close']\n"
        "    return (p * v).cumsum() / v.cumsum()\n\n"
        "def calculate_ema(data, window=10):\n"
        '    """Exponential moving average."""\n'
        "    return data['close'].ewm(span=window).mean()\n",
        encoding="utf-8",
    )
    (root / "pkg" / "risk.py").write_text(
        "class RiskMonitor:\n"
        '    """Monitor position risk and exposure."""\n'
        "    def check_leverage(self, position, max_leverage=10):\n"
        "        return position.notional / position.equity <= max_leverage\n",
        encoding="utf-8",
    )


@pytest.fixture
def rag(tmp_path) -> WorkspaceRAGEngine:
    _write_project(tmp_path)
    return WorkspaceRAGEngine(workspace_root=tmp_path)


def test_rag_indexes_ast_chunks(rag):
    """AST 切片: 函数/类级 Chunk, 不塞全量源码。"""
    stats = rag.get_stats()
    assert stats["files"] == 2
    assert stats["chunks"] == 3  # calculate_vwap + calculate_ema + RiskMonitor
    assert stats["types"] == {"function": 2, "class": 1}


def test_rag_semantic_search(rag):
    """语义检索: 命中 vwap 函数 chunk 并附带源文件上下文。"""
    result = rag.search_context("vwap volume weighted price", top_k=2)
    assert "calculate_vwap" in result
    assert "indicators.py" in result
    assert "匹配度:" in result
    # 附带上下文行(帮助定位修改点)
    assert "cumsum" in result


def test_rag_camel_case_tokenize(rag):
    """camelCase 拆分: query 'ema' 能命中 calculate_ema。"""
    result = rag.search_context("ema exponential moving average", top_k=1)
    assert "calculate_ema" in result


def test_rag_incremental_reindex(tmp_path):
    """增量: 新文件落地后, 检索自动感知(无需显式 reindex)。"""
    _write_project(tmp_path)
    engine = WorkspaceRAGEngine(workspace_root=tmp_path)
    assert engine.get_stats()["chunks"] == 3

    # 新文件落地(模拟 Automata 检测到代码变动)
    (tmp_path / "pkg" / "execution.py").write_text(
        "def place_binance_order(symbol, qty, side):\n"
        '    """Place an order on Binance."""\n'
        "    return {'symbol': symbol, 'qty': qty, 'side': side}\n",
        encoding="utf-8",
    )
    result = engine.search_context("binance order placement", top_k=1)
    assert "place_binance_order" in result  # 自愈: 自动增量重索引
    assert engine.get_stats()["chunks"] == 4

    # 删除文件 → 索引清理
    (tmp_path / "pkg" / "execution.py").unlink()
    engine.search_context("binance", top_k=1)
    assert engine.get_stats()["chunks"] == 3


def test_rag_reindex_force(rag):
    result = rag.reindex_workspace(force=True)
    assert "索引更新完毕" in result
    assert "3 个函数/类" in result


# =========================================================================
# 二、Zero-Trust Secrets Vault
# =========================================================================


@pytest.fixture
def vault(tmp_path) -> VeyaVault:
    return VeyaVault(vault_dir=tmp_path / "vault", approval_timeout=5.0)


def test_vault_secret_encrypted_at_rest(vault, tmp_path):
    """密钥 Fernet 加密落盘: 文件里绝无明文。"""
    vault.set_secret("binance_prod_key", "BINANCE_REAL_KEY_12345!@#")
    raw = (tmp_path / "vault" / "vault.json").read_text(encoding="utf-8")
    assert "BINANCE_REAL_KEY_12345!@#" not in raw  # 明文不可见
    assert vault.has_secret("binance_prod_key")
    assert vault.list_secret_ids() == ["binance_prod_key"]


def test_vault_restore_after_restart(tmp_path):
    """重启恢复: 新实例解密同一金库。"""
    v1 = VeyaVault(vault_dir=tmp_path / "vault")
    v1.set_secret("aws_deploy_token", "AWS_PROD_999888")
    v2 = VeyaVault(vault_dir=tmp_path / "vault")  # 模拟重启
    assert v2.has_secret("aws_deploy_token")


async def _approval_flow(vault: VeyaVault, approved: bool, callback):
    """启动 execute_secure_tool 协程 + 模拟人类点击。"""
    task = asyncio.create_task(
        vault.execute_secure_tool(
            tool_name="binance_place_order",
            intent_args={"symbol": "BTCUSDT", "qty": 0.1},
            required_vault_id="binance_prod_key",
            physical_tool_callback=callback,
        )
    )
    # 等挂起注册
    for _ in range(50):
        if vault.pending_approvals:
            break
        await asyncio.sleep(0.01)
    assert vault.pending_approvals, "execute_secure_tool 应挂起等待审批"
    task_id = next(iter(vault.pending_approvals))
    resolved = vault.resolve_approval(task_id, approved)
    assert resolved
    return await task


@pytest.mark.asyncio
async def test_vault_approve_injects_secret(vault):
    """审批通过 → 物理回调收到真实密钥(隐式注入, 大模型全程瞎眼)。"""
    vault.set_secret("binance_prod_key", "BINANCE_REAL_KEY_12345!@#")
    captured = {}

    async def fake_binance(**kwargs):
        captured["secret"] = kwargs.pop("_injected_secret")
        captured["args"] = kwargs
        return "ORDER_FILLED"

    result = await _approval_flow(vault, approved=True, callback=fake_binance)
    assert "授权执行完毕" in result
    assert "ORDER_FILLED" in result
    assert captured["secret"] == "BINANCE_REAL_KEY_12345!@#"  # 真实密钥注入
    assert captured["args"] == {"symbol": "BTCUSDT", "qty": 0.1}
    assert vault.pending_approvals == {}  # 清理


@pytest.mark.asyncio
async def test_vault_reject_blocks(vault):
    vault.set_secret("binance_prod_key", "BINANCE_REAL_KEY_12345!@#")

    async def fake_binance(**kwargs):
        raise AssertionError("被拒绝的调用不应执行物理层")

    result = await _approval_flow(vault, approved=False, callback=fake_binance)
    assert "拒绝授权" in result


@pytest.mark.asyncio
async def test_vault_missing_vault_id(vault):
    async def fake(**kwargs):
        return "should not run"

    result = await vault.execute_secure_tool(
        tool_name="x", intent_args={}, required_vault_id="ghost", physical_tool_callback=fake
    )
    assert "不存在凭据 ID" in result


@pytest.mark.asyncio
async def test_vault_approval_timeout_auto_reject(tmp_path):
    """审批超时 → 自动拒绝(工业级必备)。"""
    vault = VeyaVault(vault_dir=tmp_path / "vault", approval_timeout=0.2)
    vault.set_secret("k", "s")

    async def fake(**kwargs):
        return "ok"

    result = await vault.execute_secure_tool(
        tool_name="x", intent_args={}, required_vault_id="k", physical_tool_callback=fake
    )
    assert "审批超时" in result
    assert "自动拒绝" in result


def test_vault_hitl_event_payload(vault, tmp_path):
    """SSE 播报: vault_hitl 事件携带 task_id/action, 供前端悬浮窗渲染。"""
    from server.events import _on_step_ctx

    events = []
    token = _on_step_ctx.set(events.append)
    try:
        vault.set_secret("k", "s")
        task = asyncio.run(
            vault.execute_secure_tool(
                tool_name="deploy",
                intent_args={},
                required_vault_id="k",
                physical_tool_callback=lambda **k: "ok",
            )
        )
    finally:
        _on_step_ctx.reset(token)
    # 超时(5s)自动拒绝
    assert "审批超时" in task
    hitl = [e for e in events if e.get("type") == "vault_hitl"]
    assert len(hitl) == 1
    assert "请求动用生产密钥" in hitl[0]["title"]
    assert "deploy" in hitl[0]["payload"]["action"]
    assert hitl[0]["payload"]["vault_id"] == "k"
    assert hitl[0]["payload"]["task_id"]


# =========================================================================
# 三、主脑接入
# =========================================================================


def test_system_prompt_has_rag_and_vault():
    assert "# WORKSPACE RAG (CRITICAL)" in MASTER_SYSTEM_PROMPT
    assert "system_workspace_search" in MASTER_SYSTEM_PROMPT
    assert "# ZERO-TRUST VAULT (CRITICAL)" in MASTER_SYSTEM_PROMPT
    assert "system_secure_exec" in MASTER_SYSTEM_PROMPT
    assert "Never ask the user to paste a secret" in MASTER_SYSTEM_PROMPT


def test_system_schemas_include_rag_and_vault(tmp_path):
    coord = MasterCoordinator(memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"))
    names = {s["function"]["name"] for s in coord.get_system_schemas()}
    assert "system_workspace_search" in names
    assert "system_workspace_reindex" in names
    assert "system_secure_exec" in names
    secure = next(
        s for s in coord.get_system_schemas() if s["function"]["name"] == "system_secure_exec"
    )
    assert secure["function"]["parameters"]["required"] == [
        "tool_name",
        "intent_args",
        "required_vault_id",
    ]


@pytest.mark.asyncio
async def test_master_rag_tool_route(tmp_path):
    _write_project(tmp_path)
    rag = WorkspaceRAGEngine(workspace_root=tmp_path)
    coord = MasterCoordinator(
        memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"), rag_engine=rag
    )
    out = await coord.handle_tool_call("system_workspace_search", {"query": "vwap"})
    assert "calculate_vwap" in out
    out = await coord.handle_tool_call("system_workspace_reindex", {})
    assert "索引更新完毕" in out


@pytest.mark.asyncio
async def test_master_secure_exec_full_loop(tmp_path):
    """完整闭环: 主脑 system_secure_exec → HITL 挂起 → 人类批准 → 密钥注入物理调用。"""
    vault = VeyaVault(vault_dir=tmp_path / "vault", approval_timeout=5.0)
    vault.set_secret("binance_prod_key", "BINANCE_REAL_KEY_12345!@#")
    coord = MasterCoordinator(
        memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"), vault=vault
    )

    captured = {}

    async def physical_binance(**kwargs):
        captured["secret"] = kwargs.pop("_injected_secret")
        return f"filled {kwargs['symbol']}"

    coord.register_secure_tool("binance_place_order", physical_binance)

    task = asyncio.create_task(
        coord.handle_tool_call(
            "system_secure_exec",
            {
                "tool_name": "binance_place_order",
                "intent_args": {"symbol": "BTCUSDT", "qty": 0.1},
                "required_vault_id": "binance_prod_key",
            },
        )
    )
    for _ in range(50):
        if vault.pending_approvals:
            break
        await asyncio.sleep(0.01)
    task_id = next(iter(vault.pending_approvals))
    vault.resolve_approval(task_id, approved=True)

    out = await task
    assert "授权执行完毕" in out
    assert "filled BTCUSDT" in out
    assert captured["secret"] == "BINANCE_REAL_KEY_12345!@#"
    # 密钥绝不进入对话: 主脑工具结果中不含明文
    assert "BINANCE_REAL_KEY" not in out


def test_vault_routes_reachable(tmp_path, monkeypatch):
    """审批端点已挂载: POST /api/v1/tasks/{id}/approve。"""
    from fastapi.testclient import TestClient

    from server.app import app

    monkeypatch.setenv("VEYA_VAULT_DIR", str(tmp_path / "vault"))
    client = TestClient(app)
    # 未知 task_id → 404
    resp = client.post("/api/v1/tasks/ghost/approve", json={"approved": True})
    assert resp.status_code == 404
    client.close()


# =========================================================================
# 三、HITL 闭环修复: 事件送达悬浮窗 → 审批按钮 → 协程唤醒 → 密钥注入
# =========================================================================


@pytest.mark.asyncio
async def test_hitl_toast_approve_resolves_vault_task(vault, monkeypatch):
    """断点1/2/3 修复: vault_hitl → 通知中心 HITL_REQUIRED 悬浮窗 →
    approve 端点 → resolve_approval 唤醒挂起协程 → 密钥隐式注入物理回调。"""
    from server.notification_center import global_notifier
    from server.routes.notifications import ApproveRequest, approve_notification

    # 审批端点经 server.zero_trust_vault.global_vault 解析 — 测试注入独立实例
    monkeypatch.setattr("server.zero_trust_vault.global_vault", vault)

    vault.set_secret("binance_prod_key", "BINANCE_REAL_KEY_12345!@#")
    captured = {}

    async def fake_binance(**kwargs):
        captured["secret"] = kwargs.pop("_injected_secret")
        return "ORDER_FILLED"

    q = global_notifier.connect()
    try:
        task = asyncio.create_task(
            vault.execute_secure_tool(
                tool_name="binance_place_order",
                intent_args={"symbol": "BTCUSDT", "qty": 0.1},
                required_vault_id="binance_prod_key",
                physical_tool_callback=fake_binance,
            )
        )

        # 1. 断点1/2: 悬浮窗送达全局通知中心(所有 tab 可见, 带 task_id)
        notif = await asyncio.wait_for(q.get(), timeout=5)
        assert notif["type"] == "HITL_REQUIRED"
        assert "请求动用生产密钥" in notif["title"]
        task_id = notif["payload"]["task_id"]
        assert task_id
        assert notif["payload"]["action"] == "binance_place_order"

        # 2. 断点3: 前端按钮 → 通知中心审批端点 → 唤醒挂起协程
        resp = await approve_notification(notif["id"], ApproveRequest(approved=True))
        assert resp["status"] == "ok"
        assert resp["vault_task_resolved"] is True

        # 3. 协程恢复: 真实密钥隐式注入, 大模型全程瞎眼
        result = await asyncio.wait_for(task, timeout=5)
        assert "授权执行完毕" in result
        assert captured["secret"] == "BINANCE_REAL_KEY_12345!@#"

        # 4. vault_resolved 事件 → 悬浮窗自动 dismiss
        frame = await asyncio.wait_for(q.get(), timeout=5)
        assert frame["type"] == "DISMISS"
        assert frame["payload"]["id"] == notif["id"]
    finally:
        global_notifier.disconnect(q)


@pytest.mark.asyncio
async def test_hitl_toast_reject_via_endpoint(vault, monkeypatch):
    """断点3 拒绝路径: Reject 按钮同样唤醒协程, 物理层绝不执行。"""
    from server.notification_center import global_notifier
    from server.routes.notifications import ApproveRequest, approve_notification

    monkeypatch.setattr("server.zero_trust_vault.global_vault", vault)
    vault.set_secret("k", "s")

    async def fake(**kwargs):
        raise AssertionError("被拒绝的调用不应执行物理层")

    q = global_notifier.connect()
    try:
        task = asyncio.create_task(
            vault.execute_secure_tool(
                tool_name="x", intent_args={}, required_vault_id="k", physical_tool_callback=fake
            )
        )
        notif = await asyncio.wait_for(q.get(), timeout=5)
        resp = await approve_notification(notif["id"], ApproveRequest(approved=False))
        assert resp["vault_task_resolved"] is True
        result = await asyncio.wait_for(task, timeout=5)
        assert "拒绝授权" in result
    finally:
        global_notifier.disconnect(q)


@pytest.mark.asyncio
async def test_hitl_toast_timeout_auto_dismiss(tmp_path):
    """审批超时 → 自动拒绝 + 悬浮窗自动关闭(不留陈旧审批卡片)。"""
    from server.notification_center import global_notifier

    vault = VeyaVault(vault_dir=tmp_path / "vault", approval_timeout=0.2)
    vault.set_secret("k", "s")

    async def fake(**kwargs):
        return "ok"

    q = global_notifier.connect()
    try:
        result = await vault.execute_secure_tool(
            tool_name="x", intent_args={}, required_vault_id="k", physical_tool_callback=fake
        )
        assert "审批超时" in result

        notif = await asyncio.wait_for(q.get(), timeout=5)
        assert notif["type"] == "HITL_REQUIRED"
        frame = await asyncio.wait_for(q.get(), timeout=5)
        assert frame["type"] == "DISMISS"
        assert frame["payload"]["id"] == notif["id"]
    finally:
        global_notifier.disconnect(q)


# =========================================================================
# 四、物理工具接线(缺口修复): 金库注册真实物理回调
# =========================================================================


def test_vault_physical_tools_registered():
    """缺口修复: 宿主接线把 feishu_webhook / binance_signed_request 注册进主脑金库。"""
    coord = MasterCoordinator(llm_fn=lambda **kw: {})
    callbacks = coord._agent._vault_tool_callbacks
    assert "feishu_webhook" in callbacks
    assert "binance_signed_request" in callbacks


@pytest.mark.asyncio
async def test_feishu_physical_callback_injects_secret(monkeypatch):
    """飞书 webhook 物理推送: webhook URL 经 _injected_secret 隐式注入。"""
    from server.vault_physical_tools import _feishu_webhook_callback

    captured = {}

    class FakeAdapter:
        def __init__(self, webhook_url):
            captured["url"] = webhook_url

        async def push(self, content, payload=None):
            captured["content"] = content
            return "✅ 已成功分发至飞书群组。"

    monkeypatch.setattr("server.channels.adapters.FeishuAdapter", FakeAdapter)
    result = await _feishu_webhook_callback(
        content="研报", title="回测日报", _injected_secret="https://open.feishu.cn/hook/SECRET"
    )
    assert captured["url"] == "https://open.feishu.cn/hook/SECRET"
    assert captured["content"] == "研报"
    assert "飞书" in result


@pytest.mark.asyncio
async def test_binance_physical_callback_signed_request(monkeypatch):
    """Binance 私有接口: HMAC-SHA256 签名 + API key 头, 密钥绝不外泄。"""
    from server.vault_physical_tools import _binance_signed_request_callback

    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        text = '{"balances":[]}'

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, headers=None):
            captured.update(method=method, url=url, headers=headers)
            return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    out = await _binance_signed_request_callback(
        path="/api/v3/account", query="", _injected_secret="MY_API_KEY:MY_SECRET"
    )
    assert captured["headers"]["X-MBX-APIKEY"] == "MY_API_KEY"
    assert "timestamp=" in captured["url"]
    assert "signature=" in captured["url"]
    assert "MY_SECRET" not in out  # 签名用后即弃, 明文永不外泄
