"""
veya/omodul/vision_agent.py — Vision Processing Module (Layer 3).

End-to-end vision agent module built on oskill + oprim.
Processes images/video → vision analysis → structured response.

Supports single image, image sequence, and video frame sampling.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from veya.oprim.types import ImageFrame, VisionResult
from veya.oprim.video import (
    create_image_frame,
    load_image_frame,
    sample_video_frames,
    validate_image,
)
from veya.oskill.vision import analyze_image, analyze_images


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class VisionAgentState(StrEnum):
    """Vision agent states."""

    IDLE = "idle"
    LOADING = "loading"
    ANALYZING = "analyzing"
    SUMMARIZING = "summarizing"
    DONE = "done"
    ERROR = "error"


@dataclass
class VisionSessionConfig:
    """Configuration for a vision agent session."""

    provider: str = "openai"
    model: str | None = None
    max_images: int = 20
    max_video_frames: int = 30
    video_frame_interval_sec: float = 1.0
    detail_level: str = "auto"  # auto, low, high
    timeout: float = 120.0


@dataclass
class VisionSessionResult:
    """Result of a vision agent session."""

    description: str = ""
    objects: list[str] = field(default_factory=list)
    text_in_images: str = ""
    per_image_results: list[VisionResult] = field(default_factory=list)
    state: VisionAgentState = VisionAgentState.IDLE
    processing_time_ms: float = 0.0
    error: str = ""


# ---------------------------------------------------------------------------
# Vision Agent
# ---------------------------------------------------------------------------


class VisionAgent:
    """End-to-end vision processing agent.

    Processes images and video through vision-capable LLMs to extract
    descriptions, detect objects, and read text.

    Example:
        >>> agent = VisionAgent(VisionSessionConfig())
        >>> result = await agent.analyze_single("photo.jpg", "What's in this photo?")
        >>> print(result.description)
    """

    def __init__(self, config: VisionSessionConfig | None = None):
        self.config = config or VisionSessionConfig()

    async def analyze_single(
        self,
        image_source: str | bytes,
        prompt: str = "Describe this image in detail.",
        *,
        system_prompt: str | None = None,
    ) -> VisionSessionResult:
        """Analyze a single image.

        Args:
            image_source: Path to image file or raw bytes.
            prompt: What to ask about the image.
            system_prompt: System-level instruction.

        Returns:
            VisionSessionResult.
        """
        start = time.time()

        # Load image
        if isinstance(image_source, str):
            image_bytes = load_image_frame(image_source)
            if image_bytes is None:
                return VisionSessionResult(
                    state=VisionAgentState.ERROR,
                    error=f"Cannot load image: {image_source}",
                )
            image_data = image_bytes.data
        else:
            image_data = image_source

        # Validate
        valid, err = validate_image(image_data)
        if not valid:
            return VisionSessionResult(
                state=VisionAgentState.ERROR,
                error=err,
            )

        # Analyze
        vis_result = await analyze_image(
            image_data,
            provider=self.config.provider,
            model=self.config.model,
            prompt=prompt,
            system_prompt=system_prompt,
            timeout=self.config.timeout,
        )

        if vis_result.metadata.get("error"):
            return VisionSessionResult(
                state=VisionAgentState.ERROR,
                error=str(vis_result.metadata["error"]),
            )

        return VisionSessionResult(
            description=vis_result.description,
            objects=vis_result.objects,
            text_in_images=vis_result.text_in_image,
            per_image_results=[vis_result],
            state=VisionAgentState.DONE,
            processing_time_ms=(time.time() - start) * 1000,
        )

    async def analyze_multiple(
        self,
        image_paths: list[str],
        prompt: str = "Describe these images and note any relationships between them.",
        *,
        system_prompt: str | None = None,
    ) -> VisionSessionResult:
        """Analyze multiple images together.

        Args:
            image_paths: List of image file paths.
            prompt: What to ask.
            system_prompt: System instruction.

        Returns:
            VisionSessionResult.
        """
        start = time.time()
        images: list[bytes] = []

        for path in image_paths[:self.config.max_images]:
            frame = load_image_frame(path)
            if frame is not None:
                images.append(frame.data)

        if not images:
            return VisionSessionResult(
                state=VisionAgentState.ERROR,
                error="No valid images loaded",
            )

        vis_result = await analyze_images(
            images,
            provider=self.config.provider,
            model=self.config.model,
            prompt=prompt,
            system_prompt=system_prompt,
            timeout=self.config.timeout,
        )

        if vis_result.metadata.get("error"):
            return VisionSessionResult(
                state=VisionAgentState.ERROR,
                error=str(vis_result.metadata["error"]),
            )

        return VisionSessionResult(
            description=vis_result.description,
            per_image_results=[vis_result],
            state=VisionAgentState.DONE,
            processing_time_ms=(time.time() - start) * 1000,
        )

    async def analyze_video(
        self,
        video_path: str,
        prompt: str = "Describe what happens in this video.",
        *,
        system_prompt: str | None = None,
    ) -> VisionSessionResult:
        """Analyze a video by sampling frames.

        Args:
            video_path: Path to video file.
            prompt: What to ask.
            system_prompt: System instruction.

        Returns:
            VisionSessionResult.
        """
        start = time.time()

        # Sample frames
        frames = sample_video_frames(
            video_path,
            interval_sec=self.config.video_frame_interval_sec,
            max_frames=self.config.max_video_frames,
        )

        if not frames:
            return VisionSessionResult(
                state=VisionAgentState.ERROR,
                error=f"No frames extracted from: {video_path}",
            )

        # Analyze each frame individually, then summarize
        per_frame_results: list[VisionResult] = []
        for i, frame in enumerate(frames):
            result = await analyze_image(
                frame.data,
                provider=self.config.provider,
                model=self.config.model,
                prompt=f"Frame {i}: {prompt}",
                timeout=self.config.timeout,
            )
            per_frame_results.append(result)

        # Generate summary from all frame descriptions
        descriptions = [r.description for r in per_frame_results if r.description]
        summary_prompt = (
            f"Here are frame-by-frame descriptions of a video. "
            f"Provide a cohesive summary of what happens:\n\n" +
            "\n\n---\n\n".join(f"[Frame {i}]: {d}" for i, d in enumerate(descriptions))
        )

        summary = await analyze_image(
            b"",  # No image for summary
            provider=self.config.provider,
            model=self.config.model,
            prompt=summary_prompt,
            timeout=self.config.timeout,
        )

        return VisionSessionResult(
            description=summary.description or "\n".join(descriptions),
            per_image_results=per_frame_results,
            state=VisionAgentState.DONE,
            processing_time_ms=(time.time() - start) * 1000,
        )


# ---------------------------------------------------------------------------
# omodul interface
# ---------------------------------------------------------------------------


async def run_vision_analysis(
    config: Any,
    input_data: Any,
    output_dir: Path = Path("/tmp/veya"),
) -> dict[str, Any]:
    """omodul contract: run vision analysis from config + input.

    Args:
        config: SimpleNamespace/dict with vision configuration.
        input_data: SimpleNamespace/dict with media paths, prompt, etc.
        output_dir: Output directory.

    Returns:
        Dict with description, objects, per_image_results, stats.
    """
    from types import SimpleNamespace

    if isinstance(input_data, dict):
        input_data = SimpleNamespace(**input_data)
    if isinstance(config, dict):
        config = SimpleNamespace(**config)

    media_type = getattr(input_data, "media_type", "image")
    prompt = getattr(input_data, "prompt", "Describe this image in detail.")
    system_prompt = getattr(input_data, "system_prompt", None)

    session_config = VisionSessionConfig(
        provider=getattr(config, "provider", "openai"),
        model=getattr(config, "model", None),
        max_images=getattr(config, "max_images", 20),
        max_video_frames=getattr(config, "max_video_frames", 30),
        video_frame_interval_sec=getattr(config, "video_frame_interval_sec", 1.0),
    )

    agent = VisionAgent(session_config)

    if media_type == "image":
        image_path = getattr(input_data, "image_path", None)
        image_data = getattr(input_data, "image_data", None)

        if image_path:
            result = await agent.analyze_single(image_path, prompt=prompt, system_prompt=system_prompt)
        elif image_data:
            result = await agent.analyze_single(image_data, prompt=prompt, system_prompt=system_prompt)
        else:
            result = VisionSessionResult(state=VisionAgentState.ERROR, error="No image source provided")

    elif media_type == "images":
        image_paths = getattr(input_data, "image_paths", [])
        result = await agent.analyze_multiple(image_paths, prompt=prompt, system_prompt=system_prompt)

    elif media_type == "video":
        video_path = getattr(input_data, "video_path", "")
        result = await agent.analyze_video(video_path, prompt=prompt, system_prompt=system_prompt)

    else:
        result = VisionSessionResult(state=VisionAgentState.ERROR, error=f"Unknown media type: {media_type}")

    # Save output
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "description.txt").write_text(result.description)

    return {
        "status": "completed" if result.state == VisionAgentState.DONE else "error",
        "description": result.description,
        "objects": result.objects,
        "text_in_images": result.text_in_images,
        "per_image_count": len(result.per_image_results),
        "processing_time_ms": result.processing_time_ms,
        "error": result.error,
    }
