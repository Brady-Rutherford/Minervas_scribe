"""`scribe` command line (local only).

    scribe me
    scribe sync [--out DIR] [--sections past,current] [--limit N]
    scribe census (--class ID | --dir DIR) [--json] [--sample]
    scribe probe --class ID [--res lowres|highres]
    scribe transcribe --class ID [--provider deepgram|groq|local] [--privacy names|ids|both]
                      [--formats json,csv,md,html,jsonl,pdf] [--res lowres|highres] ...

Auth: FORUM_COOKIE (full Cookie header string) from the environment or a git-ignored .env.
The cookie is only ever sent to forum.minerva.edu. Signed video URLs are never printed.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from scribe_core import __version__
from scribe_core.asr_base import ASRProvider, ASRResult
from scribe_core.export import parse_formats
from scribe_core.forum import (
    COOKIE_ENV,
    ForumAuthError,
    ForumClient,
    ForumError,
    event_census,
    merge_census,
)
from scribe_core.forum_events import kebab, main_room_info, normalize_events, recording_bounds
from scribe_core.pipeline import PipelineError, TranscribeOptions, transcribe_class
from scribe_core.privacy import ensure_private_dir, load_or_create_salt
from scribe_core.timeutil import now_iso

DEFAULT_DATA = Path("scribe_data")
BUNDLE_FILES = {
    "class": "class.json",
    "classroom": "classroom.json",
    "listing": "listing.json",  # written by sync from the section listing; has teacher-user-id
    "events": "events.json",
    "polls": "polls.json",
    "poll_responses": "poll_responses.json",
    "outcome_assessments": "outcome_assessments.json",
    "focused_outcomes": "focused_outcomes.json",
    "breakouts": "breakouts.json",
    "analytics": "analytics.json",
}
MANIFEST_COLUMNS = ["class_id", "section_id", "course_code", "term", "title", "date",
                    "recording_started", "recording_ended", "n_voice", "n_my_voice",
                    "n_events_by_type"]


# --------------------------------------------------------------------------- parser


def _csv_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _sections_arg(value: str) -> list[str]:
    kinds = _csv_list(value)
    bad = [k for k in kinds if k not in ("past", "current")]
    if bad or not kinds:
        raise argparse.ArgumentTypeError("use past, current or past,current")
    return kinds


def _formats_arg(value: str) -> list[str]:
    try:
        return parse_formats(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scribe", description="Minerva's Scribe v3 (local CLI)")
    p.add_argument("--version", action="version", version=f"scribe {__version__}")
    p.add_argument("--env-file", type=Path, help="dotenv file (default: ./.env if present)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("me", help="confirm the cookie works (prints name, id, role)")

    s = sub.add_parser("sync", help="harvest metadata for every class (no video)")
    s.add_argument("--out", type=Path, default=DEFAULT_DATA)
    s.add_argument("--sections", type=_sections_arg, default=["past", "current"])
    s.add_argument("--limit", type=int, default=0, help="max new classes to fetch this run")
    s.add_argument("--no-extras", action="store_true",
                   help="only class + class-events (skip polls/outcomes)")

    c = sub.add_parser("census", help="event-type census")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--class", dest="class_id", type=int)
    g.add_argument("--dir", type=Path, help="a `scribe sync` output dir")
    c.add_argument("--json", action="store_true")
    c.add_argument("--sample", action="store_true", help="include one redacted sample per type")

    cp = sub.add_parser("caps", help="which optional endpoints your role can read")
    cp.add_argument("--class", dest="class_id", type=int, required=True)
    cp.add_argument("--section", dest="section_id", type=int)

    pr = sub.add_parser("probe", help="ffprobe the class recording (URL is never printed)")
    pr.add_argument("--class", dest="class_id", type=int, required=True)
    pr.add_argument("--res", choices=["lowres", "highres"], default="lowres")

    t = sub.add_parser("transcribe", help="transcribe one class")
    t.add_argument("--class", dest="class_id", type=int, required=True)
    t.add_argument("--provider", choices=["auto", "deepgram", "groq", "local"], default="auto")
    t.add_argument("--model", help="provider model (nova-3 / whisper-large-v3-turbo / medium)")
    t.add_argument("--privacy", choices=["names", "ids", "both"], default="names")
    t.add_argument("--formats", type=_formats_arg, default=["json", "csv", "md", "html", "jsonl"])
    t.add_argument("--res", choices=["lowres", "highres"], default="lowres")
    t.add_argument("--language", help="deepgram: multi (default) or en; whisper: en / auto")
    t.add_argument("--out", type=Path, default=DEFAULT_DATA / "transcripts")
    t.add_argument("--bundle-dir", type=Path,
                   help="use a synced raw/<class_id> dir instead of refetching class data")
    t.add_argument("--audio", type=Path, help="local audio/video file instead of the recording")
    t.add_argument("--reuse-asr", type=Path,
                   help="reuse a saved *_asr.json (skips ASR; re-runs alignment only)")
    t.add_argument("--keyterm", action="append", default=[], help="extra keyterm (repeatable)")
    t.add_argument("--no-keyterms", action="store_true", help="don't send roster keyterms")
    t.add_argument("--no-url-mode", action="store_true",
                   help="deepgram: always download + upload instead of sending the signed URL")
    t.add_argument("--name-fixup", action="store_true",
                   help="constrained roster-name fix-up of low-confidence words")
    t.add_argument("--no-debug", action="store_true", help="skip the debug timeline HTML")
    t.add_argument("--hand-log", type=Path,
                   help="extension_v3 'my hand raises' export (JSON) to add to the timeline")
    return p


# --------------------------------------------------------------------------- helpers


def _load_env(env_file: Path | None) -> None:
    from dotenv import find_dotenv, load_dotenv

    path = str(env_file) if env_file else find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)
        _warn_if_tracked(Path(path))


def _warn_if_tracked(path: Path) -> None:
    """Warn when the .env holding FORUM_COOKIE isn't git-ignored."""
    try:
        proc = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=path.parent,
                              capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return
    if proc.returncode == 1:
        print(f"warning: {path} is NOT git-ignored. It holds credentials: add `.env` to "
              ".gitignore before committing.", file=sys.stderr)


