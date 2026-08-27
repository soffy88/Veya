"""Task-scoped artifact layout and manifest handling."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .models import ArtifactManifest, ArtifactRef

_RUN_PARTS = ("inputs", "workspace", "outputs", "evidence", "checkpoints", "trajectories")


class ArtifactStore:
    """Owns ``.veya/runs/<task_id>`` without deciding semantic completion."""

    def __init__(self, project_root: str | Path, task_id: str):
        self.project_root = Path(project_root).expanduser().resolve()
        if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
            raise ValueError("task_id must be a single safe path component")
        self.task_id = task_id
        self.run_root = self.project_root / ".veya" / "runs" / task_id
        self._artifacts: list[ArtifactRef] = []

    def ensure_layout(self) -> Path:
        self.run_root.mkdir(parents=True, exist_ok=True)
        for part in _RUN_PARTS:
            (self.run_root / part).mkdir(exist_ok=True)
        return self.run_root

    def path(self, relative: str | Path) -> Path:
        self.ensure_layout()
        candidate = (self.run_root / relative).resolve()
        if candidate != self.run_root and self.run_root not in candidate.parents:
            raise ValueError("artifact path escapes task run")
        return candidate

    def register(
        self,
        relative: str | Path,
        *,
        kind: str = "file",
        producer: str = "runtime",
        status: str = "draft",
        evidence_ids: list[str] | None = None,
    ) -> ArtifactRef:
        target = self.path(relative)
        if not target.exists():
            raise FileNotFoundError(target)
        digest = None
        size = None
        if target.is_file():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            size = target.stat().st_size
        ref = ArtifactRef(
            path=str(target.relative_to(self.run_root)),
            kind=kind,
            producer=producer,
            status=status,
            sha256=digest,
            size_bytes=size,
            evidence_ids=list(evidence_ids or []),
        )
        self._artifacts.append(ref)
        return ref

    def publish(
        self,
        source: str | Path,
        output_name: str,
        *,
        producer: str = "runtime",
        status: str = "draft",
    ) -> ArtifactRef:
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(source_path)
        target = self.path(Path("outputs") / output_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if target.exists():
            if (
                not target.is_file()
                or hashlib.sha256(target.read_bytes()).hexdigest() != source_hash
            ):
                raise FileExistsError(
                    f"immutable output already exists with different content: {target}"
                )
            return self.register(Path("outputs") / output_name, producer=producer, status=status)
        temporary = target.with_name(f".{target.name}.{source_hash}.tmp")
        shutil.copy2(source_path, temporary)
        temporary.replace(target)
        return self.register(Path("outputs") / output_name, producer=producer, status=status)

    def manifest(self) -> ArtifactManifest:
        return ArtifactManifest(task_id=self.task_id, artifacts=list(self._artifacts))

    def record(self, ref: ArtifactRef) -> ArtifactRef:
        """Record an executor-produced reference without copying its file."""
        if not any(
            existing.path == ref.path and existing.sha256 == ref.sha256
            for existing in self._artifacts
        ):
            self._artifacts.append(ref)
        return ref

    def write_manifest(self) -> Path:
        self.ensure_layout()
        path = self.run_root / "artifact_manifest.json"
        encoded = json.dumps(self.manifest().to_dict(), ensure_ascii=False, indent=2)
        if path.exists():
            if path.read_text(encoding="utf-8") != encoded:
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                path = self.run_root / f"artifact_manifest.{digest}.json"
                if path.exists():
                    return path
            else:
                return path
        temporary = path.with_name(
            f".{path.name}.{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}.tmp"
        )
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)
        return path

    @staticmethod
    def is_final(ref: ArtifactRef) -> bool:
        return ref.path.startswith("outputs/") or ref.path == "outputs"
