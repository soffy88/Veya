#!/usr/bin/env python
"""P5-a 可机器验收脚本: 打包 / checkpoint / retry"""

import asyncio
import os
import pathlib
import sys
import warnings

warnings.filterwarnings("ignore")

sys.path.insert(0, "/home/soffy/projects/hicode")
os.chdir("/home/soffy/projects/hicode")

from config.loader import load_config

load_config()

passed, failed = [], []


def check(name, cond, detail=""):
    sym = "OK" if cond else "NG"
    (passed if cond else failed).append(name)
    print(f"  [{sym}] {name}" + (f"  ({detail})" if detail else ""), flush=True)


# ─────────────────────────────────────────────
# ① 打包
# ─────────────────────────────────────────────
print("=== ① 打包 ===")

VENV = pathlib.Path("/home/soffy/projects/hicode/.venv")
check("hicode script installed", (VENV / "bin" / "hicode").exists())
check("hicode-headless script installed", (VENV / "bin" / "hicode-headless").exists())

import subprocess

r = subprocess.run([str(VENV / "bin" / "hicode"), "--help"], capture_output=True, text=True)
check("hicode --help exits 0", r.returncode == 0, r.stderr[:60])
check("hicode --help shows persona", "--persona" in r.stdout)

r = subprocess.run(
    [str(VENV / "bin" / "hicode-headless"), "--help"], capture_output=True, text=True
)
check("hicode-headless --help exits 0", r.returncode == 0, r.stderr[:60])

# headless stdin mode
r = subprocess.run(
    [str(VENV / "bin" / "hicode-headless")],
    input='{"text": "echo hello_via_headless", "persona": "build"}',
    capture_output=True,
    text=True,
    timeout=120,
)
check(
    "hicode-headless stdin runs",
    r.returncode in (0, 1),
    f"rc={r.returncode} stderr={r.stderr[:80]}",
)
import json as _json

try:
    out = _json.loads(r.stdout)
    check("hicode-headless json output", "status" in out, str(out)[:80])
except Exception as e:
    check("hicode-headless json output", False, f"parse error: {e}; stdout={r.stdout[:80]}")

# ─────────────────────────────────────────────
# ② Checkpoint
# ─────────────────────────────────────────────
print("\n=== ② Checkpoint ===")

# T2-0: obase.versionstore available
from hicode.compat import jsonl_append, jsonl_latest

check("obase.versionstore: jsonl_append", callable(jsonl_append))
check("obase.versionstore: jsonl_latest", callable(jsonl_latest))

# oprim make/restore round-trip (pure functions, no IO)
from hicode.compat import RunState, make_checkpoint, restore_from_checkpoint

state_in = RunState(
    session_id="test-ckpt-001",
    step=2,
    data={"outputs": {"research": "found 3 files"}, "command": {"text": "refactor"}},
    completed_steps=["research", "plan"],
)
ckpt = make_checkpoint(state_in, session_id="test-ckpt-001")
state_out = restore_from_checkpoint(ckpt)

check("checkpoint: make/restore session_id", state_out.session_id == state_in.session_id)
check("checkpoint: make/restore step", state_out.step == state_in.step, f"{state_out.step}")
check(
    "checkpoint: make/restore completed_steps",
    state_out.completed_steps == state_in.completed_steps,
    str(state_out.completed_steps),
)
check("checkpoint: make/restore data", state_out.data == state_in.data, str(state_out.data))

# save_checkpoint + load_checkpoint (IO)
from server.checkpoint import ckpt_path, load_checkpoint, save_checkpoint

TEST_SID = "test-p5a-ckpt-io"
# Clean up any leftover
ckpt_path(TEST_SID).unlink(missing_ok=True)

asyncio.run(save_checkpoint(TEST_SID, state_in))
check("checkpoint: file on disk", ckpt_path(TEST_SID).exists(), str(ckpt_path(TEST_SID)))

loaded_ckpt = asyncio.run(load_checkpoint(TEST_SID))
check("checkpoint: load returns CheckpointData", loaded_ckpt is not None)
state_restored = restore_from_checkpoint(loaded_ckpt)
check(
    "checkpoint: loaded step matches",
    state_restored.step == state_in.step,
    str(state_restored.step),
)
check(
    "checkpoint: loaded completed_steps match",
    state_restored.completed_steps == state_in.completed_steps,
    str(state_restored.completed_steps),
)

