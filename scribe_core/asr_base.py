"""Shared ASR types. Words are plain dicts so calibration/fusion stay simple and JSON-friendly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict


class Word(TypedDict, total=False):
    start: float           # seconds into the video/audio
    end: float
    text: str              # punctuated token
    conf: float | None     # ASR confidence 0..1 (None when the provider has none)
    cluster: int | None    # anonymous diarization cluster (Deepgram only)
    cluster_conf: float | None
    filler: bool           # "um", "uh"... (kept for JSONL, dropped from readable exports)


@dataclass
class ASRResult:
    words: list[dict]
    provider: str
    model: str
    language: str | None = None
    duration: float | None = None
    diarized: bool = False
    mode: str = "upload"            # "url" (provider fetched the signed URL) or "upload"
    meta: dict = field(default_factory=dict)


class ASRError(RuntimeError):
    pass


class ASRProvider:
    """Interface. ``transcribe`` gets EITHER a local audio file OR a remote URL.

    ``audio_fallback`` (URL-capable providers only) lazily produces a local audio file if the
    provider can't fetch the URL (expired signature, blocked host...).
    """

    name = "base"
    default_model = ""
    diarizes = False
    accepts_url = False
    needs_audio = True  # False for replayed results (no audio / URL needed at all)

    def __init__(self, model: str | None = None):
        self.model = model or self.default_model

    def transcribe(
        self,
        *,
        audio_path: Path | None = None,
        video_url: str | None = None,
        audio_fallback: Callable[[], Path] | None = None,
        keyterms: list[str] | tuple = (),
        language: str | None = None,
    ) -> ASRResult:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "diarizes": self.diarizes}