def _write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _course(section: dict) -> dict:
    course = section.get("course")
    return course if isinstance(course, dict) else {}


def _term(course: dict):
    term = course.get("term")
    if isinstance(term, dict):
        return term.get("name") or term.get("title") or term.get("id")
    return term


def load_bundle_dir(d: Path) -> dict:
    bundle = {"schema": "scribe.bundle/v1", "class_id": int(d.name) if d.name.isdigit() else None}
    for key, fname in BUNDLE_FILES.items():
        bundle[key] = _read_json(d / fname)
    return bundle


class SavedASR(ASRProvider):
    """Replays a saved ASR result (``*_asr.json``) so alignment can be re-run for free."""

    name = "saved"
    accepts_url = False
    needs_audio = False

    def __init__(self, path: Path):
        data = _read_json(path, {})
        super().__init__(data.get("model"))
        self.data = data
        self.diarizes = bool(data.get("diarized"))
        self.name = data.get("provider") or "saved"

    def transcribe(self, *, audio_path=None, video_url=None, audio_fallback=None, keyterms=(),
                   language=None) -> ASRResult:
        d = self.data
        return ASRResult(words=d.get("words") or [], provider=self.name, model=self.model,
                         language=d.get("language"), duration=d.get("duration"),
                         diarized=self.diarizes, mode="saved",
                         meta={"source_res": d.get("source_res")})


# --------------------------------------------------------------------------- commands


def cmd_me(args, client: ForumClient) -> int:
    me = client.me()
    name = " ".join(x for x in (me.get("first-name"), me.get("last-name")) if x) or "(no name)"
    print(f"{name}  id={me.get('id')}  role={me.get('role')}")
    return 0


def _manifest_row(cdir: Path, me_id) -> dict:
    listing = _read_json(cdir / "listing.json", {}) or {}  # our own file: snake_case keys
    item = kebab(listing.get("class") or {})              # Forum's class item: normalise
    klass = kebab(_read_json(cdir / "class.json") or item)
    events = normalize_events(_read_json(cdir / "events.json") or [])
    started, ended, _ = recording_bounds(klass if klass.get("recording-sessions") else item)
    counts: dict[str, int] = {}
    for ev in events:
        counts[str(ev.get("event-type"))] = counts.get(str(ev.get("event-type")), 0) + 1
    voice = [ev for ev in events if ev.get("event-type") == "voice"]
    mine = sum(1 for ev in voice if me_id is not None and str(ev.get("actor-id")) == str(me_id))
    return {
        "class_id": cdir.name,
        "section_id": listing.get("section_id"),
        "course_code": listing.get("course_code"),
        "term": listing.get("term"),
        "title": item.get("title") or klass.get("title"),
        "date": item.get("date") or klass.get("date"),
        "recording_started": started,
        "recording_ended": ended,
        "n_voice": len(voice),
        "n_my_voice": mine,
        "n_events_by_type": json.dumps(dict(sorted(counts.items())), separators=(",", ":")),
    }


