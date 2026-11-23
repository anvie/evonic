"""
describe_video.py — describe a video by sampling frames for a vision model.

Frames are sampled evenly across the video, packed four at a time into 2x2
grid images (each cell labelled with its timestamp) and sent to the same
vision model chain that describe_image uses. Only the visual track is
analysed; audio is not transcribed.

The `vision_enabled` agent flag gates this tool, exactly like describe_image.
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import tempfile
from io import BytesIO
from typing import Any, List, Optional, Tuple

from backend.tools.describe_image import (
    _call_vision_models,
    _ensure_ffmpeg,
    _read_image,
    _resolve_vision_models,
)

_MAX_VIDEO_BYTES = 100 * 1024 * 1024

_VIDEO_EXTENSIONS = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".3gp": "video/3gpp",
    ".avi": "video/x-msvideo",
}

_CELL_WIDTH = 640
_CELL_HEIGHT = 360
_FRAMES_PER_GRID = 4
_FRAME_TIMEOUT = 30
_GRID_JPEG_QUALITY = 85


def _grid_count(duration: float) -> int:
    """Number of 2x2 grid images to build for a video of ``duration`` seconds."""
    if duration <= 15:
        return 1
    if duration <= 60:
        return 2
    if duration <= 180:
        return 3
    return 4


def _frame_timestamps(duration: float, grids: int) -> List[float]:
    """Evenly spaced timestamps, centred in their slot to skip black intros/outros."""
    count = grids * _FRAMES_PER_GRID
    return [(i + 0.5) * duration / count for i in range(count)]


def _format_ts(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _probe_duration(ffprobe: str, path: str) -> Optional[float]:
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(proc.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    return duration if duration > 0 else None


def _extract_frame(ffmpeg: str, path: str, timestamp: float) -> Optional[bytes]:
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", f"{timestamp:.3f}", "-i", path,
             "-frames:v", "1", "-vf", f"scale={_CELL_WIDTH}:{_CELL_HEIGHT}"
             ":force_original_aspect_ratio=decrease",
             "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=_FRAME_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def _compose_grid(frames: List[Tuple[float, bytes]]) -> bytes:
    """Compose up to four (timestamp, jpeg) frames into one labelled 2x2 JPEG."""
    from PIL import Image, ImageDraw

    canvas = Image.new("RGB", (_CELL_WIDTH * 2, _CELL_HEIGHT * 2), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    for index, (timestamp, jpeg) in enumerate(frames):
        x0 = (index % 2) * _CELL_WIDTH
        y0 = (index // 2) * _CELL_HEIGHT
        frame = Image.open(BytesIO(jpeg)).convert("RGB")
        frame.thumbnail((_CELL_WIDTH, _CELL_HEIGHT))
        canvas.paste(
            frame,
            (x0 + (_CELL_WIDTH - frame.width) // 2, y0 + (_CELL_HEIGHT - frame.height) // 2),
        )
        label = _format_ts(timestamp)
        box = draw.textbbox((x0 + 6, y0 + 6), label)
        draw.rectangle((box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3), fill=(0, 0, 0))
        draw.text((x0 + 6, y0 + 6), label, fill=(255, 255, 255))
    out = BytesIO()
    canvas.save(out, format="JPEG", quality=_GRID_JPEG_QUALITY)
    return out.getvalue()


def build_grids(ffmpeg: str, ffprobe: str, path: str) -> Tuple[Optional[List[Tuple[bytes, List[float]]]], float, Optional[str]]:
    """Sample ``path`` into grid images.

    Returns ``(grids, duration, error)`` where ``grids`` is a list of
    ``(jpeg_bytes, timestamps)``.
    """
    duration = _probe_duration(ffprobe, path)
    if not duration:
        return None, 0.0, "Error: Could not read the video duration. The file may be corrupt or not a video."

    frames: List[Tuple[float, bytes]] = []
    for timestamp in _frame_timestamps(duration, _grid_count(duration)):
        jpeg = _extract_frame(ffmpeg, path, timestamp)
        if jpeg:
            frames.append((timestamp, jpeg))
    if not frames:
        return None, duration, "Error: Could not extract any frames from the video."

    grids = []
    for i in range(0, len(frames), _FRAMES_PER_GRID):
        chunk = frames[i:i + _FRAMES_PER_GRID]
        grids.append((_compose_grid(chunk), [ts for ts, _ in chunk]))
    return grids, duration, None


def execute(agent: dict, args: dict) -> Any:
    """Describe a video file by analysing frames sampled across its duration.

    Args:
        agent: Agent context dict.
        args:
            path (str, required): Path to the video file.
            query (str, optional): Specific question about the video.

    Returns:
        str: Plain-text description of the video, or an error message.
    """
    import base64

    path = args.get("path")
    path = path.strip() if isinstance(path, str) else ""
    query = args.get("query")
    query = query.strip() if isinstance(query, str) else ""

    if not agent.get("vision_enabled", 1):
        return (
            "Error: Video analysis is not enabled for this agent "
            "(vision_enabled=0). Enable it in the agent's settings to use "
            "the describe_video tool. "
            "Troubleshooting: https://evonic.dev/troubleshooting/agent-vision/"
        )
    if not path:
        return "Error: 'path' parameter is required. Provide the file path to the video."

    ext = os.path.splitext(path)[1].lower()
    mime_type = _VIDEO_EXTENSIONS.get(ext) or mimetypes.guess_type(path)[0]
    if not mime_type or not mime_type.startswith("video/"):
        return (
            f"Error: Unsupported video type '{mime_type or ext or 'unknown'}'. "
            "Supported formats: MP4, MOV, WebM, MKV, 3GP, AVI."
        )

    ffmpeg = _ensure_ffmpeg()
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return "Error: ffmpeg/ffprobe is not installed on the host, so videos cannot be analysed."

    video_data, host_path, read_error = _read_image(
        agent, path, max_bytes=_MAX_VIDEO_BYTES, kind="Video")
    if read_error:
        return read_error

    if host_path:
        try:
            from backend.tools._ensure_workplace_file import ensure_workplace_file
            ensure_workplace_file(host_path, agent)
        except (ImportError, RuntimeError):
            pass

    vision_models, error = _resolve_vision_models(agent)
    if error:
        return f"Error: {error}"

    with tempfile.TemporaryDirectory() as tmp:
        local_path = host_path
        if not local_path:
            local_path = os.path.join(tmp, "video" + (ext or ".mp4"))
            with open(local_path, "wb") as handle:
                handle.write(video_data)
        grids, duration, grid_error = build_grids(ffmpeg, ffprobe, local_path)
    if grid_error:
        return grid_error

    system_prompt = (
        "You are a helpful video analysis assistant. You are given frames sampled "
        f"evenly from a {_format_ts(duration)} long video. Each image is a 2x2 grid "
        "read left to right, top to bottom, and each cell is labelled with its "
        "mm:ss timestamp. Describe what happens over time, treating the frames as "
        "one continuous video rather than separate pictures."
    )
    if query:
        user_text = f"Please analyze this video and answer the following question: {query}"
    else:
        user_text = (
            "Please describe this video in detail: the scenes, people, actions, "
            "any on-screen text, and the sequence of events."
        )
    user_text += "\n\nGrid timestamps: " + "; ".join(
        f"image {i}: " + ", ".join(_format_ts(t) for t in stamps)
        for i, (_, stamps) in enumerate(grids, 1)
    )

    content = [{"type": "text", "text": user_text}]
    for jpeg, _ in grids:
        data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": data_url}})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]
    return _call_vision_models(vision_models, messages)
