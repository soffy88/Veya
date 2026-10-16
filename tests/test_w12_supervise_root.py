import asyncio
from types import SimpleNamespace

from server.bot_team import turns


def test_supervise_without_workspace_uses_write_root(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WRITE_ROOT", str(tmp_path))
    monkeypatch.setattr("server.supervisor.enabled", lambda: True)
    seen = {}

    def fake_run(**kw):
        seen.update(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr("server.supervision_tools.supervise_task", fake_run)

    class _Store:
        def append(self, *a, **k):
            pass

    bot = SimpleNamespace(bot_id="b1")
    asyncio.run(
        turns._supervise_from_phrase(
            bot=bot,
            text="每 5 分钟告诉我进度：统计 README.md 有多少行",
            user={},
            workspace_id=None,
            conversations=_Store(),
        )
    )
    assert seen["project_root"] == str(tmp_path / "supervised" / "b1")
    assert (tmp_path / "supervised" / "b1").is_dir()