def write_manifest(out: Path, me_id) -> Path:
    rows = []
    raw = out / "raw"
    if raw.exists():
        for cdir in sorted(raw.iterdir(), key=lambda p: (len(p.name), p.name)):
            if (cdir / "_complete.json").exists():
                rows.append(_manifest_row(cdir, me_id))
    path = out / "manifest.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def cmd_sync(args, client: ForumClient) -> int:
    out = ensure_private_dir(args.out)
    raw = out / "raw"
    raw.mkdir(exist_ok=True)
    me = client.me()
    _write_json(out / "me.json", {"id": me.get("id"), "role": me.get("role")})

    sections: dict = {}
    for kind in args.sections:
        for sec in client.sections(past=(kind == "past")):
            if isinstance(sec, dict) and sec.get("id") is not None:
                sections.setdefault(sec["id"], {**sec, "_kind": kind})
    _write_json(out / "sections.json", list(sections.values()))
    print(f"{len(sections)} sections")

    todo = []
    for sid, sec in sections.items():
        course = _course(sec)
        items = client.classes(sid)
        print(f"  section {sid} {course.get('course-code') or ''}: {len(items)} classes")
        for item in items:
            if isinstance(item, dict) and item.get("id") is not None:
                todo.append((sid, course, item))

    fetched = skipped = failed = auth_streak = 0
    total = len(todo)
    for i, (sid, course, item) in enumerate(todo, 1):
        cid = int(item["id"])
        cdir = raw / str(cid)
        if (cdir / "_complete.json").exists():
            skipped += 1
            continue
        if args.limit and fetched >= args.limit:
            print(f"limit of {args.limit} new classes reached; run again to continue")
            break
        cdir.mkdir(parents=True, exist_ok=True)
        _write_json(cdir / "listing.json", {"section_id": sid,
                                            "course_code": course.get("course-code"),
                                            "term": _term(course), "class": item})
        try:
            bundle = client.class_bundle(cid, extras=not args.no_extras)
            auth_streak = 0
        except ForumError as exc:
            if isinstance(exc, ForumAuthError):
                auth_streak += 1
                # 401/redirect = dead cookie. Repeated 403s = probably dead too. Stop early
                # instead of hammering Forum with hundreds of doomed requests.
                if exc.status != 403 or auth_streak >= 3:
                    raise
            failed += 1
            _write_json(cdir / "_error.json", {"at": now_iso(), "error": str(exc)})
            print(f"[{i}/{total}] class {cid}: FAILED ({exc})")
            continue
        for key, fname in BUNDLE_FILES.items():
            if bundle.get(key) is not None:
                _write_json(cdir / fname, bundle[key])
        events = bundle.get("events") or []
        _write_json(cdir / "_complete.json", {"fetched_at": bundle["fetched_at"],
                                              "status": bundle["status"]})
        (cdir / "_error.json").unlink(missing_ok=True)
        fetched += 1
        n_voice = sum(1 for e in events if e.get("event-type") == "voice")
        print(f"[{i}/{total}] class {cid} {course.get('course-code') or ''} "
              f"{(item.get('title') or '')[:50]!r}: {len(events)} events, {n_voice} voice")

    path = write_manifest(out, me.get("id"))
    print(f"done: {fetched} fetched, {skipped} already complete, {failed} failed -> {path}")
    return 0 if not failed else 1


def _print_census(census: dict, aggregated: bool) -> None:
    width = max([len(k) for k in census] + [10])
    head = f"{'event-type':<{width}}  {'count':>7}" + ("  classes" if aggregated else "")
    print(head + "  event-data keys")
    for etype, row in census.items():
        keys = row["data_keys"] if aggregated else list(row["data_keys"].keys())
        extra = f"  {row['classes']:>7}" if aggregated else ""
        print(f"{etype:<{width}}  {row['count']:>7}{extra}  {', '.join(keys)}")


def cmd_census(args, client: ForumClient | None) -> int:
    if args.dir:
        per_class = []
        for f in sorted((args.dir / "raw").glob("*/events.json")):
            per_class.append(event_census(_read_json(f, []) or []))
        census = merge_census(per_class)
        print(json.dumps(census, indent=1)) if args.json else _print_census(census, True)
        print(f"({len(per_class)} classes)", file=sys.stderr)
        return 0
    raw = client.class_events(args.class_id)
    census = event_census(raw, include_sample=args.sample)
    rooms: dict[str, int] = {}
    for ev in normalize_events(raw):
        if ev.get("event-type") == "voice":
            rooms[str(ev.get("room-id"))] = rooms.get(str(ev.get("room-id")), 0) + 1
    room, method = main_room_info(raw)
    if args.json:
        print(json.dumps({"census": census, "voice_by_room": rooms, "main_room_id": room,
                          "main_room_method": method}, indent=1))
    else:
        _print_census(census, False)
        print(f"voice events by room: {rooms} (main room: {room}, via {method})")
    return 0


