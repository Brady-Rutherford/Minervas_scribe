"""End-to-end orchestration: bundle -> (signed URL | audio) -> ASR -> calibrate -> fuse ->
turns -> transcript v2 -> exports (+ debug timeline).

``transcribe_bundle`` is host-agnostic (the local server calls it with a bundle + signed URL
posted by the extension). ``transcribe_class`` wraps it for the CLI, fetching the bundle and a
fresh signed URL with a ``ForumClient``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .asr import ASRProvider, select_provider
from .asr_deepgram import build_keyterms
from .audio import download as download_file
from .audio import job_workdir, signed_url_expiry, to_opus
from .calibrate import calibrate, make_shift, shift
from .export import DEFAULT_FORMATS, parse_formats, write_exports
from .export.debug_html import write_debug_html
from .fixup import find_shaky_spans, name_fixup
from .forum_events import (
    kebab,
    main_room_info,
    normalize_events,
    other_events,
    parse_voice_events,
    recording_timeline,
    teacher_id,
    timeline_duration,
    timeline_segments,
    timing_precision,
    voice_stats,
    wall_to_video,
)
from .fuse import PROFESSOR, calibration_words, cluster_to_user, fuse, how_mix, smooth
from .privacy import pseudonym
from .redact import redact_urls
from .schema import build_transcript, class_block
from .timeutil import epoch_ms
from .turns import build_turns

log = logging.getLogger(__name__)

Progress = Callable[[str, str, float | None], None]


class PipelineError(RuntimeError):
    pass


@dataclass
class TranscribeOptions:
    provider: str | None = None          # None/"auto": deepgram > groq > local
    model: str | None = None
    privacy: str = "names"               # names | ids | both
    formats: tuple = DEFAULT_FORMATS
    res: str = "lowres"                  # lowres | highres
    strict_res: bool = False             # True: raise instead of falling back to the other res
    language: str | None = None          # deepgram default "multi"; whisper auto-detect
    keyterms: bool = True
    extra_keyterms: tuple = ()
    url_mode: bool = True                # let Deepgram fetch the signed URL itself
    name_fixup: bool = False
    debug_html: bool = True
    save_asr: bool = True                # *_asr.json: re-run alignment later without paying ASR
    keep_workdir: bool = False
    workdir_root: Path | None = None
    salt: str | None = None
    extra: dict = field(default_factory=dict)


def _noop(stage: str, message: str, fraction: float | None = None) -> None:
    pass


def slug(text: str | None, fallback: str = "class") -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-")
    return s[:40] or fallback


def output_basename(bundle: dict) -> str:
    c = class_block(bundle.get("class"), bundle.get("class_id"))
    parts = [slug(c.get("course_code"), ""), (c.get("date") or "")[:10], f"class{c['id']}"]
    return "_".join(p for p in parts if p)


def _modes(privacy: str) -> list[str]:
    if privacy == "both":
        return ["names", "ids"]
    if privacy not in ("names", "ids"):
        raise PipelineError("privacy must be names, ids or both")
    return [privacy]


def transcribe_bundle(
    bundle: dict,
    *,
    out_dir: Path,
    video_url: str | None = None,
    fresh_url: Callable[[], str] | None = None,
    audio_path: Path | None = None,
    provider: ASRProvider | None = None,
    options: TranscribeOptions | None = None,
    me_id=None,
    progress: Progress | None = None,
    job_id: str | None = None,
    asr_lock=None,
    hand_log: list[dict] | None = None,
    source_res: str | Callable[[], str | None] | None = None,
) -> dict:
    """Run the full v3 pipeline for one class. Returns {"files", "summary", "transcripts"}.

    ``hand_log``: optional entries from extension_v3's "log my own hand raises" export
    (``{"t": epoch_ms, "classId": ...}``); they become ``hand-raise-key`` events for you only.

    ``source_res``: the recording rendition the audio came from ("lowres" | "highres"), or a
    callable read after ASR (``transcribe_class`` passes one, because the signed URL is only
    fetched right before use). Recorded as ``asr.source_res`` in the transcript and in
    ``*_asr.json``; when None, a replayed ASR result's own ``source_res`` is kept.
    """
    opts = options or TranscribeOptions()
    progress = progress or _noop
    out_dir = Path(out_dir)
    if not isinstance(bundle.get("class"), dict):
        raise PipelineError("The bundle has no class data (class_grader/classes/{id} failed).")
    # Forum sends kebab-case or snake_case keys depending on request headers: normalise once.
    klass = kebab(bundle["class"])
    events = normalize_events(bundle.get("events") if isinstance(bundle.get("events"), list)
                              else [])
    bundle = {**bundle, "class": klass, "events": events}
    modes = _modes(opts.privacy)
    names_wanted = "names" in modes
    warnings: list[str] = []

    # ---- Forum side ----------------------------------------------------------------------
    segs = recording_timeline(klass)
    if not segs:
        warnings.append("No recording sessions with a start time; Forum timing unavailable.")
    elif len(segs) > 1:
        warnings.append(f"{len(segs)} recording sessions; assumed concatenated in order "
                        "(check the calibration status).")
    room, room_method = main_room_info(events)
    voice_raw = parse_voice_events(events, segs, room_id=room, include_names=names_wanted)
    precision = timing_precision(voice_raw)
    if not voice_raw:
        warnings.append("No main-room Forum voice events; attribution relies on diarization.")
    elif precision == "s":
        warnings.append("Forum voice timing has whole-second precision (older class): "
                        "turn edges are approximate.")
    if room_method == "most-common-voice-room":
        warnings.append("No 'Main Classroom' room signature found; main room guessed as the "
                        "most common voice room.")
    voice_names = {v["user_id"]: v.get("name") for v in voice_raw if v.get("name")}
    breakout = {}
    for (uid, rm), secs in voice_stats(events)["per_user_room"].items():
        if rm != str(room):
            breakout[uid] = breakout.get(uid, 0.0) + secs
    all_terms = build_keyterms(bundle, opts.extra_keyterms)
    keyterms = all_terms if opts.keyterms else list(opts.extra_keyterms)

    # ---- ASR -----------------------------------------------------------------------------
    prov = provider or select_provider(opts.provider, opts.model)
    if not prov.diarizes:
        warnings.append(f"{prov.name} has no diarization: speakers come from Forum voice "
                        "events only (weaker in crosstalk).")

    def current_url() -> str | None:
        url = fresh_url() if fresh_url else video_url
        exp = signed_url_expiry(url)
        if exp is not None and exp <= datetime.now(tz=UTC):
            raise PipelineError("The signed video URL has expired. Start again to get a fresh one.")
        return url

    with job_workdir(job_id, opts.workdir_root, keep=opts.keep_workdir) as wd:
        clean = prov.name == "deepgram"

        def materialize_audio() -> Path:
            if audio_path is not None:
                progress("preparing_audio", "Converting audio …", None)
                return to_opus(Path(audio_path), wd, clean=clean)
            url = current_url()
            if not url:
                raise PipelineError("No video URL or audio file to transcribe.")
            progress("downloading", "Downloading video …", 0.0)
            video = download_file(url, wd / "input.mp4",
                                  progress=lambda f: progress("downloading",
                                                              "Downloading video …", f))
            progress("preparing_audio", "Extracting audio …", None)
            try:
                return to_opus(video, wd, clean=clean)
            finally:
                video.unlink(missing_ok=True)  # the raw video never outlives this step

        use_url = (prov.accepts_url and opts.url_mode and audio_path is None
                   and (video_url or fresh_url))
        lock = asr_lock if (asr_lock is not None and prov.name == "local") else nullcontext()
        try:
            if not prov.needs_audio:
                result = prov.transcribe(keyterms=keyterms, language=opts.language)
            elif use_url:
                progress("transcribing", f"Transcribing with {prov.name} (URL mode) …", None)
                with lock:
                    result = prov.transcribe(video_url=current_url(),
                                             audio_fallback=materialize_audio,
                                             keyterms=keyterms, language=opts.language)
            else:
                audio = materialize_audio()  # before the lock: signed URLs expire
                progress("transcribing", f"Transcribing with {prov.name} …", None)
                with lock:
                    result = prov.transcribe(audio_path=audio, keyterms=keyterms,
                                             language=opts.language)
        except PipelineError:
            raise
        except Exception as exc:
            raise PipelineError(f"Transcription failed: {redact_urls(exc)}") from None

    # ---- alignment -------------------------------------------------------------------------
    progress("processing", "Calibrating clocks and attributing speakers …", None)
    words = sorted(result.words, key=lambda w: (w["start"], w["end"]))
    dur = max([result.duration or 0.0, timeline_duration(segs) or 0.0]
              + [w["end"] for w in words[-1:]])
    cal = calibrate(calibration_words(words, voice_raw), voice_raw, dur or None)
    if not cal["applied"]:
        warnings.append("Timing unverified: " + "; ".join(cal.get("notes") or []))
    voice = shift(voice_raw, cal)
    report: dict = {}
    # Forum never logs the professor's mic, so "no student mic on" points at the professor.
    # With no student voice events at all that says nothing, so nobody is labelled professor.
    teacher = (teacher_id(bundle) or PROFESSOR) if voice else None
    mapping = (cluster_to_user(words, voice, teacher=teacher, report=report)
               if result.diarized else {})
    fused = smooth(fuse(words, voice, mapping, teacher=teacher))
    fixups: list = []
    if opts.name_fixup:
        fused, fixups = name_fixup(fused, all_terms)
    turns = build_turns(fused)
    shift_t = make_shift(cal)
    chapters = [{**c, "t": round(shift_t(c["t"]), 3),
                 "end": None if c["end"] is None else round(shift_t(c["end"]), 3)}
                for c in timeline_segments(events, segs)]
    events_out = [{**e, "t": None if e["t"] is None else round(shift_t(e["t"]), 3)}
                  for e in other_events(events, segs)]
    me = me_id if me_id is not None else (bundle.get("me") or {}).get("id")
    for entry in hand_log or []:
        cid = entry.get("classId", entry.get("class_id"))
        if cid not in (None, "") and str(cid) != str(klass.get("id", bundle.get("class_id"))):
            continue
        t_ms = epoch_ms(entry.get("t"))
        t = wall_to_video(t_ms, segs) if t_ms is not None else None
        if t is not None:
            events_out.append({"t": round(shift_t(t), 3), "type": "hand-raise-key",
                               "user_id": None if me is None else str(me), "room_id": None,
                               "text": None, "private": False})
    events_out.sort(key=lambda e: (e["t"] is None, e["t"] or 0.0))
    res_used = source_res() if callable(source_res) else source_res
    if res_used is None:
        res_used = result.meta.get("source_res")
    asr_info = {"provider": result.provider, "model": result.model, "language": result.language,
                "mode": result.mode, "diarized": result.diarized, "duration": result.duration,
                "n_words": len(words), "keyterms": len(keyterms),
                "url_mode_error": result.meta.get("url_mode_error"), "source_res": res_used}

    # ---- outputs ---------------------------------------------------------------------------
    progress("compiling", "Writing outputs …", None)
    base = output_basename(bundle)
    files: list[Path] = []
    transcripts = {}
    export_notes: list[str] = []
    mix = how_mix(fused)
    for mode in modes:
        salt = opts.salt if mode == "ids" else None
        if mode == "ids" and not salt:
            raise PipelineError("ids mode needs a salt (TranscribeOptions.salt)")
        t = build_transcript(
            bundle=bundle, words=fused, turns=turns, voice=voice, chapters=chapters,
            events=events_out, calibration=cal, mapping=mapping, asr_info=asr_info,
            privacy=mode, salt=salt, me_id=me, teacher_id=teacher, main_room_id=room,
            main_room_method=room_method, timing_precision=precision,
            voice_names=voice_names if mode == "names" else None, breakout_seconds=breakout,
            how_mix=mix, shaky_spans=find_shaky_spans(fused), fixups=fixups,
            warnings=warnings, cluster_report=report.get("clusters"),
        )
        transcripts[mode] = t
        files += write_exports(t, out_dir, f"{base}_{mode}", parse_formats(opts.formats),
                               skipped=export_notes)
        if opts.debug_html:
            def label(uid, _t=t, _mode=mode, _salt=salt):
                if uid is None:
                    return "Unattributed"
                k = str(uid) if _mode == "names" else pseudonym(uid, _salt)
                return (_t["speakers"].get(k) or {}).get("name") or k
            files.append(write_debug_html(
                out_dir / f"{base}_{mode}_debug.html", title=t["class"]["title"],
                voice_raw=voice_raw, voice_cal=voice, words=fused, turns=turns,
                calibration=cal, mapping=mapping, label=label))
    if opts.save_asr:
        asr_path = out_dir / f"{base}_asr.json"
        asr_path.write_text(json.dumps({
            "provider": result.provider, "model": result.model, "language": result.language,
            "duration": result.duration, "diarized": result.diarized, "source_res": res_used,
            "words": result.words,
        }, ensure_ascii=False), encoding="utf-8")
        files.append(asr_path)
    first = transcripts[modes[0]]
    summary = {
        "class_id": first["class"]["id"],
        "title": first["class"]["title"],
        "timing": first["alignment"]["timing"],
        "calibration_status": cal["status"],
        "offset_s": round(cal["offset"], 3),
        "drift": cal["drift"],
        "how_mix": mix,
        "n_utterances": len(first["utterances"]),
        "n_words": len(words),
        "provider": result.provider,
        "asr_mode": result.mode,
        "warnings": warnings + sorted(set(export_notes)),
    }
    progress("complete", "Transcription complete!", 1.0)
    return {"files": files, "summary": summary, "transcripts": transcripts}


def transcribe_class(class_id, client, *, out_dir: Path, options: TranscribeOptions | None = None,
                     bundle: dict | None = None, audio_path: Path | None = None,
                     provider: ASRProvider | None = None, progress: Progress | None = None,
                     hand_log: list[dict] | None = None) -> dict:
    """CLI path: fetch the bundle and (right before use) a fresh signed URL via ForumClient."""
    opts = options or TranscribeOptions()
    progress = progress or _noop
    progress("fetching", "Fetching class data from Forum …", None)
    if client is None and bundle is None:
        raise PipelineError("Offline mode needs a synced bundle (--bundle-dir).")
    if bundle is None:
        bundle = client.class_bundle(class_id)
    me = client.me() if client is not None else dict(bundle.get("me") or {})
    bundle["me"] = {"id": me.get("id"), "role": me.get("role")}
    used: dict = {}

    def fresh_url() -> str:
        urls = client.recording_urls(class_id)
        res = opts.res
        url = urls.get(res)
        if not url and not opts.strict_res:
            res = "highres" if opts.res == "lowres" else "lowres"
            url = urls.get(res)
            if url:
                log.warning("No %s recording for class %s; using %s (strict_res is off).",
                            opts.res, class_id, res)
        if not url and opts.strict_res:
            raise PipelineError(f"Forum returned no {opts.res} recording URL for class "
                                f"{class_id} (strict_res: no fallback to the other rendition).")
        if not url:
            raise PipelineError(f"Forum returned no recording URL for class {class_id}.")
        used["res"] = res
        return url

    online = client is not None and audio_path is None
    return transcribe_bundle(bundle, out_dir=out_dir, fresh_url=fresh_url if online else None,
                             audio_path=audio_path, provider=provider, options=opts,
                             me_id=me.get("id"), progress=progress, hand_log=hand_log,
                             source_res=lambda: used.get("res"))
