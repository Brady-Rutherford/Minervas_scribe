"""ASR provider registry (plan phase 1.9). Deepgram first; Groq and local are fallbacks.

With ``groq`` or ``local`` there is no diarization, so fusion runs on Forum signals only and
attribution is weaker in crosstalk.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .asr_base import ASRError, ASRProvider, ASRResult, Word
from .asr_deepgram import DeepgramASR
from .audio import audio_duration, split_for_limit
from .text import is_filler, normalize_spacing, transfer_punctuation

__all__ = [
    "ASRError", "ASRProvider", "ASRResult", "Word", "DeepgramASR", "GroqASR", "LocalWhisperASR",
    "PROVIDERS", "resolve_provider_name", "select_provider",
]

log = logging.getLogger(__name__)


def _as_dict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "to_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - SDK objects vary
                pass
    return dict(getattr(obj, "__dict__", {}) or {})


def parse_whisper_verbose(resp, offset: float = 0.0) -> tuple[list[dict], list[dict], dict]:
    """Parse an OpenAI/Groq ``verbose_json`` response with word + segment timestamps."""
    d = _as_dict(resp)
    words = []
    for w in d.get("words") or []:
        wd = _as_dict(w)
        text = str(wd.get("word", wd.get("text", ""))).strip()
        if not text:
            continue
        words.append({"start": float(wd["start"]) + offset, "end": float(wd["end"]) + offset,
                      "text": text, "conf": None, "cluster": None, "cluster_conf": None,
                      "filler": is_filler(text)})
    segments = []
    for s in d.get("segments") or []:
        sd = _as_dict(s)
        segments.append({"start": float(sd.get("start", 0.0)) + offset,
                         "end": float(sd.get("end", 0.0)) + offset,
                         "text": normalize_spacing(str(sd.get("text", "")).strip())})
    transfer_punctuation(words, segments)
    return words, segments, {"language": d.get("language"), "duration": d.get("duration")}


def _prompt_from_keyterms(keyterms, max_chars: int = 800) -> str | None:
    text = ", ".join(keyterms)
    return text[:max_chars] or None


class GroqASR(ASRProvider):
    """Groq Whisper (no diarization). Respects the ``model`` argument (plan B3)."""

    name = "groq"
    default_model = "whisper-large-v3-turbo"
    max_bytes = 24 * 1024 * 1024  # Groq free-tier upload cap is 25 MB

    def __init__(self, model: str | None = None, *, api_key: str | None = None, client=None):
        super().__init__(model)
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self._client = client

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise ASRError("GROQ_API_KEY is not set.")
            from groq import Groq  # lazy: optional extra `groq`
            self._client = Groq(api_key=self.api_key)
        return self._client

    def transcribe(self, *, audio_path=None, video_url=None, audio_fallback=None, keyterms=(),
                   language=None) -> ASRResult:
        path = audio_path or (audio_fallback() if audio_fallback else None)
        if path is None:
            raise ASRError("Groq needs a local audio file.")
        path = Path(path)
        lang = None if language in (None, "multi") else language
        prompt = _prompt_from_keyterms(keyterms)
        client = self._get_client()
        duration = audio_duration(path) or 0.0
        words: list[dict] = []
        meta: dict = {"pieces": 0}
        for piece, offset in split_for_limit(path, self.max_bytes, duration, path.parent):
            kwargs = dict(file=(piece.name, piece.read_bytes()), model=self.model,
                          response_format="verbose_json",
                          timestamp_granularities=["word", "segment"], temperature=0.0)
            if lang:
                kwargs["language"] = lang
            if prompt:
                kwargs["prompt"] = prompt
            resp = client.audio.transcriptions.create(**kwargs)
            w, _segs, info = parse_whisper_verbose(resp, offset)
            words.extend(w)
            meta["pieces"] += 1
            meta.setdefault("language", info.get("language"))
        return ASRResult(words=words, provider=self.name, model=self.model,
                         language=lang or meta.get("language"), duration=duration or None,
                         diarized=False, mode="upload", meta=meta)


class LocalWhisperASR(ASRProvider):
    """Offline faster-whisper (no diarization). Private, slower."""

    name = "local"
    default_model = "medium"

    def __init__(self, model: str | None = None, *, compute_type: str = "int8",
                 device: str = "auto"):
        super().__init__(model)
        self.compute_type = compute_type
        self.device = device
        self._model = None

    def transcribe(self, *, audio_path=None, video_url=None, audio_fallback=None, keyterms=(),
                   language=None) -> ASRResult:
        path = audio_path or (audio_fallback() if audio_fallback else None)
        if path is None:
            raise ASRError("Local Whisper needs a local audio file.")
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy: optional extra `local`
            self._model = WhisperModel(self.model, device=self.device,
                                       compute_type=self.compute_type)
        lang = None if language in (None, "multi") else language
        segments, info = self._model.transcribe(
            str(path), language=lang, word_timestamps=True, vad_filter=True,
            initial_prompt=_prompt_from_keyterms(keyterms),
        )
        words = []
        for seg in segments:
            for w in seg.words or []:
                text = (w.word or "").strip()
                if text:
                    words.append({"start": float(w.start), "end": float(w.end), "text": text,
                                  "conf": float(w.probability), "cluster": None,
                                  "cluster_conf": None, "filler": is_filler(text)})
        return ASRResult(words=words, provider=self.name, model=self.model,
                         language=getattr(info, "language", lang),
                         duration=getattr(info, "duration", None), diarized=False, mode="upload")


PROVIDERS: dict[str, type[ASRProvider]] = {
    "deepgram": DeepgramASR,   # default: words + diarization + keyterms
    "groq": GroqASR,           # fallback (word timestamps, no diarization)
    "local": LocalWhisperASR,  # offline / privacy mode (no diarization)
}


def resolve_provider_name(name: str | None = None, env=None) -> str:
    """Explicit choice > DEEPGRAM_API_KEY > GROQ_API_KEY > local."""
    env = os.environ if env is None else env
    if name and name != "auto":
        if name not in PROVIDERS:
            raise ASRError(f"Unknown ASR provider {name!r}; choose from {sorted(PROVIDERS)}")
        return name
    if env.get("DEEPGRAM_API_KEY"):
        return "deepgram"
    if env.get("GROQ_API_KEY"):
        return "groq"
    return "local"


def select_provider(name: str | None = None, model: str | None = None, env=None) -> ASRProvider:
    return PROVIDERS[resolve_provider_name(name, env)](model)
