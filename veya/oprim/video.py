"""
veya/oprim/video.py — Atomic video/image operations (Layer 1).

Stateless, pure-function operations for image encoding, decoding, resizing,
format conversion, and video frame extraction.

Dependencies: stdlib only (base64, io, struct). Optional: PIL/Pillow for resize.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from veya.oprim.types import ImageFormat, ImageFrame

# ---------------------------------------------------------------------------
# Image format detection
# ---------------------------------------------------------------------------

_MIME_TO_FORMAT: dict[str, ImageFormat] = {
    "image/png": ImageFormat.PNG,
    "image/jpeg": ImageFormat.JPEG,
    "image/webp": ImageFormat.WEBP,
    "image/bmp": ImageFormat.BMP,
    "image/gif": ImageFormat.GIF,
}

_EXT_TO_FORMAT: dict[str, ImageFormat] = {
    ".png": ImageFormat.PNG,
    ".jpg": ImageFormat.JPEG,
    ".jpeg": ImageFormat.JPEG,
    ".webp": ImageFormat.WEBP,
    ".bmp": ImageFormat.BMP,
    ".gif": ImageFormat.GIF,
}


def detect_image_format(data: bytes) -> ImageFormat | None:
    """Detect image format from magic bytes (first few bytes of the file).

    Returns:
        ImageFormat or None if unrecognized.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ImageFormat.PNG
    if data.startswith(b"\xff\xd8\xff"):
        return ImageFormat.JPEG
    if data.startswith(b"RIFF") and b"WEBP" in data[:12]:
        return ImageFormat.WEBP
    if data.startswith(b"BM"):
        return ImageFormat.BMP
    if data.startswith(b"GIF8"):
        return ImageFormat.GIF
    return None


def detect_format_from_path(path: str) -> ImageFormat | None:
    """Detect image format from file extension."""
    suffix = Path(path).suffix.lower()
    return _EXT_TO_FORMAT.get(suffix)


# ---------------------------------------------------------------------------
# Base64 encode / decode
# ---------------------------------------------------------------------------


def encode_image_base64(image_path: str) -> str:
    """Read an image file and return its base64-encoded string.

    Returns:
        Base64 string (no data URI prefix).

    Raises:
        FileNotFoundError: If the path does not exist.
    """
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def decode_image_base64(data: str) -> bytes:
    """Decode a base64 string back to raw image bytes."""
    return base64.b64decode(data)


def encode_image_bytes_base64(image_bytes: bytes) -> str:
    """Encode raw image bytes to base64 string."""
    return base64.b64encode(image_bytes).decode("utf-8")


def image_to_data_uri(image_bytes: bytes, fmt: ImageFormat | None = None) -> str:
    """Convert raw image bytes to a data URI string.

    Args:
        image_bytes: Raw image file bytes.
        fmt: Image format (auto-detected if None).

    Returns:
        Data URI like "data:image/png;base64,iVBORw0KG..."

    Example:
        >>> uri = image_to_data_uri(png_bytes)
        >>> uri.startswith("data:image/png;base64,")
        True
    """
    if fmt is None:
        fmt = detect_image_format(image_bytes) or ImageFormat.PNG
    b64 = encode_image_bytes_base64(image_bytes)
    mime = _MIME_TO_FORMAT_REVERSE.get(fmt, "image/png")
    return f"data:{mime};base64,{b64}"


_MIME_TO_FORMAT_REVERSE: dict[ImageFormat, str] = {v: k for k, v in _MIME_TO_FORMAT.items()}


def parse_data_uri(uri: str) -> tuple[bytes, ImageFormat] | None:
    """Parse a data URI into (image_bytes, format).

    Returns:
        Tuple of (bytes, ImageFormat) or None if parsing fails.

    Example:
        >>> uri = "data:image/png;base64,iVBORw0KGgo="
        >>> data, fmt = parse_data_uri(uri)
        >>> fmt == ImageFormat.PNG
        True
    """
    if not uri.startswith("data:"):
        return None
    try:
        header, b64_data = uri.split(",", 1)
        mime = header[5:].split(";")[0]
        fmt = _MIME_TO_FORMAT.get(mime)
        if fmt is None:
            return None
        return base64.b64decode(b64_data), fmt
    except (ValueError, base64.binascii.Error):
        return None


# ---------------------------------------------------------------------------
# Image resize (simple pixel averaging, no PIL dependency)
# ---------------------------------------------------------------------------


