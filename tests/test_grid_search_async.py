"""网格搜索异步战役测试 — Fire-and-Forget 分发 / ProcessPool Map / Automata 看管。

分三层验证:
1. 3O 主库机制 (oprim._grid_search): 展开/映射/规约/热力图 (真实 ProcessPool)
2. Veya 适配 (quant_coprocessor.execute_grid_search): 线程安全进度 + 参数注入
3. Automata 流水线: 工单即回 → 进度拦截 → 无头唤醒 → 通知交付
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from veya.platform import oprim as load_oprim

oprim = load_oprim()

# =========================================================================
# 一、3O 主库机制 (oprim._grid_search)
# =========================================================================


def test_expand_param_grid_cartesian():
    combos = oprim.expand_param_grid({"window": [10, 20], "risk": [0.01, 0.02, 0.05]})
    assert len(combos) == 6
    assert {"window": 10, "risk": 0.05} in combos
    assert oprim.expand_param_grid({}) == [{}]
    assert oprim.expand_param_grid({"a": [1, 2]}) == [{"a": 1}, {"a": 2}]


def _pool_worker(params: dict) -> dict:
    """测试用真实进程池 worker (模块级, 可 pickle)."""
    time.sleep(0.05)
    if params.get("window") == 30:
        raise ValueError("boom")  # 异常隔离验证
    return {"sharpe": 10.0 - params["window"], "total_return": 0.1}


def _test_pool_worker(strategy_code: str, asset_id: str, data_dir: str, params: dict) -> dict:
    """适配器测试用假 worker (模块级 → 可被 ProcessPool 子进程 pickle)."""
    return {"params": params, "sharpe": float(params.get("window", 0)) / 10.0, "total_return": 0.05}


def test_run_grid_search_processpool_map():
    """真实 ProcessPool: 全组合覆盖 + 异常隔离 + 进度序列."""
    combos = oprim.expand_param_grid({"window": [10, 20, 30, 40]})
    progress: list[tuple[int, int]] = []

    results = oprim.run_grid_search(
        _pool_worker,
        combos,
        max_workers=2,
        progress_callback=lambda done, total, latest: progress.append((done, total)),
    )

    assert len(results) == 4
    by_window = {r["params"]["window"]: r for r in results}
    assert by_window[10]["sharpe"] == 0.0
    assert by_window[40]["sharpe"] == -30.0
    assert "error" in by_window[30]  # 单组合异常被隔离
    # 进度 1..4, 每步 total=4
    assert [d for d, _ in progress] == [1, 2, 3, 4]
    assert all(t == 4 for _, t in progress)


def test_run_grid_search_single_combo_fast_path():
    """单组合快路径: 不启进程池, 直接同步执行."""
    progress: list[int] = []
    results = oprim.run_grid_search(
        _pool_worker, [{"window": 10}], progress_callback=lambda d, t, r: progress.append(d)
    )
    assert results[0]["sharpe"] == 0.0
    assert progress == [1]


def test_reduce_best():
    results = [
        {"params": {"w": 10}, "sharpe": 1.2},
        {"params": {"w": 20}, "error": "boom"},
        {"params": {"w": 30}, "sharpe": 2.1},
    ]
    best = oprim.reduce_best(results)
    assert best["params"] == {"w": 30}
    assert oprim.reduce_best([{"params": {}, "error": "x"}]) is None


def test_build_heatmap_payload():
    results = [
        {"params": {"window": 10, "risk": 0.01}, "sharpe": 1.0},
        {"params": {"window": 20, "risk": 0.01}, "sharpe": 1.5},
        {"params": {"window": 10, "risk": 0.05}, "sharpe": 0.8},
        {"params": {"window": 20, "risk": 0.05}, "error": "boom"},
    ]
    hm = oprim.build_heatmap_payload(results, "window", "risk")
    assert hm["xAxis"] == [10, 20]
    assert hm["yAxis"] == [0.01, 0.05]
    assert [0, 0, 1.0] in hm["data"]  # [x_idx, y_idx, value]
    assert [1, 0, 1.5] in hm["data"]
    assert [0, 1, 0.8] in hm["data"]
    assert len(hm["data"]) == 3  # 失败条目不进图


# =========================================================================
# 二、Veya 适配 (QuantCoprocessor.execute_grid_search)
# =========================================================================


@pytest.fixture
def fake_coprocessor(tmp_path):
    """换掉真实沙箱 worker → 进程池内跑假回测 (验证多核映射链路)."""
    import server.quant_coprocessor as qc

    # 假行情文件 (execute_grid_search 有存在性预检)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "BTCUSDT.parquet").write_bytes(b"")

    cop = qc.QuantCoprocessor(data_dir=data_dir, pool_size=2, pool_worker=_test_pool_worker)
    return cop


async def test_execute_grid_search_adapter(fake_coprocessor):
    """async 接口: 展开 → 进程池 → 进度回调跳回事件循环."""
    progress: list[tuple[int, int, float]] = []

    def on_progress(done: int, total: int, latest: dict) -> None:
        progress.append((done, total, latest.get("sharpe", -1)))

    results = await fake_coprocessor.execute_grid_search(
        "def run_strategy(df, hyper_params):\n    return df\n",
        "BTCUSDT",
        {"window": [10, 20, 30, 40]},
        progress_callback=on_progress,
    )
    await asyncio.sleep(0.05)  # 等 run_coroutine_threadsafe 跳转落地

    assert len(results) == 4
    assert {r["params"]["window"] for r in results} == {10, 20, 30, 40}
    assert sorted(d for d, _, _ in progress) == [1, 2, 3, 4]
    # 进度回调在事件循环线程执行 (线程安全跳转)
    assert all(isinstance(v, float) for _, _, v in progress)


async def test_execute_grid_search_missing_data_raises(tmp_path):
    import server.quant_coprocessor as qc

    cop = qc.QuantCoprocessor(data_dir=tmp_path, pool_size=1)
    with pytest.raises(FileNotFoundError):
        await cop.execute_grid_search("x", "GHOST", {"w": [1]})


# =========================================================================
# 三、Automata 流水线 (Fire-and-Forget 工单)
# =========================================================================


@pytest.fixture
async def fake_automata(tmp_path, monkeypatch):
    """最小 Automata: 假回测 + 假无头主脑 + 记录通知."""
    from server import automata as automata_mod

    # 假 coprocessor 单例方法 (流水线内部延迟 import; 实例属性替换 → 无 self 绑定)
    async def fake_grid_search(strategy_code, asset_id, param_grid, progress_callback=None):
        combos = oprim.expand_param_grid(param_grid)
        out = []
        for c in combos:
            r = {
                "params": c,
                "sharpe": float(c["window"]) / 100.0 + (0.05 if c.get("risk") == 0.05 else 0.0),
                "total_return": 0.1,
            }
            out.append(r)
            if progress_callback:
                progress_callback(len(out), len(combos), r)
        return out

    import server.quant_coprocessor as qc

    monkeypatch.setattr(qc.quant_coprocessor, "execute_grid_search", fake_grid_search)

    # 记录通知 (push 是同步方法, 流水线在事件循环内调用)
    pushed: list[dict] = []

    def fake_push(type_, title, content, payload=None):
        pushed.append({"type": type_, "title": title, "content": content, "payload": payload or {}})

    import server.notification_center as nc

    monkeypatch.setattr(nc.global_notifier, "push", fake_push)

    prompts: list[str] = []

    async def fake_headless(prompt: str) -> str:
        prompts.append(prompt)
        return "<veya-artifact>heatmap</veya-artifact>"

    a = automata_mod.VeyaAutomata(execute_callback=fake_headless)
    return a, pushed, prompts


async def test_automata_fire_and_forget_workflow(fake_automata):
    """工单即回 → 后台流水线: 进度拦截 → 物理 Reduce → 无头唤醒 → SUCCESS 交付."""
    automata, pushed, prompts = fake_automata

    task_id = automata.start_grid_search_task(
        "BTCUSDT",
        "def run_strategy(df, hyper_params):\n    return df\n",
        {"window": [10, 20, 30], "risk": [0.01, 0.05]},
        session_id="sess_1",
    )
    assert task_id.startswith("grid_search_")  # 立即返回, 不挂起

    # 等待后台流水线跑完
    await asyncio.gather(*list(automata._grid_tasks))
    await asyncio.sleep(0.05)

    types = [p["type"] for p in pushed]
    assert "INFO" in types  # 进度弹窗 (3/6 之类)
    assert "SUCCESS" in types  # 终极交付
    final = next(p for p in pushed if p["type"] == "SUCCESS")
    assert "网格搜索完成" in final["title"]
    assert final["payload"]["best"]["params"] == {"window": 30, "risk": 0.05}  # 夏普最高
    assert final["payload"]["best"]["sharpe"] == pytest.approx(0.35)
    # 热力图载荷 (window × risk 二维)
    assert final["payload"]["heatmap"]["xAxis"] == [10, 20, 30]
    assert final["payload"]["heatmap"]["yAxis"] == [0.01, 0.05]
    # 无头主脑被唤醒, prompt 含最优参数
    assert len(prompts) == 1
    assert "window': 30" in prompts[0] or "window': 30" in json.dumps(prompts[0])
    assert "<veya-artifact>" in final["payload"]["content"]


async def test_automata_all_failed_notifies_error(fake_automata, monkeypatch):
    """所有组合失败 → ERROR 通知, 不唤醒无头主脑."""
    automata, pushed, prompts = fake_automata
    import server.quant_coprocessor as qc

    async def failing(strategy_code, asset_id, param_grid, progress_callback=None):
        return [{"params": c, "error": "sandbox died"} for c in oprim.expand_param_grid(param_grid)]

    monkeypatch.setattr(qc.quant_coprocessor, "execute_grid_search", failing)
    automata.start_grid_search_task("BTCUSDT", "x", {"window": [10, 20]})
    await asyncio.gather(*list(automata._grid_tasks))

    assert pushed[-1]["type"] == "ERROR"
    assert "所有参数组合均报错" in pushed[-1]["content"]
    assert prompts == []
