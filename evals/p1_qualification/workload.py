"""Real workload generator for the P1 qualification harness.

Every cycle performs deterministic, verifiable work:

- SHA-256 hash chains over a 1 KiB block (CPU).
- Deterministic integer transform over a fixed vector (CPU).
- Artifact write + read-back + hash verification inside the run dir (IO).
- Sorted-merge over a deterministic pseudo-random sequence (CPU).

No ``time.sleep`` anywhere: wall-clock duration is a product of executed
cycles, and the orchestrator runs cycles until the elapsed time reaches the
configured target.  The per-cycle cost self-calibrates at startup so a cycle
lands in the 0.25-0.5 s band on any machine.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorkCycleEvidence:
    cycle: int
    input_hash: str
    output_hash: str
    artifact_relpath: str
    artifact_sha256: str
    duration_s: float


@dataclass
class RealWorkload:
    """Deterministic real-work source.  No sleeps, no network, no RNG."""

    run_dir: Path
    hash_rounds: int = 4000
    vector_len: int = 4000
    _cycle: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.artifacts_dir = Path(self.run_dir) / "work_artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def calibrate(self, target_cycle_s: float = 0.35) -> dict:
        """Scale hash_rounds so one cycle costs ~target_cycle_s seconds."""
        probe = self.cycle()
        measured = max(probe.duration_s, 1e-6)
        scale = target_cycle_s / measured
        # Clamp to sane bounds; keep deterministic by rounding.
        scale = min(8.0, max(0.25, scale))
        self.hash_rounds = max(500, int(self.hash_rounds * scale))
        self.vector_len = max(1000, int(self.vector_len * min(2.0, max(0.5, scale))))
        return {
            "probe_s": round(measured, 4),
            "scale": round(scale, 3),
            "hash_rounds": self.hash_rounds,
            "vector_len": self.vector_len,
        }

    def _block(self, cycle: int) -> bytes:
        seed = f"p1q-cycle-{cycle}".encode()
        return (seed * ((1024 // len(seed)) + 1))[:1024]

    def cycle(self) -> WorkCycleEvidence:
        started = time.perf_counter()
        self._cycle += 1
        cycle = self._cycle

        # 1. CPU: hash chain.
        digest = hashlib.sha256(self._block(cycle)).digest()
        for _ in range(self.hash_rounds):
            digest = hashlib.sha256(digest + self._block(cycle)).digest()
        input_hash = digest.hex()

        # 2. CPU: deterministic integer transform (no RNG).
        vec = [(i * 2654435761 + cycle * 97) % 1000003 for i in range(self.vector_len)]
        acc = 0
        for i, v in enumerate(vec):
            acc = (acc + (v ^ (i * 31)) * (i + 1)) % (2**63 - 1)
        merged = sorted(vec[: self.vector_len // 4])
        output_pre = f"{input_hash}:{acc}:{merged[0]}:{merged[-1]}:{len(merged)}"
        output_hash = hashlib.sha256(output_pre.encode()).hexdigest()

        # 3. IO: artifact write + read-back + verify.
        relpath = f"work_artifacts/cycle-{cycle:06d}.txt"
        artifact_path = Path(self.run_dir) / relpath
        payload = f"cycle={cycle}\ninput={input_hash}\noutput={output_hash}\nacc={acc}\n"
        artifact_path.write_text(payload, encoding="utf-8")
        read_back = artifact_path.read_text(encoding="utf-8")
        if read_back != payload:
            raise RuntimeError(f"artifact read-back mismatch at cycle {cycle}")
        artifact_sha256 = hashlib.sha256(read_back.encode()).hexdigest()

        duration_s = time.perf_counter() - started
        return WorkCycleEvidence(
            cycle=cycle,
            input_hash=input_hash,
            output_hash=output_hash,
            artifact_relpath=relpath,
            artifact_sha256=artifact_sha256,
            duration_s=duration_s,
        )

    @property
    def cycles(self) -> int:
        return self._cycle
