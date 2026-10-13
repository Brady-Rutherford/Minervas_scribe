"""Audio plumbing: per-job workdirs, streaming download, ffmpeg -> Opus, ffprobe, faststart.

Rules: the raw video never outlives its job (plan B2), and signed URLs never appear in logs
or error messages (query strings are stripped / redacted).
"""

from __future__ import annotations

import json
import logging
import shutil
import struct
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from .redact import redact_urls, strip_query

log = logging.getLogger(__name__)

# Plain transcode for Groq / local Whisper (plan B8): ~11 MB for 90 min at 16 kbps.
PLAIN_BITRATE = "16k"
# Deepgram upload path (phase 1.1): light cleanup that helps quiet laptop mics.
CLEAN_FILTER = "highpass=f=80,lowpass=f=7600,loudnorm=I=-18:TP=-2:LRA=11"
CLEAN_BITRATE = "24k"


class AudioError(RuntimeError):
    pass


@contextmanager
def job_workdir(job_id: str | None = None, root: str | Path | None = None, keep: bool = False
                ) -> Iterator[Path]:
    """A private temp directory per job, deleted on exit (with the video inside it)."""
    if root is not None:
        Path(root).mkdir(parents=True, exist_ok=True)
    prefix = f"scribe_{job_id}_" if job_id else "scribe_"
    d = Path(tempfile.mkdtemp(prefix=prefix, dir=str(root) if root else None))
    try:
        yield d
    finally:
        if not keep:
            shutil.rmtree(d, ignore_errors=True)


def signed_url_expiry(url: str | None) -> datetime | None:
    """CloudFront ``Expires=`` (epoch seconds) as a datetime, if present."""
    if not url:
        return None
    try:
        exp = parse_qs(urlsplit(url).query).get("Expires", [None])[0]
        return datetime.fromtimestamp(int(exp), tz=UTC) if exp else None
    except (ValueError, TypeError, OverflowError):
        return None


def download(
    url: str,
    dest: Path,
    *,
    progress: Callable[[float], None] | None = None,
    session: requests.Session | None = None,
    timeout: float = 60.0,
    chunk_size: int = 1 << 20,
) -> Path:
    """Stream ``url`` to ``dest`` (via ``.part`` + rename). ``progress`` gets 0..1."""
    sess = session or requests
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    try:
        with sess.get(url, stream=True, timeout=timeout) as r:
            if r.status_code >= 400:
                raise AudioError(
                    f"Video download failed with HTTP {r.status_code} for {strip_query(url)} "
                    "(signed URLs expire: fetch a fresh one)."
                )
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(part, "wb") as fh:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(min(1.0, done / total))
    except requests.RequestException as exc:
        part.unlink(missing_ok=True)
        raise AudioError(f"Video download failed: {redact_urls(exc)}") from None
    except AudioError:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return dest


# --------------------------------------------------------------------------- ffmpeg


def opus_cmd(src: str | Path, out: str | Path, *, clean: bool = False, bitrate: str | None = None,
             ffmpeg: str = "ffmpeg") -> list[str]:
    """ffmpeg command: 16 kHz mono Opus (optionally with the cleanup filter chain)."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-i", str(src), "-vn", "-ac", "1", "-ar", "16000"]
    if clean:
        cmd += ["-af", CLEAN_FILTER]
    cmd += ["-c:a", "libopus", "-b:a", bitrate or (CLEAN_BITRATE if clean else PLAIN_BITRATE),
            "-application", "voip", str(out)]
    return cmd


def cut_cmd(src: str | Path, out: str | Path, start: float, duration: float,
            ffmpeg: str = "ffmpeg") -> list[str]:
    """Copy a time window out of an Opus file (no re-encode; packet-accurate, ~20 ms)."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
            "-c", "copy", str(out)]


