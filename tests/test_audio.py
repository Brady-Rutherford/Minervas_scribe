import struct
from datetime import UTC, datetime

import pytest

from scribe_core import audio
from scribe_core.audio import (
    CLEAN_FILTER,
    AudioError,
    faststart_from_boxes,
    file_reader,
    job_workdir,
    mp4_top_level_boxes,
    opus_cmd,
    parse_ffprobe,
    run_tool,
    signed_url_expiry,
)
from scribe_core.redact import redact_urls, strip_query

SIGNED = "https://dexample.cloudfront.net/u/rec.mp4?Expires=4102444800&Signature=SIG&Key-Pair-Id=K"


def test_to_opus_command_is_16k_mono_opus_16kbps():
    cmd = opus_cmd("in.mp4", "out.ogg")
    assert cmd[0] == "ffmpeg" and cmd[cmd.index("-i") + 1] == "in.mp4"
    assert cmd[cmd.index("-ac") + 1] == "1" and cmd[cmd.index("-ar") + 1] == "16000"
    assert cmd[cmd.index("-c:a") + 1] == "libopus" and cmd[cmd.index("-b:a") + 1] == "16k"
    assert cmd[cmd.index("-application") + 1] == "voip"
    assert "-vn" in cmd and "-af" not in cmd and cmd[-1] == "out.ogg"


def test_clean_command_adds_filter_chain_and_24k():
    cmd = opus_cmd("in.mp4", "out.ogg", clean=True)
    assert cmd[cmd.index("-af") + 1] == CLEAN_FILTER
    assert "loudnorm=I=-18:TP=-2:LRA=11" in CLEAN_FILTER and "highpass=f=80" in CLEAN_FILTER
    assert cmd[cmd.index("-b:a") + 1] == "24k"


def test_to_opus_runs_ffmpeg_into_workdir(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(audio, "run_tool", lambda cmd, timeout=None: seen.setdefault("cmd", cmd))
    out = audio.to_opus("in.mp4", tmp_path)
    assert out == tmp_path / "audio.ogg" and seen["cmd"][-1] == str(out)


def test_tool_errors_never_contain_the_signature(tmp_path):
    with pytest.raises(AudioError) as exc:
        run_tool(["ffprobe", "-v", "error", str(tmp_path / "missing.mp4") + "?x=1",
                  SIGNED])
    assert "Signature=SIG" not in str(exc.value)


def test_job_workdir_is_deleted_with_its_video():
    with job_workdir("job1") as d:
        (d / "input.mp4").write_bytes(b"x" * 10)
        assert d.exists() and "scribe_job1_" in d.name
    assert not d.exists()


def _box(btype: bytes, payload: int) -> bytes:
    return struct.pack(">I", 8 + payload) + btype + b"\0" * payload


def test_faststart_detection_from_top_level_boxes(tmp_path):
    fast = tmp_path / "fast.mp4"
    fast.write_bytes(_box(b"ftyp", 16) + _box(b"moov", 40) + _box(b"mdat", 100))
    slow = tmp_path / "slow.mp4"
    slow.write_bytes(_box(b"ftyp", 16) + _box(b"mdat", 100) + _box(b"moov", 40))
    fb = mp4_top_level_boxes(file_reader(fast))
    sb = mp4_top_level_boxes(file_reader(slow))
    assert [b[0] for b in fb] == ["ftyp", "moov", "mdat"]
    assert faststart_from_boxes(fb) is True and faststart_from_boxes(sb) is False
    assert faststart_from_boxes([("ftyp", 0, 8)]) is None


def test_largesize_boxes_are_walked(tmp_path):
    big = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 16 + 8) + b"\0" * 8
    f = tmp_path / "big.mp4"
    f.write_bytes(_box(b"ftyp", 8) + big + _box(b"moov", 8))
    assert [b[0] for b in mp4_top_level_boxes(file_reader(f))] == ["ftyp", "mdat", "moov"]


def test_parse_ffprobe_extracts_resolution_duration_codecs():
    info = parse_ffprobe({
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360},
                    {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100",
                     "channels": 1, "bit_rate": "64000"}],
        "format": {"duration": "5400.25", "bit_rate": "700000", "size": "472000000",
                   "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
    })
    assert (info["width"], info["height"], info["duration"]) == (640, 360, 5400.25)
    assert info["video_codec"] == "h264" and info["audio_sample_rate"] == 44100


def test_signed_url_helpers():
    assert signed_url_expiry(SIGNED) == datetime(2100, 1, 1, tzinfo=UTC)
    assert signed_url_expiry("https://x/y.mp4") is None
    assert strip_query(SIGNED) == "https://dexample.cloudfront.net/u/rec.mp4"
    text = f"could not fetch '{SIGNED}' now"
    assert redact_urls(text) == ("could not fetch 'https://dexample.cloudfront.net/u/rec.mp4"
                                 "?<redacted>' now")
