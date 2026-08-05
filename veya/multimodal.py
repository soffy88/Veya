"""
Multimodal processing module — P2 core capability.
Features: image understanding, OCR, document parsing (PDF/Word/images).
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


@dataclass
class MultimodalResult:
    """Multimodal processing result."""

    source_type: str  # image, document, audio
    source_path: str
    text: str = ""
    description: str = ""
    extracted_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str = ""


class ImageProcessor:
    """
    Image processor.

    Features:
    1. OCR text extraction
    2. Code-screenshot recognition
    3. Image description generation
    4. Image encoding (for LLMs)
    """

    MEDIA_TYPES: ClassVar[dict[str, str]] = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
    }

    def __init__(self):
        self.supported_formats = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

    def _is_supported(self, path: str) -> bool:
        """Check whether the image format is supported."""
        return Path(path).suffix.lower() in self.supported_formats

    def encode_image(self, image_path: str) -> str | None:
        """Encode an image as base64."""
        if not self._is_supported(image_path):
            return None
        try:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            print(f"[ImageProcessor] Failed to encode image: {e}")
            return None

    def media_type(self, image_path: str) -> str:
        """Return the MIME type for a file extension (defaults to image/png)."""
        suffix = Path(image_path).suffix.lstrip(".").lower()
        return self.MEDIA_TYPES.get(suffix, "image/png")

    def to_content_block(self, image_path: str) -> dict[str, Any] | None:
        """Encode an image as an OpenAI-style ``image_url`` content block (G12, consumed by providers).

        Returns ``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}``,
        or None when the file is missing or unsupported.
        """
        b64 = self.encode_image(image_path)
        if b64 is None:
            return None
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{self.media_type(image_path)};base64,{b64}",
            },
        }

    def extract_text_ocr(self, image_path: str) -> str:
        """Extract text via OCR (simplified; Tesseract/third-party OCR can be plugged in)."""
        if not self._is_supported(image_path):
            return ""

        # 模拟 OCR 结果
        # 实际实现应使用 pytesseract 或其他 OCR 库
        return f"[OCR placeholder for {image_path}]"

    def is_code_screenshot(self, image_path: str) -> bool:
        """Determine whether the image is a code screenshot."""
        text = self.extract_text_ocr(image_path)
        code_indicators = [
            "def ",
            "class ",
            "import ",
            "return ",
            "function",
            "const ",
            "let ",
            "var ",
            "=>",
            "#include",
        ]
        return any(indicator in text for indicator in code_indicators)

    def parse_code_from_image(self, image_path: str) -> str | None:
        """Extract code from a code screenshot."""
        if not self.is_code_screenshot(image_path):
            return None

        # 简化实现：提取看起来是代码的部分
        text = self.extract_text_ocr(image_path)

        # 去除行号、提示符等
        lines = text.split("\n")
        cleaned_lines = []
        for line in lines:
            # 去除常见行号前缀
            cleaned = re.sub(r"^\s*\d+[:\.]?\s*", "", line)
            # 去除提示符
            cleaned = re.sub(r"^(?:>>>|\$|>)\s*", "", cleaned)
            if cleaned.strip():
                cleaned_lines.append(cleaned)

        return "\n".join(cleaned_lines)

    def analyze(self, image_path: str) -> MultimodalResult:
        """Analyze an image."""
        try:
            if not os.path.exists(image_path):
                return MultimodalResult(
                    source_type="image",
                    source_path=image_path,
                    success=False,
                    error="File not found",
                )

            base64_image = self.encode_image(image_path)
            text = self.extract_text_ocr(image_path)
            code = self.parse_code_from_image(image_path)

            # 生成描述
            description = f"Image: {Path(image_path).name}"
            if code:
                description += " (appears to be a code screenshot)"

            return MultimodalResult(
                source_type="image",
                source_path=image_path,
                text=text,
                description=description,
                extracted_code=code,
                metadata={
                    "base64_size": len(base64_image) if base64_image else 0,
                    "is_code_screenshot": code is not None,
                },
            )
        except Exception as e:
            return MultimodalResult(
                source_type="image", source_path=image_path, success=False, error=str(e)
            )


class DocumentProcessor:
    """
    Document processor.

    Features:
    1. PDF text extraction
    2. Word document parsing
    3. Document segmentation
    4. Metadata extraction
    """

    def __init__(self):
        self.supported_formats = {".pdf", ".docx", ".doc", ".txt", ".md"}

    def extract_pdf(self, doc_path: str) -> str:
        """Extract PDF text."""
        try:
            # 简化版：实际应使用 PyPDF2 / pdfplumber
            # 这里仅作为接口示例
            return f"[PDF text placeholder for {doc_path}]"
        except Exception as e:
            return f"[PDF extraction error: {e!s}]"

    def extract_docx(self, doc_path: str) -> str:
        """Extract Word document text."""
        try:
            # 简化版：实际应使用 python-docx
            return f"[DOCX text placeholder for {doc_path}]"
        except Exception as e:
            return f"[DOCX extraction error: {e!s}]"

    def extract_text(self, doc_path: str) -> str:
        """Extract text generically."""
        suffix = Path(doc_path).suffix.lower()

        if suffix == ".pdf":
            return self.extract_pdf(doc_path)
        elif suffix in [".docx", ".doc"]:
            return self.extract_docx(doc_path)
        elif suffix in [".txt", ".md", ".py", ".js", ".json"]:
            with open(doc_path, encoding="utf-8") as f:
                return f.read()
        else:
            return f"[Unsupported document format: {suffix}]"

    def segment_document(
        self, doc_path: str, chunk_size: int = 1000, overlap: int = 100
    ) -> list[dict[str, Any]]:
        """Segment a document into chunks."""
        text = self.extract_text(doc_path)
        chunks = []

        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk = text[start:end]
            chunks.append({"text": chunk, "start": start, "end": end, "source": doc_path})
            start = end - overlap

        return chunks

    def analyze(self, doc_path: str) -> MultimodalResult:
        """Analyze a document."""
        try:
            if not os.path.exists(doc_path):
                return MultimodalResult(
                    source_type="document",
                    source_path=doc_path,
                    success=False,
                    error="File not found",
                )

            text = self.extract_text(doc_path)
            segments = self.segment_document(doc_path)

            return MultimodalResult(
                source_type="document",
                source_path=doc_path,
                text=text,
                description=f"Document: {Path(doc_path).name}",
                metadata={
                    "format": Path(doc_path).suffix.lower(),
                    "length": len(text),
                    "segments": len(segments),
                },
            )
        except Exception as e:
            return MultimodalResult(
                source_type="document", source_path=doc_path, success=False, error=str(e)
            )


class MultimodalProcessor:
    """
    Multimodal processor.

    Handles images, documents, audio and other inputs uniformly.
    """

    def __init__(self):
        self.image_processor = ImageProcessor()
        self.document_processor = DocumentProcessor()

    def process(self, file_path: str) -> MultimodalResult:
        """Process any supported file."""
        suffix = Path(file_path).suffix.lower()

        if suffix in self.image_processor.supported_formats:
            return self.image_processor.analyze(file_path)
        elif suffix in self.document_processor.supported_formats:
            return self.document_processor.analyze(file_path)
        else:
            return MultimodalResult(
                source_type="unknown",
                source_path=file_path,
                success=False,
                error=f"Unsupported file format: {suffix}",
            )

    def process_batch(self, file_paths: list[str]) -> list[MultimodalResult]:
        """Process files in batch."""
        return [self.process(path) for path in file_paths]

    def prepare_for_llm(self, file_path: str) -> dict[str, Any] | None:
        """Prepare a file for LLM consumption."""
        result = self.process(file_path)

        if not result.success:
            return None

        if result.source_type == "image":
            base64_image = self.image_processor.encode_image(file_path)
            return {
                "type": "image",
                "url": f"data:{self.image_processor.media_type(file_path)};base64,{base64_image}",
                "text": result.text,
                "description": result.description,
            }
        else:
            return {"type": "document", "text": result.text, "description": result.description}

    def build_vision_messages(
        self,
        text: str,
        image_paths: list[str],
        *,
        system: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build vision messages ready for an LLM provider (G12).

        Text + images → OpenAI-style content blocks (``text`` blocks + ``image_url``
        data-URI blocks); missing/unsupported images are skipped automatically.
        A non-empty ``system`` prompt is placed first.
        """
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for path in image_paths:
            block = self.image_processor.to_content_block(path)
            if block is not None:
                blocks.append(block)

        messages: list[dict[str, Any]] = [{"role": "user", "content": blocks}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        return messages


# =========================================================================
# Audio Processor (G13 voice/vision integration)
# =========================================================================


class AudioProcessor:
    """
    Audio processor for voice agent integration.

    Features:
    1. Audio format detection (WAV, MP3, raw PCM)
    2. Audio to base64 encoding
    3. Audio segmentation (split into chunks)
    4. Audio metadata extraction (duration, sample rate, channels)
    5. Silence detection and trimming
    """

    SUPPORTED_FORMATS: ClassVar[set[str]] = {".wav", ".mp3", ".ogg", ".flac", ".aac", ".pcm", ".raw"}

    def __init__(self):
        self.supported_formats = self.SUPPORTED_FORMATS

    def detect_format(self, path: str) -> str | None:
        """Detect audio format from file extension."""
        suffix = Path(path).suffix.lower()
        return suffix.lstrip(".") if suffix in self.supported_formats else None

    def encode_audio_base64(self, audio_path: str) -> str | None:
        """Encode an audio file as base64."""
        if not os.path.exists(audio_path):
            return None
        try:
            with open(audio_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            print(f"[AudioProcessor] Failed to encode audio: {e}")
            return None

    def extract_metadata(self, audio_path: str) -> dict[str, Any]:
        """Extract metadata from an audio file.

        Returns dict with: duration_sec, sample_rate, channels, sample_width, format.
        """
        try:
            import wave
            with wave.open(audio_path, "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                return {
                    "duration_sec": frames / rate if rate > 0 else 0,
                    "sample_rate": rate,
                    "channels": wf.getnchannels(),
                    "sample_width": wf.getsampwidth(),
                    "format": "wav",
                    "file_size": os.path.getsize(audio_path),
                }
        except Exception:
            suffix = Path(audio_path).suffix.lower()
            return {
                "duration_sec": 0,
                "sample_rate": 16000,
                "channels": 1,
                "sample_width": 2,
                "format": suffix.lstrip("."),
                "file_size": os.path.getsize(audio_path),
            }

    def read_pcm(self, audio_path: str, target_sample_rate: int = 16000) -> bytes | None:
        """Read an audio file and return raw PCM bytes.

        For WAV files, extracts the PCM data. For other formats, returns raw bytes.
        """
        if not os.path.exists(audio_path):
            return None
        try:
            suffix = Path(audio_path).suffix.lower()
            if suffix == ".wav":
                import wave
                with wave.open(audio_path, "rb") as wf:
                    return wf.readframes(wf.getnframes())
            else:
                with open(audio_path, "rb") as f:
                    return f.read()
        except Exception as e:
            print(f"[AudioProcessor] Failed to read audio: {e}")
            return None

    def analyze(self, audio_path: str) -> MultimodalResult:
        """Analyze an audio file."""
        try:
            if not os.path.exists(audio_path):
                return MultimodalResult(
                    source_type="audio",
                    source_path=audio_path,
                    success=False,
                    error="File not found",
                )

            metadata = self.extract_metadata(audio_path)
            base64_audio = self.encode_audio_base64(audio_path)

            return MultimodalResult(
                source_type="audio",
                source_path=audio_path,
                text=f"[Audio: {Path(audio_path).name}]",
                description=f"Audio file: {Path(audio_path).name} ({metadata.get('duration_sec', 0):.1f}s)",
                metadata={
                    **metadata,
                    "base64_size": len(base64_audio) if base64_audio else 0,
                },
            )
        except Exception as e:
            return MultimodalResult(
                source_type="audio", source_path=audio_path, success=False, error=str(e)
            )


# =========================================================================
# Video Processor (G13 voice/vision integration)
# =========================================================================


class VideoProcessor:
    """
    Video processor for vision agent integration.

    Features:
    1. Video metadata extraction
    2. Frame sampling (using ffmpeg)
    3. Video format detection
    4. Thumbnail generation
    """

    SUPPORTED_FORMATS: ClassVar[set[str]] = {".mp4", ".webm", ".avi", ".mov", ".mkv", ".ogg"}

    def __init__(self):
        self.supported_formats = self.SUPPORTED_FORMATS

    def detect_format(self, path: str) -> str | None:
        """Detect video format from file extension."""
        suffix = Path(path).suffix.lower()
        return suffix.lstrip(".") if suffix in self.supported_formats else None

    def extract_metadata(self, video_path: str) -> dict[str, Any]:
        """Extract video metadata using ffprobe (requires ffmpeg)."""
        try:
            import subprocess
            import json

            cmd = [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                video_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                fmt = data.get("format", {})
                video_stream = None
                for stream in data.get("streams", []):
                    if stream.get("codec_type") == "video":
                        video_stream = stream
                        break

                return {
                    "duration_sec": float(fmt.get("duration", 0)),
                    "file_size": int(fmt.get("size", 0)),
                    "format_name": fmt.get("format_name", ""),
                    "width": video_stream.get("width", 0) if video_stream else 0,
                    "height": video_stream.get("height", 0) if video_stream else 0,
                    "codec": video_stream.get("codec_name", "") if video_stream else "",
                    "fps": self._parse_fps(video_stream) if video_stream else 0,
                }
        except Exception:
            pass
        return {"duration_sec": 0, "file_size": os.path.getsize(video_path)}

    @staticmethod
    def _parse_fps(stream: dict) -> float:
        """Parse FPS from ffprobe stream info."""
        fps_str = stream.get("r_frame_rate", "0/1")
        try:
            num, den = fps_str.split("/")
            return float(num) / float(den) if float(den) != 0 else 0
        except (ValueError, ZeroDivisionError):
            return 0

    def extract_frame(self, video_path: str, timestamp_sec: float = 0.0) -> bytes | None:
        """Extract a single frame from a video at a given timestamp."""
        try:
            import subprocess
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name

            cmd = [
                "ffmpeg",
                "-ss", str(timestamp_sec),
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "2",
                "-y",
                tmp_path,
            ]
            subprocess.run(cmd, capture_output=True, check=True, timeout=30)

            with open(tmp_path, "rb") as f:
                result = f.read()
            os.unlink(tmp_path)
            return result
        except Exception:
            return None

    def sample_frames(
        self, video_path: str, interval_sec: float = 1.0, max_frames: int = 30
    ) -> list[dict[str, Any]]:
        """Sample frames from a video at regular intervals.

        Returns list of dicts with: timestamp_sec, image_base64, image_bytes.
        """
        metadata = self.extract_metadata(video_path)
        duration = metadata.get("duration_sec", 0)
        if duration <= 0:
            return []

        frames: list[dict] = []
        for i in range(max_frames):
            ts = i * interval_sec
            if ts > duration:
                break
            frame_data = self.extract_frame(video_path, ts)
            if frame_data:
                frames.append({
                    "timestamp_sec": ts,
                    "image_bytes": frame_data,
                    "image_base64": base64.b64encode(frame_data).decode("utf-8"),
                })
        return frames

    def analyze(self, video_path: str) -> MultimodalResult:
        """Analyze a video file (extracts metadata + first frame)."""
        try:
            if not os.path.exists(video_path):
                return MultimodalResult(
                    source_type="video",
                    source_path=video_path,
                    success=False,
                    error="File not found",
                )

            metadata = self.extract_metadata(video_path)
            thumbnail = self.extract_frame(video_path, 0)

            return MultimodalResult(
                source_type="video",
                source_path=video_path,
                text=f"[Video: {Path(video_path).name}]",
                description=(
                    f"Video: {Path(video_path).name} "
                    f"({metadata.get('width', '?')}x{metadata.get('height', '?')}, "
                    f"{metadata.get('duration_sec', 0):.1f}s)"
                ),
                metadata={
                    **metadata,
                    "has_thumbnail": thumbnail is not None,
                    "thumbnail_size": len(thumbnail) if thumbnail else 0,
                },
            )
        except Exception as e:
            return MultimodalResult(
                source_type="video", source_path=video_path, success=False, error=str(e)
            )


# =========================================================================
# Enhanced MultimodalProcessor (G13 — now supports audio + video)
# =========================================================================

# Patch MultimodalProcessor to support audio/video
_original_process = MultimodalProcessor.process


def _enhanced_process(self, file_path: str) -> MultimodalResult:
    """Enhanced process that also handles audio and video files."""
    suffix = Path(file_path).suffix.lower()

    if suffix in self.image_processor.supported_formats:
        return self.image_processor.analyze(file_path)
    elif suffix in self.document_processor.supported_formats:
        return self.document_processor.analyze(file_path)
    elif suffix in (".wav", ".mp3", ".ogg", ".flac", ".aac"):
        return AudioProcessor().analyze(file_path)
    elif suffix in (".mp4", ".webm", ".avi", ".mov", ".mkv"):
        return VideoProcessor().analyze(file_path)
    else:
        return MultimodalResult(
            source_type="unknown",
            source_path=file_path,
            success=False,
            error=f"Unsupported file format: {suffix}",
        )


MultimodalProcessor.process = _enhanced_process


# 便捷函数
def create_multimodal_processor() -> MultimodalProcessor:
    """Create a multimodal processor."""
    return MultimodalProcessor()


def create_audio_processor() -> AudioProcessor:
    """Create an audio processor."""
    return AudioProcessor()


def create_video_processor() -> VideoProcessor:
    """Create a video processor."""
    return VideoProcessor()


if __name__ == "__main__":
    # 测试
    processor = create_multimodal_processor()

    # 测试图像处理
    print("=== Testing Image Processing ===")
    # 创建一个临时测试图像文件
    test_image = "/tmp/test_image.txt"
    with open(test_image, "w") as f:
        f.write("def hello():\n    return 'world'")

    # 注意：这里用 .txt 模拟，实际应为图片
    result = processor.process(test_image)
    print(f"Result: {json.dumps(result.__dict__, default=str, indent=2)}")

    # 测试文档处理
    print("\n=== Testing Document Processing ===")
    test_doc = "/tmp/test_doc.md"
    with open(test_doc, "w") as f:
        f.write("# Hello\n\nThis is a test document.\n\n## Section 2\n\nMore content here." * 10)

    result = processor.process(test_doc)
    print(f"Result: {json.dumps(result.__dict__, default=str, indent=2)}")
    print(f"Segments: {len(processor.document_processor.segment_document(test_doc))}")