def run_tool(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run ffmpeg/ffprobe; errors carry a redacted command and stderr tail (no signed URLs)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise AudioError(f"{cmd[0]} not found on PATH (install ffmpeg).") from None
    except subprocess.TimeoutExpired:
        raise AudioError(f"{cmd[0]} timed out") from None
    if proc.returncode != 0:
        shown = " ".join(redact_urls(c) for c in cmd)
        tail = redact_urls((proc.stderr or "").strip()[-800:])
        raise AudioError(f"{cmd[0]} failed ({proc.returncode}): {shown}\n{tail}")
    return proc


def to_opus(src: str | Path, out: str | Path, *, clean: bool = False,
            bitrate: str | None = None) -> Path:
    """Extract mono 16 kHz Opus. ``src`` may be a local path or a URL (ffmpeg streams it)."""
    out = Path(out)
    if out.is_dir():
        out = out / ("audio_clean.ogg" if clean else "audio.ogg")
    run_tool(opus_cmd(src, out, clean=clean, bitrate=bitrate))
    return out


def split_for_limit(path: Path, max_bytes: int, duration: float, workdir: Path
                    ) -> list[tuple[Path, float]]:
    """Split an Opus file into back-to-back pieces under ``max_bytes`` (no overlap, so no
    duplicated words at seams, plan T6). Returns [(piece, offset_seconds)]."""
    size = Path(path).stat().st_size
    if size <= max_bytes or duration <= 0:
        return [(Path(path), 0.0)]
    n = int(size // max_bytes) + 1
    piece = duration / n
    out = []
    for i in range(n):
        dst = workdir / f"piece_{i:03d}.ogg"
        # Back-to-back windows; the last one runs a little long to catch rounding.
        run_tool(cut_cmd(path, dst, i * piece, piece if i < n - 1 else piece + 5.0))
        out.append((dst, i * piece))
    return out


# --------------------------------------------------------------------------- probing


def ffprobe_cmd(src: str | Path, ffprobe: str = "ffprobe") -> list[str]:
    return [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
            str(src)]


def parse_ffprobe(data: dict) -> dict:
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    fmt = data.get("format") or {}

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    return {
        "width": video.get("width"),
        "height": video.get("height"),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "audio_sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
        "audio_channels": audio.get("channels"),
        "audio_bit_rate": int(audio["bit_rate"]) if audio.get("bit_rate") else None,
        "duration": _f(fmt.get("duration")),
        "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "size": int(fmt["size"]) if fmt.get("size") else None,
        "format": fmt.get("format_name"),
    }


def mp4_top_level_boxes(read_at: Callable[[int, int], bytes], max_boxes: int = 64
                        ) -> list[tuple[str, int, int]]:
    """Walk top-level MP4 boxes: [(type, offset, size)]. Stops after moov+mdat are seen."""
    boxes: list[tuple[str, int, int]] = []
    offset = 0
    seen: set[str] = set()
    for _ in range(max_boxes):
        head = read_at(offset, 16)
        if len(head) < 8:
            break
        size, = struct.unpack(">I", head[:4])
        btype = head[4:8].decode("latin-1")
        if size == 1:
            if len(head) < 16:
                break
            size, = struct.unpack(">Q", head[8:16])
        elif size == 0:  # box runs to end of file
            boxes.append((btype, offset, 0))
            break
        if size < 8:
            break
        boxes.append((btype, offset, size))
        seen.add(btype)
        if {"moov", "mdat"} <= seen:
            break
        offset += size
    return boxes


def faststart_from_boxes(boxes: list[tuple[str, int, int]]) -> bool | None:
    order = [b[0] for b in boxes]
    if "moov" not in order or "mdat" not in order:
        return None
    return order.index("moov") < order.index("mdat")


def file_reader(path: str | Path) -> Callable[[int, int], bytes]:
    def read_at(offset: int, n: int) -> bytes:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(n)
    return read_at


def http_range_reader(url: str, session: requests.Session | None = None, timeout: float = 30.0
                      ) -> Callable[[int, int], bytes]:
    sess = session or requests

    def read_at(offset: int, n: int) -> bytes:
        headers = {"Range": f"bytes={offset}-{offset + n - 1}"}
        with sess.get(url, headers=headers, stream=True, timeout=timeout) as r:
            if r.status_code not in (200, 206):
                raise AudioError(f"Range request failed ({r.status_code}) for {strip_query(url)}")
            # A 200 means ranges are unsupported: read only what we need, then drop the stream.
            data = b""
            for chunk in r.iter_content(chunk_size=n):
                data += chunk
                if len(data) >= n:
                    break
            return data[:n]
    return read_at


def ffprobe_info(src: str | Path, *, check_faststart: bool = True, timeout: float = 120.0) -> dict:
    """Resolution, duration, codecs, and whether ``moov`` precedes ``mdat`` (faststart).

    faststart=True means ffmpeg can seek windows of the remote file cheaply via range requests.
    """
    proc = run_tool(ffprobe_cmd(src), timeout=timeout)
    info = parse_ffprobe(json.loads(proc.stdout or "{}"))
    info["faststart"] = None
    if check_faststart:
        s = str(src)
        try:
            remote = s.startswith(("http://", "https://"))
            reader = http_range_reader(s) if remote else file_reader(s)
            boxes = mp4_top_level_boxes(reader)
            info["faststart"] = faststart_from_boxes(boxes)
            info["top_level_boxes"] = [b[0] for b in boxes]
        except (AudioError, OSError, requests.RequestException) as exc:
            info["faststart_error"] = redact_urls(exc)
    return info


def audio_duration(path: str | Path) -> float | None:
    try:
        return parse_ffprobe(json.loads(run_tool(ffprobe_cmd(path)).stdout or "{}"))["duration"]
    except (AudioError, ValueError):
        return None
