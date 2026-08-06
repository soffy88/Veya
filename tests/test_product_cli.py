"""产品化 CLI 门禁 — veya init / start / doctor / 本地模型免 Key。

覆盖:
  - 子命令路由 (init/start/doctor 与旧 flag 兼容共存)
  - veya init 非交互模式: 配置落盘 (~/.veya/config.json 结构对齐 loader)
  - veya doctor 自检: 无配置时给出引导而非报错
  - Ollama 本地 endpoint 免 API Key (llm_call 不再误走 stub)
"""

from __future__ import annotations

import json
import os

import pytest

from cli.main import main
from cli.product import PROVIDERS, run_doctor, run_init

# ---------------------------------------------------------------------------
# 子命令路由
# ---------------------------------------------------------------------------

def test_cli_routes_product_subcommands(monkeypatch, capsys):
    """veya init / start / doctor 被分派到 product 模块, 旧 flag 不受影响。"""
    import cli.product as product

    called: list[str] = []
    monkeypatch.setattr(product, "run_init", lambda argv: called.append("init") or 0)
    monkeypatch.setattr(product, "run_start", lambda argv: called.append("start") or 0)
    monkeypatch.setattr(product, "run_doctor", lambda argv: called.append("doctor") or 0)

    assert main(["init"]) == 0
    assert main(["doctor"]) == 0
    assert called == ["init", "doctor"]

    # 旧行为: --version 仍可用且版本已升 (argparse 打印到 stdout 后 exit 0)
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "0.6.0" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# veya init (非交互)
# ---------------------------------------------------------------------------

def test_init_noninteractive_writes_config(monkeypatch, tmp_path):
    """--yes 非交互: 生成 ~/.veya/config.json, 结构对齐 config.loader 消费路径。"""
    home = tmp_path / "home"
    monkeypatch.setattr("cli.product._HOME_DIR", home)
    monkeypatch.setattr("cli.product._CONFIG_PATH", home / "config.json")
    monkeypatch.setattr("cli.product._ENV_PATH", home / ".env")

    ws = tmp_path / "work"
    ws.mkdir()

    rc = run_init(["--provider", "openai", "--key", "sk-test-123",
                   "--workspace", str(ws), "--yes"])
    assert rc == 0

    cfg = json.loads((home / "config.json").read_text())
    assert cfg["llm"]["provider"] == "openai"
    assert cfg["llm"]["model"] == "gpt-4o-mini"
    assert cfg["providers"]["openai"]["api_key"] == "sk-test-123"
    assert cfg["workspace"] == str(ws)
    assert cfg["persona"] == "build"

    # Key 落盘 (home + 工作区 .env)
    assert (home / ".env").read_text().startswith("OPENAI_API_KEY=sk-test-123")
    assert (ws / ".env").read_text().startswith("OPENAI_API_KEY=sk-test-123")

    # 工作区安全策略生成, allowed_paths 绑定工作区
    sec = (ws / ".veya" / "security.yaml").read_text()
    assert str(ws) in sec
    assert "restricted_permissions" in sec


def test_init_skips_key_for_ollama(monkeypatch, tmp_path, capsys):
    """ollama 提供商: 不要求 Key, 配置可写入。"""
    home = tmp_path / "home"
    monkeypatch.setattr("cli.product._HOME_DIR", home)
    monkeypatch.setattr("cli.product._CONFIG_PATH", home / "config.json")
    monkeypatch.setattr("cli.product._ENV_PATH", home / ".env")
    monkeypatch.setattr("cli.product.probe_ollama", lambda: ["qwen2.5:7b"])

    ws = tmp_path / "work"
    ws.mkdir()
    rc = run_init(["--provider", "ollama", "--workspace", str(ws), "--yes"])
    assert rc == 0
    cfg = json.loads((home / "config.json").read_text())
    assert cfg["llm"]["provider"] == "ollama"
    assert "providers" not in cfg or not cfg["providers"]
    assert "检测到本地 Ollama" in capsys.readouterr().out


