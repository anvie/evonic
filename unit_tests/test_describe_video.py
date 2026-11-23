import shutil
import subprocess
from io import BytesIO
from unittest.mock import patch

import pytest

import backend.tools.describe_video as describe_video
from backend.agent_runtime import context as ctx
from backend.tools.describe_video import execute

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not installed",
)


@pytest.mark.parametrize("duration,expected", [
    (3, 1), (15, 1), (15.5, 2), (60, 2), (61, 3), (180, 3), (181, 4), (3600, 4),
])
def test_grid_count_by_duration(duration, expected):
    assert describe_video._grid_count(duration) == expected


def test_frame_timestamps_are_centred_and_even():
    stamps = describe_video._frame_timestamps(8, 1)
    assert stamps == [1.0, 3.0, 5.0, 7.0]
    assert len(describe_video._frame_timestamps(300, 4)) == 16


def _make_video(path, seconds=8):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc=duration={seconds}:size=320x240:rate=10",
         "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


def _vision_model():
    return {"id": "vm", "name": "vm"}


@needs_ffmpeg
def test_build_grids_makes_labelled_2x2_images(tmp_path):
    from PIL import Image

    video = tmp_path / "clip.mp4"
    _make_video(video, seconds=20)
    grids, duration, error = describe_video.build_grids(
        shutil.which("ffmpeg"), shutil.which("ffprobe"), str(video))
    assert error is None
    assert round(duration) == 20
    assert len(grids) == 2  # 15 < 20 <= 60
    for jpeg, stamps in grids:
        assert len(stamps) == 4
        img = Image.open(BytesIO(jpeg))
        assert img.size == (describe_video._CELL_WIDTH * 2, describe_video._CELL_HEIGHT * 2)


@needs_ffmpeg
def test_execute_sends_grids_and_timestamps_to_vision_model(tmp_path):
    video = tmp_path / "clip.mp4"
    _make_video(video, seconds=8)
    captured = {}

    def fake_call(models, messages):
        captured["messages"] = messages
        return "a test pattern"

    with patch.object(describe_video, "_resolve_vision_models",
                      return_value=([_vision_model()], None)), \
         patch.object(describe_video, "_call_vision_models", side_effect=fake_call):
        result = execute({}, {"path": str(video), "query": "what moves?"})

    assert result == "a test pattern"
    content = captured["messages"][1]["content"]
    images = [p for p in content if p["type"] == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "what moves?" in content[0]["text"]
    assert "00:01, 00:03, 00:05, 00:07" in content[0]["text"]


def test_vision_disabled():
    assert "vision_enabled=0" in execute({"vision_enabled": 0}, {"path": "a.mp4"})


def test_path_required():
    assert "'path' parameter is required" in execute({}, {})


def test_unsupported_type(tmp_path):
    result = execute({}, {"path": str(tmp_path / "notes.txt")})
    assert result.startswith("Error: Unsupported video type")


def test_missing_file_reports_not_found(tmp_path):
    with patch.object(describe_video, "_ensure_ffmpeg", return_value="ffmpeg"), \
         patch.object(describe_video.shutil, "which", return_value="ffprobe"):
        result = execute({}, {"path": str(tmp_path / "gone.mp4")})
    assert result.startswith("Error:")


def test_too_large_uses_video_wording(tmp_path):
    video = tmp_path / "big.mp4"
    video.write_bytes(b"x" * 16)
    with patch.object(describe_video, "_ensure_ffmpeg", return_value="ffmpeg"), \
         patch.object(describe_video.shutil, "which", return_value="ffprobe"), \
         patch.object(describe_video, "_MAX_VIDEO_BYTES", 8):
        # _read_image receives max_bytes at call time from the patched constant
        result = execute({}, {"path": str(video)})
    assert result.startswith("Error: Video file")


def test_video_attachment_note_hints_describe_video():
    info = {"filename": "c.mp4", "mime_type": "video/mp4", "size_bytes": 10,
            "file_path": "/tmp/c.mp4", "attachment_id": 1}
    assert "`describe_video`" in ctx.build_attachment_note(info, has_describe_image=True)
    assert "describe_video" not in ctx.build_attachment_note(info, has_describe_image=False)