def resize_image_simple(
    data: bytes,
    target_width: int,
    target_height: int,
) -> bytes:
    """Resize an image using simple nearest-neighbor downsampling of raw RGB pixels.

    This is a fallback when PIL is not available. Works for raw RGB data only.
    For production use with actual image files, use PIL/Pillow.

    Args:
        data: Raw RGB pixel data (width * height * 3 bytes).
        target_width: Desired width in pixels.
        target_height: Desired height in pixels.

    Returns:
        Resized raw RGB pixel data.
    """
    # For actual image files, this is a stub; real resize needs PIL
    # We store the raw pixel approach for flexible input
    if target_width <= 0 or target_height <= 0:
        return data

    # Estimate original dimensions from data size
    total_pixels = len(data) // 3
    # Guess square-ish dimensions
    src_width = src_height = int(total_pixels**0.5)
    if src_width * src_height != total_pixels:
        # Non-square; just return original (can't guess dimensions)
        return data

    result = bytearray(target_width * target_height * 3)
    x_ratio = src_width / target_width
    y_ratio = src_height / target_height

    for y in range(target_height):
        for x in range(target_width):
            src_x = min(int(x * x_ratio), src_width - 1)
            src_y = min(int(y * y_ratio), src_height - 1)
            src_idx = (src_y * src_width + src_x) * 3
            dst_idx = (y * target_width + x) * 3
            result[dst_idx : dst_idx + 3] = data[src_idx : src_idx + 3]

    return bytes(result)


# ---------------------------------------------------------------------------
# Image frame creation & validation
# ---------------------------------------------------------------------------


def create_image_frame(
    data: bytes,
    width: int = 0,
    height: int = 0,
    fmt: ImageFormat | None = None,
    timestamp_ms: float = 0.0,
) -> ImageFrame:
    """Create an ImageFrame from raw bytes with auto-detection.

    Args:
        data: Raw image bytes.
        width: Image width (0 = unknown).
        height: Image height (0 = unknown).
        fmt: Image format (auto-detected if None).
        timestamp_ms: Frame timestamp.

    Returns:
        ImageFrame instance.
    """
    if fmt is None:
        fmt = detect_image_format(data) or ImageFormat.PNG
    return ImageFrame(
        data=data,
        width=width,
        height=height,
        format=fmt,
        timestamp_ms=timestamp_ms,
    )


def load_image_frame(path: str, timestamp_ms: float = 0.0) -> ImageFrame | None:
    """Load an image file into an ImageFrame.

    Returns:
        ImageFrame or None if the file doesn't exist or is unsupported.
    """
    if not os.path.exists(path):
        return None
    fmt = detect_format_from_path(path)
    if fmt is None:
        return None
    with open(path, "rb") as f:
        data = f.read()
    return create_image_frame(data, fmt=fmt, timestamp_ms=timestamp_ms)


# ---------------------------------------------------------------------------
# Video frame extraction (simple — no heavy codec deps)
# ---------------------------------------------------------------------------


def extract_video_frame_at(
    video_path: str,
    timestamp_sec: float,
) -> bytes | None:
    """Extract a single frame from a video file at a given timestamp.

    Uses ffmpeg subprocess (requires ffmpeg installed).
    Returns PNG bytes of the extracted frame.

    Args:
        video_path: Path to video file.
        timestamp_sec: Time in seconds to extract frame from.

    Returns:
        PNG image bytes, or None on failure.
    """
    import subprocess
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name

        cmd = [
            "ffmpeg",
            "-ss",
            str(timestamp_sec),
            "-i",
            video_path,
            "-vframes",
            "1",
            "-q:v",
            "2",
            "-y",
            tmp_path,
        ]
        subprocess.run(cmd, capture_output=True, check=True, timeout=30)

        with open(tmp_path, "rb") as f:
            result = f.read()
        os.unlink(tmp_path)
        return result
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def sample_video_frames(
    video_path: str,
    interval_sec: float = 1.0,
    max_frames: int = 30,
) -> list[ImageFrame]:
    """Sample frames from a video at regular intervals.

    Args:
        video_path: Path to video file.
        interval_sec: Seconds between frame samples.
        max_frames: Maximum number of frames to extract.

    Returns:
        List of ImageFrame objects.
    """
    frames: list[ImageFrame] = []

    for i in range(max_frames):
        timestamp = i * interval_sec
        data = extract_video_frame_at(video_path, timestamp)
        if data is None:
            break
        frames.append(create_image_frame(data, timestamp_ms=timestamp * 1000))

    return frames


# ---------------------------------------------------------------------------
# Image validation
# ---------------------------------------------------------------------------


def validate_image(data: bytes, max_size_kb: int = 20480) -> tuple[bool, str]:
    """Validate image data for safety and size limits.

    Args:
        data: Raw image bytes.
        max_size_kb: Maximum allowed size in KB (default 20MB).

    Returns:
        Tuple of (is_valid, reason).
    """
    if not data:
        return False, "Empty image data"

    if len(data) > max_size_kb * 1024:
        return False, f"Image too large: {len(data) / 1024:.0f}KB > {max_size_kb}KB"

    fmt = detect_image_format(data)
    if fmt is None:
        return False, "Unrecognized image format"

    return True, "ok"
