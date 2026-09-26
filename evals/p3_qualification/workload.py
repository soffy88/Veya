"""Deterministic real CPU/IO work for P3 qualification.

No clock padding is used. Duration comes from hashing, transforms, artifact
write/read-back, and the canonical gateway execution performed by the runner.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkResult:
    cycle: int
    artifact: str
    digest: str


class RealP3Workload:
    def __init__(self, root: str | Path, *, hash_rounds: int = 5000) -> None:
        self.root = Path(root)
        self.artifacts = self.root / "p3-artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.hash_rounds = hash_rounds
        self.cycle_count = 0

    def cycle(self, owner: str) -> WorkResult:
        self.cycle_count += 1
        cycle = self.cycle_count
        block = f"p3:{owner}:{cycle}".encode() * 64
        digest = hashlib.sha256(block).digest()
        for _ in range(self.hash_rounds):
            digest = hashlib.sha256(digest + block).digest()
        values = [(index * 2654435761 + cycle) % 1000003 for index in range(2000)]
        values.sort()
        digest = hashlib.sha256(f"{digest.hex()}:{values[0]}:{values[-1]}".encode()).digest()
        rel = Path("p3-artifacts") / f"{owner}-{cycle:07d}.json"
        path = self.root / rel
        payload = f"owner={owner}\ncycle={cycle}\ndigest={digest.hex()}\n"
        path.write_text(payload, encoding="utf-8")
        if path.read_text(encoding="utf-8") != payload:
            raise RuntimeError("P3 artifact read-back mismatch")
        return WorkResult(cycle=cycle, artifact=str(rel), digest=digest.hex())