# Multi-append: latest wins (simulate checkpoint after each squad)
state_step3 = RunState(
    session_id=TEST_SID,
    step=3,
    data={"outputs": {"execute": "done"}, "command": {}},
    completed_steps=["research", "plan", "execute"],
)
asyncio.run(save_checkpoint(TEST_SID, state_step3))
loaded2 = asyncio.run(load_checkpoint(TEST_SID))
state2 = restore_from_checkpoint(loaded2)
check("checkpoint: latest-wins (step=3)", state2.step == 3, str(state2.step))
check(
    "checkpoint: latest-wins completed=3",
    len(state2.completed_steps) == 3,
    str(state2.completed_steps),
)

# Clean up
ckpt_path(TEST_SID).unlink(missing_ok=True)

# Coordinator integration: verify checkpoint file written after handle()
from server.assembly import Infra

Infra.init(load_config())
from server.coordinator import coordinator

COORD_SID = "test-coordinator-ckpt"
ckpt_path(COORD_SID).unlink(missing_ok=True)

result = asyncio.run(coordinator.handle({"text": "echo coord_ckpt_test"}, session_id=COORD_SID))
check("coordinator: session_id in result", result.get("session_id") == COORD_SID)
check(
    "coordinator: checkpoint file written", ckpt_path(COORD_SID).exists(), str(ckpt_path(COORD_SID))
)

# Resume: verify resume skips completed step
loaded_ckpt2 = asyncio.run(load_checkpoint(COORD_SID))
if loaded_ckpt2:
    state_before = restore_from_checkpoint(loaded_ckpt2)
    step_before = state_before.step
    resume_result = asyncio.run(coordinator.resume(state_before))
    check(
        "coordinator: resume: resumed_from_step present",
        "resumed_from_step" in resume_result,
        str(resume_result.get("resumed_from_step")),
    )
    check(
        "coordinator: resume: status returned",
        resume_result.get("status") in ("success", "failed"),
        str(resume_result.get("status")),
    )
else:
    check("coordinator: resume", False, "no checkpoint loaded")

ckpt_path(COORD_SID).unlink(missing_ok=True)

# ─────────────────────────────────────────────
# ③ Retry
# ─────────────────────────────────────────────
print("\n=== ③ Retry ===")

from hicode.compat import retry_with_backoff

# Test 1: retryable exception → retries to success
calls = {"n": 0}


async def flaky():
    calls["n"] += 1
    if calls["n"] < 3:
        raise ConnectionError("transient")
    return "ok"


result_retry = asyncio.run(
    retry_with_backoff(
        flaky,
        max_attempts=3,
        base_delay=0.01,
    )
)
check("retry: flaky succeeds on 3rd attempt", result_retry == "ok", result_retry)
check("retry: call count = 3", calls["n"] == 3, str(calls["n"]))

# Test 2: non-retryable → immediate raise, no retry
calls2 = {"n": 0}


async def fatal():
    calls2["n"] += 1
    raise ValueError("not retryable")


raised_ok = False
try:
    asyncio.run(
        retry_with_backoff(
            fatal,
            max_attempts=3,
            base_delay=0.01,
            retryable=(ConnectionError,),
        )
    )
except ValueError:
    raised_ok = True
check("retry: non-retryable raises immediately", raised_ok)
check("retry: non-retryable call count = 1", calls2["n"] == 1, str(calls2["n"]))

# Test 3: exhausted attempts → raises last exception
calls3 = {"n": 0}


async def always_fails():
    calls3["n"] += 1
    raise ConnectionError("always fails")


exhausted_ok = False
try:
    asyncio.run(
        retry_with_backoff(
            always_fails,
            max_attempts=2,
            base_delay=0.01,
        )
    )
except ConnectionError:
    exhausted_ok = True
check("retry: exhausted raises last exc", exhausted_ok)
check("retry: exhausted call count = 2", calls3["n"] == 2, str(calls3["n"]))

# Test 4: retry is wired into assembly's llm_caller (import check)
import inspect

from server.assembly import _make_llm_caller

src = inspect.getsource(_make_llm_caller)
check("retry: wired in _make_llm_caller", "retry_with_backoff" in src)
check("retry: _RetryableHTTP defined", "_RetryableHTTP" in src)
check("retry: covers 429/5xx HTTP", "429" in src)

# ─────────────────────────────────────────────
print()
print("=" * 50)
print(f"P5-a: {len(passed)}/{len(passed) + len(failed)} PASS")
if failed:
    print(f"FAIL: {failed}")
    sys.exit(1)
