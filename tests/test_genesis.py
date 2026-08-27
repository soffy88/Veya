"""Veya Genesis 测试 — 永久记忆 / 3O 物理工具 / 独立 Agent 实体 / 守护进程。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.agents.architect_tools import ThreeOPhysicalTools, validate_3o_purity
from server.agents.genesis_agent import GenesisAgent
from server.agents.genesis_daemon import GenesisDaemon
from server.agents.genesis_memory import GenesisMemory


@pytest.fixture(autouse=True)
def _clean_genesis_env(monkeypatch):
    """隔离 GENESIS_* 环境变量(项目 .env 会被 config.loader 注入,污染默认值断言)。"""
    for var in ("GENESIS_API_KEY", "GENESIS_MODEL", "GENESIS_PROVIDER", "GENESIS_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# 1. 永久记忆系统 (Memory Bank)
# ---------------------------------------------------------------------------


def test_memory_ledger_persistence(tmp_path):
    """账本: 记录元素 → 新实例重载 → 无缝恢复;升级自动递增版本号。"""
    memory = GenesisMemory(storage_dir=tmp_path / "mem")
    assert memory.memory["element_ledger"] == {}
    assert memory.memory["experience_log"] == []

    memory.record_element("oskill", "factor/dual_ma.py", "双均线交叉算子 v1")
    assert memory.has_element("oskill", "factor/dual_ma.py")

    memory.record_element("oskill", "factor/dual_ma.py", "双均线交叉算子 v2 修复边界")
    entry = memory.get_element("oskill", "factor/dual_ma.py")
    assert entry["version"] == 2  # 升级自动版本号递增

    # 重启实例(模拟服务器重启) → 恢复上次智力状态
    revived = GenesisMemory(storage_dir=tmp_path / "mem")
    assert revived.has_element("oskill", "factor/dual_ma.py")
    assert revived.get_element("oskill", "factor/dual_ma.py")["version"] == 2
    assert revived.memory["last_active"] is not None


def test_memory_experience_jsonl_append(tmp_path):
    """经验: JSONL 追加式落盘,主文件落后也不丢。"""
    memory = GenesisMemory(storage_dir=tmp_path / "mem")
    memory.record_experience("在 oprim 层引入 os 模块", "纯数学原则: oprim/oskill 禁止 I/O 模块")
    memory.record_experience("沙箱 OOM", "大数组先估算内存")

    # JSONL 文件存在且两行
    jsonl = tmp_path / "mem" / "experiences.jsonl"
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 2

    # 手动清空主文件,JSONL 仍能恢复经验(崩溃不丢)
    (tmp_path / "mem" / "memory.json").write_text(
        json.dumps({"element_ledger": {}, "experience_log": [], "last_active": None}),
        encoding="utf-8",
    )
    revived = GenesisMemory(storage_dir=tmp_path / "mem")
    assert len(revived.memory["experience_log"]) == 2

    recent = revived.recent_experiences(1)
    assert "大数组先估算内存" in recent[0]["lesson"]  # 最近一条经验


def test_build_context_prompt_injects_subconscious(tmp_path):
    """潜意识注入: ledger + 最近经验压缩成 Prompt。"""
    memory = GenesisMemory(storage_dir=tmp_path / "mem")
    memory.record_element("oprim", "_macd.py", "计算 MACD 指标")
    for i in range(8):  # 灌 8 条经验,注入时只取最近 5 条
        memory.record_experience(f"mistake {i}", f"lesson {i}")

    prompt = memory.build_context_prompt()
    assert "[YOUR INTERNAL MEMORY (DO NOT HALLUCINATE)]" in prompt
    assert "oprim/_macd.py" in prompt
    assert "计算 MACD 指标" in prompt
    assert "lesson 7" in prompt  # 最近一条在
    assert "mistake 0" not in prompt  # 最早一条被截断(防 Token 爆炸)


def test_memory_search_elements(tmp_path):
    memory = GenesisMemory(storage_dir=tmp_path / "mem")
    memory.record_element("oskill", "indicator/ema.py", "指数移动平均")
    memory.record_element("oskill", "factor/ic.py", "IC 因子")

    hits = memory.search_elements("均线")
    assert len(hits) == 0  # "均线" 不在 ema 的描述里 → 需要精确匹配
    hits = memory.search_elements("ema")
    assert hits[0]["path"] == "oskill/indicator/ema.py"
    assert memory.get_layer_summary("oskill") == ["oskill/factor/ic.py", "oskill/indicator/ema.py"]


# ---------------------------------------------------------------------------
# 2. 3O 物理工具 (ThreeOPhysicalTools)
# ---------------------------------------------------------------------------


@pytest.fixture
def three_o_root(tmp_path) -> Path:
    """模拟 3O 主库: 5 层目录。"""
    root = tmp_path / "3O"
    for layer in ("oprim", "oskill", "omodul", "obase", "oservi"):
        (root / layer).mkdir(parents=True)
    return root


def test_validate_3o_purity():
    ok, _ = validate_3o_purity("oprim", "import numpy as np\ndef f(x):\n    return np.abs(x)\n")
    assert ok

    ok, reason = validate_3o_purity("oprim", "import os\nx = os.getcwd()\n")
    assert not ok and "纯度" in reason

    ok, reason = validate_3o_purity("oprim", "from pathlib import Path\n")
    assert not ok and "pathlib" in reason

    ok, reason = validate_3o_purity("oprim", "def f():\n    return open('x').read()\n")
    assert not ok and "open" in reason

    # obase 是 I/O 层,不受纯度约束
    ok, _ = validate_3o_purity("obase", "import os\nx = os.getcwd()\n")
    assert ok


def test_forge_element_writes_file(three_o_root):
    tools = ThreeOPhysicalTools(three_o_root)
    result = tools.forge_element(
        "oskill",
        "indicator/dual_ma.py",
        "import numpy as np\n\ndef dual_ma(prices, fast=5, slow=20):\n"
        "    fast_ma = np.convolve(prices, np.ones(fast)/fast, mode='valid')\n"
        "    slow_ma = np.convolve(prices, np.ones(slow)/slow, mode='valid')\n"
        "    return fast_ma, slow_ma\n",
    )
    assert "成功" in result and "dual_ma.py" in result
    target = three_o_root / "oskill" / "indicator" / "dual_ma.py"
    assert target.exists() and "def dual_ma" in target.read_text(encoding="utf-8")


def test_forge_element_blocks_impure_import(three_o_root):
    """oprim 层引入 os → 锻造被拒(失败文本,模型可读原因自纠)。"""
    tools = ThreeOPhysicalTools(three_o_root)
    result = tools.forge_element("oprim", "_io_leak.py", "import os\nVALUE = os.getcwd()\n")
    assert result.startswith("失败")
    assert "纯度" in result
    assert not (three_o_root / "oprim" / "_io_leak.py").exists()

    # 显式 allow_impure=True 放行(Genesis 需自证理由)
    result = tools.forge_element(
        "oprim", "_io_leak.py", "import os\nVALUE = os.getcwd()\n", allow_impure=True
    )
    assert "成功" in result


def test_forge_path_escape_rejected(three_o_root):
    tools = ThreeOPhysicalTools(three_o_root)
    with pytest.raises(RuntimeError, match="escapes layer root"):
        tools.forge_element("oskill", "../evil.py", "x = 1")
    with pytest.raises(RuntimeError, match="unknown 3O layer"):
        tools.forge_element("frontend", "app.js", "x = 1")


def test_search_library_and_list_layer(three_o_root):
    tools = ThreeOPhysicalTools(three_o_root)
    tools.forge_element("oskill", "factor/ic.py", "def compute_ic():\n    return 0.0\n")

    hits = tools.search_library("compute_ic")
    assert "factor/ic.py" in hits

    listing = tools.list_layer("oskill")
    assert "factor/ic.py" in listing


@pytest.mark.asyncio
async def test_run_in_sandbox_real_execution(three_o_root):
    tools = ThreeOPhysicalTools(three_o_root)
    out = await tools.run_in_sandbox("print(6 * 7)")
    assert "exit_code=0" in out
    assert "42" in out

    with pytest.raises(RuntimeError, match="TypeError"):
        await tools.run_in_sandbox("raise TypeError('bad math')")


# ---------------------------------------------------------------------------
# 3. 独立 Agent 实体 (GenesisAgent)
# ---------------------------------------------------------------------------


def _tool_response(name: str, args: dict, content: str = "", tc_id: str = "call_1") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _text_response(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {}}


def test_agent_requires_dedicated_key():
    """独立身份: 必须提供专属 Key(与主业务物理隔离)。"""
    with pytest.raises(ValueError, match="dedicated"):
        GenesisAgent(dedicated_api_key=None)  # 无 key 且无 llm_fn → 拒绝启动


def test_agent_identity_prompt_boundaries():
    """人格: 独立于 Veya 主系统,只效忠 3O;拒绝非 3O 任务。"""
    agent = GenesisAgent(dedicated_api_key="sk-test", llm_fn=lambda *a, **k: None)
    prompt = agent._get_identity_prompt()
    assert "You are 'Genesis'" in prompt
    assert "independent of the main Veya system" in prompt
    assert "REJECT THEM" in prompt  # 前端/业务逻辑 → 拒绝
    assert "Pure Math/Logic ONLY" in prompt


def test_agent_isolated_key_config():
    """专属 Key 只存在于实例 config,不碰环境变量。"""
    agent = GenesisAgent(dedicated_api_key="sk-genesis-42", llm_fn=lambda *a, **k: None)
    assert agent._llm_config == {"providers": {"openai": {"api_key": "sk-genesis-42"}}}
    assert agent.temperature == 0.0
    assert agent.model == "gpt-4o"


@pytest.mark.asyncio
async def test_agent_mission_success_records_ledger(three_o_root, tmp_path):
    """任务成功: forge 成功 → 永久记入账本;潜意识注入 system prompt。"""
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        # 断言专属配置被传递
        assert kwargs["config"] == {"providers": {"openai": {"api_key": "sk-genesis"}}}
        assert kwargs["temperature"] == 0.0
        turn = len(calls)
        if turn == 1:
            return _tool_response(
                "forge_element",
                {
                    "layer": "oskill",
                    "element_name": "indicator/dual_ma.py",
                    "code": "import numpy as np\n\ndef dual_ma(prices, fast=5, slow=20):\n    return prices, prices\n",
                },
            )
        return _text_response("已锻造并通过 3O 范式检查: oskill/indicator/dual_ma.py")

    memory = GenesisMemory(storage_dir=tmp_path / "mem")
    agent = GenesisAgent(
        dedicated_api_key="sk-genesis",
        library_root=three_o_root,
        memory=memory,
        llm_fn=fake_llm,
        max_steps=3,
    )
    result = await agent.handle_mission("帮我写一个双均线交叉算子")

    assert result["status"] == "success"
    # 潜意识注入: 第一轮 system prompt 同时含人格 + 记忆
    assert "You are 'Genesis'" in calls[0][0]["content"]
    assert "[YOUR INTERNAL MEMORY" in calls[0][0]["content"]
    # 锻造成功 → 永久账本
    assert memory.has_element("oskill", "indicator/dual_ma.py")
    entry = memory.get_element("oskill", "indicator/dual_ma.py")
    assert entry["version"] == 1
    # 文件真实落库
    assert (three_o_root / "oskill" / "indicator" / "dual_ma.py").exists()


@pytest.mark.asyncio
async def test_agent_failure_records_experience(three_o_root, tmp_path):
    """任务失败: 沙箱报错 → 记入永久经验,CRITICAL FAILURE 回喂自纠。"""
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        turn = len(calls)
        if turn == 1:
            return _tool_response("run_in_sandbox", {"code": "raise TypeError('bad')"})
        return _text_response("已修正")

    memory = GenesisMemory(storage_dir=tmp_path / "mem")
    tools = ThreeOPhysicalTools(three_o_root)

    async def broken_sandbox(code):
        raise RuntimeError("exit_code=1 stderr: TypeError: bad")

    tools.run_in_sandbox = broken_sandbox  # type: ignore[method-assign]
    agent = GenesisAgent(
        dedicated_api_key="sk-genesis",
        library_root=three_o_root,
        memory=memory,
        physical_tools=tools,
        llm_fn=fake_llm,
        max_steps=3,
    )
    result = await agent.handle_mission("测试沙箱")

    assert result["status"] == "success"
    # 惨痛教训已入库
    lessons = memory.recent_experiences(1)
    assert "run_in_sandbox" in lessons[0]["mistake"]
    assert "TypeError" in lessons[0]["lesson"]
    # 失败被回喂给模型自纠
    assert "CRITICAL FAILURE" in calls[1][-1]["content"]


@pytest.mark.asyncio
async def test_agent_max_steps_abort(three_o_root, tmp_path):
    """超过最大认知步数 → Mission abort。"""

    async def looping_llm(messages, **kwargs):
        return _tool_response("search_library", {"query": "x"})

    agent = GenesisAgent(
        dedicated_api_key="sk-genesis",
        library_root=three_o_root,
        memory=GenesisMemory(storage_dir=tmp_path / "mem"),
        llm_fn=looping_llm,
        max_steps=2,
    )
    result = await agent.handle_mission("无限循环任务")
    assert result["status"] == "failed"
    assert "Max cognitive steps" in result["error"]
    assert result["steps"] == 2


def test_agent_wake_up_and_status(three_o_root, tmp_path):
    """生命周期: 唤醒报告记忆状态。"""
    memory = GenesisMemory(storage_dir=tmp_path / "mem")
    memory.record_element("obase", "storage.py", "存储层")
    agent = GenesisAgent(
        dedicated_api_key="sk-genesis",
        library_root=three_o_root,
        memory=memory,
        llm_fn=lambda *a, **k: None,
    )
    state = agent.wake_up()
    assert state["elements_managed"] == 1
    assert state["model"] == "gpt-4o"
    assert agent.status()["lessons_learned"] == 0


# ---------------------------------------------------------------------------
# 4. 守护进程 (Daemon Mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_run_once(three_o_root, tmp_path):
    """inbox 任务 → 处理 → 结果落盘 → 任务移入 done。"""
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return _tool_response(
                "forge_element",
                {"layer": "obase", "element_name": "io/queue.py", "code": "QUEUE = []\n"},
            )
        return _text_response("done")

    memory = GenesisMemory(storage_dir=tmp_path / "mem")
    agent = GenesisAgent(
        dedicated_api_key="sk-genesis",
        library_root=three_o_root,
        memory=memory,
        llm_fn=fake_llm,
        max_steps=3,
    )
    daemon = GenesisDaemon(agent, work_dir=tmp_path / "work")

    # 投递一条架构级指令
    (daemon.inbox_dir / "task1.json").write_text(
        json.dumps({"mission": "在 obase 层锻造一个队列元素", "session_id": "m1"}), encoding="utf-8"
    )

    n = await daemon.run_once()
    assert n == 1
    # 结果落盘
    result = json.loads((daemon.results_dir / "m1.json").read_text(encoding="utf-8"))
    assert result["status"] == "success"
    assert result["session_id"] == "m1"
    # 任务移入 done
    assert (daemon.done_dir / "task1.json").exists()
    assert not (daemon.inbox_dir / "task1.json").exists()
    # 记忆成长
    assert memory.has_element("obase", "io/queue.py")
    # 再次 run_once → 无新任务
    assert await daemon.run_once() == 0


def test_daemon_cli_requires_mode():
    """CLI 必须指定 --daemon 或 --one-shot。"""
    from server.agents.genesis_daemon import main

    with pytest.raises(SystemExit):
        main(["--library-root", "."])


# ---------------------------------------------------------------------------
# 5. NVIDIA NIM 专属配置 (独立身份落地)
# ---------------------------------------------------------------------------


def test_agent_nim_config_from_env(three_o_root, tmp_path, monkeypatch):
    """NIM 专属配置: key/model/provider/endpoint 全部从 env 读取并注入 config。"""
    monkeypatch.setenv("GENESIS_API_KEY", "nvapi-test-key-123")
    monkeypatch.setenv("GENESIS_MODEL", "nvidia/llama-3.3-nemotron-super-49b-v1.5")
    monkeypatch.setenv("GENESIS_PROVIDER", "openai")
    monkeypatch.setenv("GENESIS_ENDPOINT", "https://integrate.api.nvidia.com/v1/chat/completions")

    agent = GenesisAgent(llm_fn=lambda *a, **k: None)  # 无显式参数 → 全部来自 env
    assert agent.api_key == "nvapi-test-key-123"
    assert agent.model == "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    assert agent.provider == "openai"
    assert agent.endpoint == "https://integrate.api.nvidia.com/v1/chat/completions"
    # 物理隔离: key 只存在于实例私有 config
    assert agent._llm_config["providers"] == {"openai": {"api_key": "nvapi-test-key-123"}}
    assert agent._llm_config["endpoints"] == {
        "openai": "https://integrate.api.nvidia.com/v1/chat/completions"
    }


@pytest.mark.asyncio
async def test_agent_llm_failure_does_not_crash(three_o_root, tmp_path):
    """LLM 网络/鉴权失败 → mission 返回 failed,实体不崩溃。"""

    async def exploding_llm(messages, **kwargs):
        raise RuntimeError("401 Unauthorized: invalid NIM key")

    agent = GenesisAgent(
        dedicated_api_key="nvapi-bad",
        library_root=three_o_root,
        memory=GenesisMemory(storage_dir=tmp_path / "mem"),
        llm_fn=exploding_llm,
    )
    result = await agent.handle_mission("test")
    assert result["status"] == "failed"
    assert "401 Unauthorized" in result["error"]