def test_init_rejects_missing_workspace(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    monkeypatch.setattr("cli.product._HOME_DIR", home)
    monkeypatch.setattr("cli.product._CONFIG_PATH", home / "config.json")
    rc = run_init(["--provider", "openai", "--workspace", str(tmp_path / "nope"), "--yes"])
    assert rc == 1


# ---------------------------------------------------------------------------
# veya doctor
# ---------------------------------------------------------------------------

def test_doctor_reports_missing_setup(monkeypatch, tmp_path, capsys):
    """无任何配置时: doctor 输出引导, 返回非零但不抛异常。"""
    monkeypatch.setattr("cli.product._HOME_DIR", tmp_path / "home")
    monkeypatch.setattr("cli.product._CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("cli.product.probe_ollama", lambda: None)

    rc = run_doctor([])
    out = capsys.readouterr().out
    assert "veya doctor" in out
    assert "veya init" in out          # 引导而非报错堆栈
    assert rc == 1

    # JSON 模式可脚本化
    run_doctor(["--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert any(c["name"] == "模型接入" for c in data["checks"])


def test_doctor_all_green(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    monkeypatch.setattr("cli.product._HOME_DIR", home)
    monkeypatch.setattr("cli.product._CONFIG_PATH", home / "config.json")
    monkeypatch.setattr("cli.product._ENV_PATH", home / ".env")
    monkeypatch.setattr("cli.product.probe_ollama", lambda: None)
    monkeypatch.setattr("cli.product._port_in_use", lambda port: False)

    ws = tmp_path / "work"
    ws.mkdir()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({
        "llm": {"provider": "deepseek", "model": "deepseek-chat"},
        "providers": {"deepseek": {"api_key": "sk-x"}},
        "workspace": str(ws),
    }))
    rc = run_doctor(["--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert rc == 0


# ---------------------------------------------------------------------------
# Ollama 本地 endpoint 免 Key
# ---------------------------------------------------------------------------

def test_llm_local_endpoint_skips_key_check(monkeypatch):
    """VEYA_LLM_ENDPOINT 指向 localhost 时, 无 Key 不降级 stub。"""
    from veya.llm import llm_call

    async def _fake_provider_call(client, provider, **kw):
        assert kw["api_key"] == ""       # 本地模型无 key 也放行
        assert kw["endpoint"].startswith("http://localhost")
        return {"choices": [{"message": {"content": "local-ok"}}], "usage": {}}

    monkeypatch.setattr("veya.llm.provider_call", _fake_provider_call)
    monkeypatch.setattr("os.environ", {**os.environ, "VEYA_LLM_ENDPOINT": "http://localhost:11434/v1/chat/completions"})

    import asyncio

    result = asyncio.run(llm_call(
        [{"role": "user", "content": "hi"}],
        provider="openai", model="qwen2.5:7b",
    ))
    content = result["choices"][0]["message"]["content"]
    assert content == "local-ok"
    assert "shim" not in content          # 未走 stub


def test_llm_remote_without_key_still_stubs():
    """远端 provider 无 Key 仍走 stub (行为不变)。"""
    import asyncio

    from veya.llm import llm_call

    result = asyncio.run(llm_call(
        [{"role": "user", "content": "hi"}],
        provider="openai", model="gpt-4o-mini",
    ))
    assert "shim" in result["choices"][0]["message"]["content"]


def test_providers_catalog_complete():
    assert set(PROVIDERS) == {"openai", "anthropic", "dashscope", "deepseek", "ollama"}
    assert PROVIDERS["ollama"]["env"] == ""     # 本地模型无 key env


# ---------------------------------------------------------------------------
# veya start 端口自动避让
# ---------------------------------------------------------------------------

def test_find_free_port_avoids_busy(monkeypatch):
    """8765 被外部服务占用时, start 自动避让到下一个空闲端口。"""
    from cli.product import _find_free_port

    busy = {8765, 8766, 8767}
    monkeypatch.setattr("cli.product._port_in_use", lambda port: port in busy)
    assert _find_free_port(8765) == 8768
    assert _find_free_port(9000) == 9000   # 空闲直接使用
    # 全部被占 → 回退起始端口 (uvicorn 会给出 bind 错误, 但逻辑不崩)
    monkeypatch.setattr("cli.product._port_in_use", lambda port: True)
    assert _find_free_port(8765) == 8765
