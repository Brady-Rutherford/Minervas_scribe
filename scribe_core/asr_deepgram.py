"""Deepgram Nova-3: the default provider (plan phase 1.1).

Gives word timestamps + diarization clusters + keyterm prompting in one request for the whole
class (no chunk seams). Privacy stance: EU endpoint and ``mip_opt_out=true`` (opt out of the
Deepgram Model Improvement Program).

URL mode sends the signed CloudFront URL to Deepgram so it fetches the video itself. If that
fails (expired signature, blocked fetch), we fall back to uploading a locally extracted,
lightly cleaned Opus file. The signed URL is never logged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

from .asr_base import ASRError, ASRProvider, ASRResult
from .forum_events import kebab
from .redact import redact_urls, strip_query
from .text import is_filler

log = logging.getLogger(__name__)

DG_URL = "https://api.eu.deepgram.com/v1/listen"
KEY_ENV = "DEEPGRAM_API_KEY"
URL_ENV = "DEEPGRAM_API_URL"
# Deepgram caps keyterms at ~500 tokens / ~100 words. Stay under with names first.
KEYTERM_WORD_BUDGET = 90


# --------------------------------------------------------------------------- keyterms


def roster(klass: dict | None) -> list[dict]:
    """Normalise ``class-users``. The shape is unverified, so accept a nested ``user`` object
    OR flat ``first-name``/``last-name`` fields on the entry itself."""
    out = []
    for cu in kebab(klass or {}).get("class-users") or []:
        if not isinstance(cu, dict):
            continue
        user = cu.get("user") if isinstance(cu.get("user"), dict) else cu
        first = (user.get("first-name") or cu.get("first-name") or "").strip()
        last = (user.get("last-name") or cu.get("last-name") or "").strip()
        preferred = (user.get("preferred-name") or cu.get("preferred-name") or "").strip()
        uid = user.get("id", user.get("user-id", cu.get("user-id")))
        if uid is None and user is cu:
            uid = cu.get("id")
        out.append(
            {
                "user_id": None if uid is None else str(uid),
                "first": first,
                "last": last,
                "preferred": preferred,
                "name": " ".join(x for x in (preferred or first, last) if x),
                "role": (cu.get("role") or user.get("role") or "").lower() or None,
                "absent": bool(cu.get("absent", False)),
            }
        )
    return out


def build_keyterms(bundle_or_klass: dict, extra=(), budget: int = KEYTERM_WORD_BUDGET
                   ) -> list[str]:
    """Roster names first (that's where ASR fails hardest), then course + session titles, then
    ``extra``. Deduped (case-insensitive, order kept) and capped at ``budget`` words."""
    klass = bundle_or_klass.get("class") if "class" in bundle_or_klass else bundle_or_klass
    klass = kebab(klass or {})
    names = []
    for r in roster(klass):
        full = " ".join(x for x in (r["first"], r["last"]) if x)
        if full:
            names.append(full)
        if r["preferred"] and r["preferred"] != r["first"]:
            names.append(" ".join(x for x in (r["preferred"], r["last"]) if x))
    course = ((klass.get("section") or {}).get("course") or {}) if isinstance(
        klass.get("section"), dict) else {}
    terms = names + [course.get("title") or "", klass.get("title") or ""] + list(extra)
    out: list[str] = []
    seen: set[str] = set()
    left = budget
    for t in terms:
        t = " ".join(str(t).split())
        if not t or t.lower() in seen:
            continue
        n = len(t.split())
        if n > left:
            continue  # skip this one; a shorter later term may still fit
        seen.add(t.lower())
        out.append(t)
        left -= n
        if left <= 0:
            break
    return out


# --------------------------------------------------------------------------- request


def build_params(*, model: str = "nova-3", language: str = "multi", keyterms=(),
                 filler_words: bool = True, diarize: bool = True) -> list[tuple[str, str]]:
    """Query params as a list of pairs (``keyterm`` repeats once per term)."""
    params = [
        ("model", model),
        ("language", language),
        ("diarize", "true" if diarize else "false"),
        ("utterances", "true"),
        ("utt_split", "0.8"),
        ("smart_format", "true"),
        ("punctuate", "true"),
        ("paragraphs", "true"),
        ("mip_opt_out", "true"),
        ("filler_words", "true" if filler_words else "false"),
    ]
    params += [("keyterm", t) for t in keyterms]
    return params


def words_from_deepgram(resp: dict) -> list[dict]:
    try:
        alt = resp["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError, TypeError):
        raise ASRError("Unexpected Deepgram response shape (no channels/alternatives).") from None
    out = []
    for w in alt.get("words") or []:
        text = w.get("punctuated_word") or w.get("word") or ""
        out.append(
            {
                "start": float(w["start"]),
                "end": float(w["end"]),
                "text": text,
                "conf": w.get("confidence"),
                "cluster": w.get("speaker"),
                "cluster_conf": w.get("speaker_confidence"),
                "filler": is_filler(w.get("word") or text),
            }
        )
    return out


class DeepgramASR(ASRProvider):
    name = "deepgram"
    default_model = "nova-3"
    diarizes = True
    accepts_url = True

    def __init__(self, model: str | None = None, *, api_key: str | None = None,
                 endpoint: str | None = None, session: requests.Session | None = None,
                 timeout: float = 900.0, filler_words: bool = True):
        super().__init__(model)
        self.api_key = api_key or os.environ.get(KEY_ENV, "")
        self.endpoint = endpoint or os.environ.get(URL_ENV) or DG_URL
        self.session = session or requests.Session()
        self.timeout = timeout
        self.filler_words = filler_words

    def _post(self, params, **kwargs):
        headers = {"Authorization": f"Token {self.api_key}", **kwargs.pop("headers", {})}
        try:
            return self.session.post(self.endpoint, params=params, headers=headers,
                                     timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise ASRError(f"Deepgram request failed: {redact_urls(exc)}") from None

    def transcribe(self, *, audio_path=None, video_url=None, audio_fallback=None, keyterms=(),
                   language=None) -> ASRResult:
        if not self.api_key:
            raise ASRError(f"{KEY_ENV} is not set.")
        params = build_params(model=self.model, language=language or "multi",
                              keyterms=list(keyterms), filler_words=self.filler_words)
        mode = None
        resp_json = None
        url_error = None
        if video_url:
            log.info("Deepgram URL mode for %s", strip_query(video_url))
            try:
                r = self._post(params, json={"url": video_url})
            except ASRError as exc:  # network trouble: try the upload path
                r, url_error = None, str(exc)
            if r is not None:
                if r.status_code in (401, 403):
                    raise ASRError(f"Deepgram rejected the API key ({r.status_code}).")
                if r.ok:
                    resp_json, mode = r.json(), "url"
                else:
                    url_error = f"HTTP {r.status_code}: {redact_urls(r.text[:300])}"
            if resp_json is None:
                log.warning("Deepgram URL mode failed (%s); falling back to upload", url_error)
        if resp_json is None:
            path = audio_path
            if path is None and audio_fallback is not None:
                path = audio_fallback()
            if path is None:
                raise ASRError(f"Deepgram URL mode failed ({url_error}) and no audio to upload.")
            with open(Path(path), "rb") as fh:
                r = self._post(params, data=fh, headers={"Content-Type": "audio/ogg"})
            if not r.ok:
                raise ASRError(f"Deepgram upload failed: HTTP {r.status_code} "
                               f"{redact_urls(r.text[:300])}")
            resp_json, mode = r.json(), "upload"
        words = words_from_deepgram(resp_json)
        meta = resp_json.get("metadata") or {}
        results = resp_json.get("results") or {}
        return ASRResult(
            words=words,
            provider=self.name,
            model=self.model,
            language=language or "multi",
            duration=meta.get("duration"),
            diarized=True,
            mode=mode,
            meta={
                "request_id": meta.get("request_id"),
                "n_utterances": len(results.get("utterances") or []),
                "keyterms": len(keyterms),
                "url_mode_error": url_error,
            },
        )
