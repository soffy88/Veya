import asyncio
import shutil
import subprocess
from pathlib import Path

from tests.remote.test_l1_parallel import call_tool, initialize, make_gateway, status, wait_phase


async def main():
    tmp_path = Path("/data/soffy/tmp/test_10b")
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    tmp_path.mkdir(parents=True)

    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "base.txt").write_text("VEYA_READ_OK\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)

    gateway, secret = make_gateway(repo_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {
                    "worker": "opencode",
                    "task": (
                        "Read base.txt and report its contents."
                        " Do not write code."
                        " Do not modify any file."
                    ),
                    "task_kind": "READ",
                    "effect_requirement": "READ_ONLY",
                    "verification_requirement": "NONE",
                    "commit_requirement": "NONE",
                    "promotion_policy": "NONE",
                    "allowed_files": ["base.txt"],
                    "allowed_roots": [str(repo_path)],
                }
            ]
        },
    )
    parent_id = envelope["execution_id"]
    child_id = envelope["result"]["child_execution_ids"][0]
    await wait_phase(gateway, secret, session, parent_id, {"COMPLETED", "FAILED"}, timeout=180)
    child_st = await status(gateway, secret, session, child_id)
    print("STDOUT:", child_st.get("stdout_tail"))
    print("STDERR:", child_st.get("stderr_tail"))


asyncio.run(main())
