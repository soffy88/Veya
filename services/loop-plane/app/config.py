"""loop-plane 配置（SPEC §10）。

环境变量:
    LOOP_PLANE_PORT   8787        服务端口
    LOOP_DATA_DIR     ~/.veya/loop 数据根（events/audit/graphs/skills/exports）
    LOOP_WORKSPACE    cwd         sandbox 根
    VEYA_LOOP_OPTIONAL true       无 veya-loop 库时 causal 降级
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    port: int = 8787
    data_dir: Path = field(default_factory=lambda: Path.home() / ".veya" / "loop")
    workspace: Path = field(default_factory=Path.cwd)
    veya_loop_optional: bool = True
    default_tenant: str = "default"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("LOOP_DATA_DIR") or (Path.home() / ".veya" / "loop"))
        return cls(
            port=int(os.environ.get("LOOP_PLANE_PORT", "8787")),
            data_dir=data_dir,
            workspace=Path(os.environ.get("LOOP_WORKSPACE") or Path.cwd()),
            veya_loop_optional=os.environ.get("VEYA_LOOP_OPTIONAL", "true").lower() != "false",
        )

    def ensure_dirs(self) -> None:
        for sub in ("", "graphs", "skills", "exports/plans"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)


settings = Settings.from_env()
