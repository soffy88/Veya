"""O3 Observer 测试 — 真沙箱 / 真快照 / 真执行, 无 mock。

覆盖: 内容寻址快照 / unshare 隔离 / 预热池 / 稠密奖励 / lookahead 裁决 /
PUCT 树搜索 (误导性先验下仍收敛到最优主变着)。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from veya.platform import oprim as load_oprim

oprim = load_oprim()

# =========================================================================
# 测试夹具: 一个有两处 bug 的模块 (baseline)
# =========================================================================

CALC_BASE = '''"""一个有两处 bug 的小模块。"""


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def percentile(values, p):
    """返回第 p 百分位（p 取值 0~100）。"""
    if not values:
        raise ValueError("values 不能为空")
    s = sorted(values)
    idx = int(len(s) * p / 100)
    return s[idx]


def mean(values):
    return sum(values) / len(values)
'''

TEST_CALC = '''import unittest

from calc import clamp, mean, percentile


class T(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(clamp(5, 0, 3), 3)
        self.assertEqual(clamp(-1, 0, 3), 0)

    def test_percentile_mid(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 50), 3)

    def test_percentile_max(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 100), 5)

    def test_mean_empty_raises(self):
        with self.assertRaises(ValueError):
            mean([])


if __name__ == "__main__":
    unittest.main()
'''

GOOD_CALC = CALC_BASE.replace("idx = int(len(s) * p / 100)",
                              "idx = min(len(s) - 1, int(len(s) * p / 100))") \
                      .replace("def mean(values):\n    return",
                               "def mean(values):\n    if not values:\n"
                               "        raise ValueError('empty')\n    return")


@pytest.fixture
def base_dir(tmp_path):
    d = tmp_path / "base"
    d.mkdir()
    (d / "calc.py").write_text(CALC_BASE, encoding="utf-8")
    (d / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
    return d


# =========================================================================
# 一、快照: 内容寻址 + 硬链接 CoW
# =========================================================================

def test_tree_digest_content_addressing(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    oprim.atomic_write(str(src / "a.txt"), "hello")
    oprim.atomic_write(str(src / "sub" / "b.txt"), "world")
    d1 = oprim.tree_digest(str(src))
    import os
    os.utime(src / "a.txt", (0, 0))
    assert oprim.tree_digest(str(src)) == d1          # 只认内容不认 mtime
    oprim.atomic_write(str(src / "a.txt"), "hello!")
    assert oprim.tree_digest(str(src)) != d1          # 内容变则 digest 变


def test_snapshot_store_hardlink_cow(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    oprim.atomic_write(str(src / "a.txt"), "hello")
    store = oprim.SnapshotStore(str(tmp_path / "store"),
                                backend=oprim.HardlinkBackend())
    dig = store.commit(str(src))
    assert store.exists(dig)
    assert store.commit(str(src)) == dig              # 幂等
    out = store.checkout(dig, str(tmp_path / "out"))
    oprim.atomic_write(str(Path(out) / "a.txt"), "MUTATED")  # 原子写不会顺着硬链接改坏 store
    assert oprim.tree_digest(str(src)) == dig


def test_snapshot_store_self_recursion_guard(tmp_path):
    """快照库建在被快照目录内部时, 必须剔除自身, 不能自递归 (ENAMETOOLONG 防线)."""
    work = tmp_path / "work"
    work.mkdir()
    oprim.atomic_write(str(work / "a.txt"), "hello")
    store = oprim.SnapshotStore(str(work / ".snapshots"))
    dig = store.commit(str(work))                     # 修复前这里会无限递归
    assert store.exists(dig)
    out = store.checkout(dig, str(work / "out"))
    assert (Path(out) / "a.txt").read_text() == "hello"
    assert not (Path(out) / ".snapshots").exists()   # 快照库自身被剔除


# =========================================================================
# 二、沙箱: unshare 隔离 + 预热池
# =========================================================================

def test_sandbox_isolation(tmp_path):
    sb = oprim.LocalSandbox(str(tmp_path), isolation="netns")
    r = sb.run(["python3", "-c", "print('hi')"])
    assert r.ok and "hi" in r.stdout
    # 网络被切断 (仅当 unshare 可用; seccomp 禁用的 CI 环境跳过此断言)
    if sb.isolation == "netns":
        r = sb.run(["python3", "-c",
                    "import socket;socket.create_connection(('1.1.1.1',53),timeout=2)"])
        assert r.exit_code != 0
    # 宿主环境变量不泄漏, 确定性变量已注入 (两种隔离级别下都成立: env 显式注入)
    r = sb.run(["python3", "-c", "import os;print(os.environ.get('SECRET','<none>'))"])
    assert "<none>" in r.stdout
    r = sb.run(["python3", "-c", "import os;print(os.environ['PYTHONHASHSEED'])"])
    assert r.stdout.strip() == "0"
    # 超时被杀掉
    r = sb.run(["sleep", "5"], timeout_s=0.4)
    assert r.timed_out and not r.ok
    sb.destroy()


def test_sandbox_pool_prewarm_and_reuse(tmp_path):
    pool = oprim.SandboxPool(size=3, base_dir=str(tmp_path / "pool"),
                             isolation="netns").prewarm()
    assert pool.stats.created == 3
    got = [pool.acquire() for _ in range(3)]
    for s in got:
        pool.release(s)
    pool.acquire()
    assert pool.stats.created == 3                   # 实例被复用而非重建
    pool.shutdown()


# =========================================================================
# 三、稠密奖励
# =========================================================================

def test_reward_probes_and_gates(base_dir, tmp_path):
    baseline = {rel: (base_dir / rel).read_text(encoding="utf-8")
                for rel in ("calc.py", "test_calc.py")}
    sb = oprim.LocalSandbox(str(tmp_path), isolation="netns")
    shutil.copytree(base_dir, sb.workspace, dirs_exist_ok=True)

    probes = [oprim.py_syntax_gate(["calc.py"]),
              oprim.unittest_probe(weight=3.0),
              oprim.DiffSizeProbe(baseline, weight=1.0)]
    rw = oprim.run_probes(probes, sb)
    assert rw.value == pytest.approx((0.5 * 3 + 1.0) / 4)   # 基线 2/4 通过
    assert not rw.gated

    # gate 未过 → 奖励归零并短路
    oprim.atomic_write(str(Path(sb.workspace) / "calc.py"), "def broken(:\n")
    rw2 = oprim.run_probes(probes, sb)
    assert rw2.value == 0.0 and rw2.gated
    assert len(rw2.probes) == 1                      # 短路省掉后续探针
    sb.destroy()


def test_frozen_file_gate_blocks_cheating(base_dir, tmp_path):
    """删测试刷分被冻结文件门拦下 (宿主侧哈希比对)。"""
    sb = oprim.LocalSandbox(str(tmp_path), isolation="netns")
    shutil.copytree(base_dir, sb.workspace, dirs_exist_ok=True)
    frozen = {"test_calc.py": hashlib.sha256(TEST_CALC.encode()).hexdigest()}
    fp = oprim.FileFrozenProbe(frozen)
    assert fp.run(sb).score == 1.0
    oprim.atomic_write(str(Path(sb.workspace) / "test_calc.py"), "# 删光了")
    assert fp.run(sb).score == 0.0
    sb.destroy()


# =========================================================================
# 四、单步 lookahead 裁决
# =========================================================================

def test_lookahead_selects_best_and_escalates(base_dir, tmp_path):
    baseline = {rel: (base_dir / rel).read_text(encoding="utf-8")
                for rel in ("calc.py", "test_calc.py")}
    frozen = {"test_calc.py": hashlib.sha256(TEST_CALC.encode()).hexdigest()}

    def wp(pid, path, content):
        return oprim.ActionPlan(pid, [oprim.Action(f"{pid}_a", "write_file",
                                                   {"path": path, "content": content})])

    plans = [
        wp("c_good", "calc.py", GOOD_CALC),
        wp("c_bad", "calc.py", "def broken(:\n"),
        oprim.ActionPlan("c_irr", [oprim.Action("a", "exec", {"cmd": "rm -rf /"},
                                                reversibility=oprim.Reversibility.IRREVERSIBLE)]),
    ]
    probes = [oprim.py_syntax_gate(["calc.py", "test_calc.py"]),
              oprim.FileFrozenProbe(frozen),
              oprim.unittest_probe(weight=3.0),
              oprim.DiffSizeProbe(baseline, weight=1.0)]
    store = oprim.SnapshotStore(str(tmp_path / "store"))

    with oprim.SandboxPool(size=3, base_dir=str(tmp_path / "pool"), isolation="netns") as pool:
        v = oprim.lookahead(plans, str(base_dir), store, pool, probes,
                            min_reward=0.9,
                            divergences=[oprim.Divergence(
                                "data_scale", "样本 5 行 vs 生产 4000 万行", "high")])
    assert v.chosen is not None and v.chosen.plan_id == "c_good"
    assert any(r.plan_id == "c_bad" and r.gated for r in v.ranked)
    # 不可逆候选根本没进沙箱
    assert "c_irr" not in {r.plan_id for r in v.ranked}
    assert any(e.plan_id == "c_irr" and e.reason_code == "IRREVERSIBLE"
               for e in v.escalations)
    assert len(v.divergences) == 1                   # sim-to-real 差异带进决策记录


def test_lookahead_reproducible_and_threshold(base_dir, tmp_path):
    baseline = {rel: (base_dir / rel).read_text(encoding="utf-8")
                for rel in ("calc.py", "test_calc.py")}
    frozen = {"test_calc.py": hashlib.sha256(TEST_CALC.encode()).hexdigest()}
    wp = lambda pid, content: oprim.ActionPlan(          # noqa: E731 - 测试内快捷构造
        pid, [oprim.Action(f"{pid}_a", "write_file", {"path": "calc.py", "content": content})])
    plans = [wp("c_good", GOOD_CALC), wp("c_noop", CALC_BASE)]
    probes = [oprim.py_syntax_gate(["calc.py", "test_calc.py"]),
              oprim.FileFrozenProbe(frozen),
              oprim.unittest_probe(weight=3.0),
              oprim.DiffSizeProbe(baseline, weight=1.0)]
    store = oprim.SnapshotStore(str(tmp_path / "store"))

    with oprim.SandboxPool(size=2, base_dir=str(tmp_path / "p1"), isolation="netns") as pool:
        v1 = oprim.lookahead(plans, str(base_dir), store, pool, probes, min_reward=0.9)
    with oprim.SandboxPool(size=2, base_dir=str(tmp_path / "p2"), isolation="netns") as pool:
        v2 = oprim.lookahead(plans, str(base_dir), store, pool, probes, min_reward=0.9)
    assert v1.decision_id == v2.decision_id          # 决策可复现
    assert [r.plan_id for r in v1.ranked] == [r.plan_id for r in v2.ranked]
    assert [round(r.reward, 6) for r in v1.ranked] == [round(r.reward, 6) for r in v2.ranked]

    # 阈值过高 → 不输出 least-bad, 升级
    with oprim.SandboxPool(size=2, base_dir=str(tmp_path / "p3"), isolation="netns") as pool:
        v3 = oprim.lookahead(plans, str(base_dir), store, pool, probes, min_reward=0.999)
    assert v3.chosen is None
    assert any(e.reason_code == "LOW_CONFIDENCE" for e in v3.escalations)


# =========================================================================
# 五、PUCT 树搜索 (深度 > 1)
# =========================================================================

_TOY_LEAF = {"LLL": 0.10, "LLR": 0.20, "LRL": 0.30, "LRR": 0.40,
            "RLL": 0.50, "RLR": 0.60, "RRL": 0.70, "RRR": 1.00}


class ToyWorld:
    """三层二叉树。先验被刻意设成误导性的(偏向 L), 检验 PUCT 能否靠 Q 纠偏。"""

    def key(self, s):
        return s

    def actions(self, s):
        return [("L", 0.9), ("R", 0.1)] if len(s) < 3 else []

    def step(self, s, a):
        return s + a

    def terminal(self, s):
        return len(s) >= 3

    def reward(self, s):
        return _TOY_LEAF[(s + "LLL")[:3]]          # 默认策略: 补 L 到叶子


def test_puct_converges_despite_misleading_prior():
    m = ToyWorld()
    mc = oprim.MCTS(m, c_puct=1.4, max_depth=3, seed=0)
    best = mc.search("", budget=200)
    assert best == "R"                              # 误导性先验下仍找到最优首步
    assert oprim.best_path(mc, "") == ["R", "R", "R"]

    # 同 seed 可复现
    m2 = ToyWorld()
    mc2 = oprim.MCTS(m2, seed=0, max_depth=3)
    assert mc2.search("", budget=200) == best


def test_puct_budget_vs_c_known_property():
    """c_puct 越大, 纠偏误导性先验所需预算越多 (已知性质, 非 bug)。"""
    weak = oprim.MCTS(ToyWorld(), c_puct=1.0, max_depth=3, seed=0)
    weak.search("", budget=80)
    assert oprim.best_path(weak, "") == ["R", "R", "R"]

    tight = oprim.MCTS(ToyWorld(), c_puct=1.4, max_depth=3, seed=0)
    tight.search("", budget=80)
    assert oprim.best_path(tight, "") != ["R", "R", "R"]   # 预算不足未收敛


def test_mcts_transposition_table():
    class ToySet:
        def key(self, s):
            return frozenset(s)

        def actions(self, s):
            return [(x, 1 / 3) for x in "abc" if x not in s]

        def step(self, s, a):
            return s + a

        def terminal(self, s):
            return len(s) >= 3

        def reward(self, s):
            return len(set(s)) / 3

    ms = oprim.MCTS(ToySet(), max_depth=3, seed=0)
    ms.search("", budget=60)
    assert ms.stats.transposition_hits > 0          # ab 与 ba 复用同一节点
    assert len(ms.table) <= 8                       # 置换表压缩节点数