def cmd_caps(args, client: ForumClient) -> int:
    caps = client.probe_capabilities(args.class_id, args.section_id)
    print(f"user {caps['user_id']} role={caps['role']}")
    for name, status in caps["endpoints"].items():
        print(f"  {name:<20} {'yes' if status == 200 else 'no'} ({status})")
    return 0


def cmd_probe(args, client: ForumClient) -> int:
    from scribe_core.audio import ffprobe_info, signed_url_expiry

    urls = client.recording_urls(args.class_id)
    url = urls.get(args.res)
    if not url:
        print(f"No {args.res} recording URL for class {args.class_id} (got: {sorted(urls)})",
              file=sys.stderr)
        return 1
    info = ffprobe_info(url)
    exp = signed_url_expiry(url)
    print(f"class {args.class_id} {args.res}: {info.get('width')}x{info.get('height')} "
          f"{info.get('video_codec')}/{info.get('audio_codec')} "
          f"{info.get('audio_sample_rate')} Hz x{info.get('audio_channels')}")
    dur = info.get("duration")
    print(f"duration: {dur:.1f}s ({dur / 60:.1f} min)" if dur else "duration: unknown")
    if info.get("size"):
        print(f"size: {info['size'] / 1e6:.1f} MB, bitrate {(info.get('bit_rate') or 0) / 1e3:.0f}"
              " kb/s")
    print(f"faststart (moov before mdat): {info.get('faststart')} "
          f"boxes={info.get('top_level_boxes')}")
    if info.get("faststart_error"):
        print(f"faststart check failed: {info['faststart_error']}")
    if exp:
        print(f"signed URL expires at {exp.isoformat()} (URL not shown)")
    class_json = client.class_json(args.class_id)
    started, ended, n = recording_bounds(class_json)
    print(f"Forum: {n} recording session(s), {started} -> {ended}")
    return 0


def cmd_transcribe(args, client: ForumClient | None, data_dir: Path = DEFAULT_DATA) -> int:
    ensure_private_dir(data_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    salt = load_or_create_salt(data_dir / ".id_salt") if args.privacy in ("ids", "both") else None
    opts = TranscribeOptions(
        provider=args.provider, model=args.model, privacy=args.privacy,
        formats=tuple(args.formats), res=args.res, language=args.language,
        keyterms=not args.no_keyterms, extra_keyterms=tuple(args.keyterm),
        url_mode=not args.no_url_mode, name_fixup=args.name_fixup,
        debug_html=not args.no_debug, salt=salt,
    )
    bundle = load_bundle_dir(args.bundle_dir) if args.bundle_dir else None
    provider = SavedASR(args.reuse_asr) if args.reuse_asr else None

    def progress(stage, message, fraction=None):
        pct = f" {fraction:.0%}" if isinstance(fraction, float) and stage == "downloading" else ""
        print(f"\r[{stage}] {message}{pct}    ", end="" if stage == "downloading" else "\n",
              flush=True)

    hand_log = None
    if args.hand_log:
        data = _read_json(args.hand_log, {}) or {}
        hand_log = data.get("entries") if isinstance(data, dict) else data
    result = transcribe_class(args.class_id, client, out_dir=out, options=opts, bundle=bundle,
                              audio_path=args.audio, provider=provider, progress=progress,
                              hand_log=hand_log)
    s = result["summary"]
    print(f"\n{s['title']} ({s['class_id']}): {s['n_utterances']} utterances, "
          f"{s['n_words']} words via {s['provider']} [{s['asr_mode']}]")
    print(f"{s['timing']} (status {s['calibration_status']}, offset {s['offset_s']:+.3f}s, "
          f"drift {s['drift']:+.6f})")
    print("attribution: " + ", ".join(f"{k} {v:.0%}" for k, v in s["how_mix"].items()))
    for w in s["warnings"]:
        print(f"warning: {w}")
    for f in result["files"]:
        print(f"  {f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    _load_env(args.env_file)
    try:
        offline = (args.command == "census" and args.dir is not None) or (
            args.command == "transcribe" and args.bundle_dir is not None
            and (args.audio is not None or args.reuse_asr is not None)
            and not os.environ.get(COOKIE_ENV))
        client = None if offline else ForumClient()
        handler = {"me": cmd_me, "sync": cmd_sync, "census": cmd_census, "caps": cmd_caps,
                   "probe": cmd_probe, "transcribe": cmd_transcribe}[args.command]
        return handler(args, client)
    except (ForumError, PipelineError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted (sync is resumable: run it again)", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
