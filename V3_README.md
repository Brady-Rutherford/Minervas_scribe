# Minerva's Scribe v3

v3 is a rewrite of the transcription engine as one Python package (`scribe_core`) with three
thin hosts: a local CLI (`scribe`), a localhost Flask backend (`scribe-server`), and a new
Chrome extension (`extension_v3/`). Everything is **added beside** v2: `logic.py`,
`server.py`, `server_modal.py`, `app.py`, `extension/` and the setup scripts are unchanged and
still work.

What changes for you:

- **Class data and the video URL both come from Forum's API.** No DOM scraping, no clicking
  "Download Class Video", no tab capture (those are kept only as an extension fallback).
- **Every word gets a speaker, and the transcript records how each speaker was decided.**
  Deepgram diarization and Forum voice events are fused per word after the two clocks are
  calibrated against each other. Unmatched speech is "Unattributed" and never "Professor".
- **Bulk harvest.** `scribe sync` pulls metadata for every class you've taken into a local
  folder plus a `manifest.csv`.
- **New outputs.** An interactive HTML transcript, Markdown, CSV, JSON, a JSONL dataset for
  CS156, optional PDF, and a four-lane debug timeline.

```
Forum bundle ──► forum_events: all recording sessions, ms timing, main room only ─┐
signed URL / audio ──► ASR: Deepgram Nova-3 (default) | Groq | local faster-whisper ┤
                                                                                     ▼
            calibrate (offset + drift, 10 ms) ─► fuse (who said each word) ─► turns
                                                                                     ▼
                         scribe.transcript/v2 ─► json csv md html jsonl pdf + debug view
```

## Layout (all new)

```
pyproject.toml            package + console scripts (scribe, scribe-server)
.env.example              template for credentials (copy to .env; never commit .env)
scribe_core/
  forum.py                ForumClient (cookie from env, 3 req/s, retries) + event census
  forum_events.py         the ONE parser: key casing, recording timeline, voice, chapters
  audio.py                per-job workdirs, streaming download, ffmpeg -> Opus, ffprobe
  asr_base.py / asr.py    provider interface + registry (deepgram, groq, local)
  asr_deepgram.py         Nova-3: EU endpoint, mip_opt_out, diarize, keyterms, URL mode
  calibrate.py            clock calibration (offset, drift, piecewise jumps)
  fuse.py                 cluster->user Hungarian mapping, per-word fusion, smoothing
  turns.py                words -> turns
  fixup.py                shaky-span detector + constrained roster-name fix-up (opt-in)
  metrics.py              attribution accuracy, boundary error, WER, 3-config comparison
  schema.py               scribe.transcript/v2
  pipeline.py             orchestration (transcribe_bundle / transcribe_class)
  privacy.py              salted pseudonyms for ids mode; self-ignoring data dirs
  export/                 to_json to_csv to_md to_html to_jsonl to_pdf debug_html
hosts/cli.py              `scribe` CLI
hosts/local_server.py     `scribe-server` (127.0.0.1 only)
extension_v3/             Chrome extension v3.0.0 (based on the April 2026 v2.0.0 build)
tests/                    pytest suite (offline, synthetic data only)
```

## Install

You need Python 3.11+ and `ffmpeg`/`ffprobe` on your PATH (`brew install ffmpeg`).

```bash
cd Minervas_scribe
git checkout v3
uv venv && uv pip install -e ".[dev]"          # or: python -m venv .venv && pip install -e ".[dev]"
uv pip install -e ".[server,pdf]"              # local backend for the extension + PDF export
uv pip install -e ".[groq]"                    # optional fallback provider
uv pip install -e ".[local]"                   # optional offline faster-whisper (large download)
```

Deepgram, the default provider, needs nothing beyond `requests`. Heavy dependencies are
imported only when used.

## Credentials (read this before creating `.env`)

The root `.gitignore` lists `.env`, `.env.*` (except `.env.example`), `scribe_data/` and
media files (`*.mp4`, `*.ogg`, `*.wav`). Every new v3 folder also has its own `.gitignore`, and
data directories that `scribe` creates contain a `.gitignore` with `*`. `scribe` prints a warning
if your `.env` is not git-ignored.

Then run `cp .env.example .env` and fill it in:

- **`FORUM_COOKIE`**: open forum.minerva.edu while logged in, then go to DevTools →
  Network, click any `forum.minerva.edu/api/...` request, open Request Headers, and copy the
  whole `cookie` value. **This is your login, so treat it like a password.** It stays on your
  machine, the CLI sends it only to `https://forum.minerva.edu` (redirects are not followed,
  and `next` links to other hosts are refused), and it expires. When `scribe me` says
  401/403, copy a fresh one.
- **`DEEPGRAM_API_KEY`**: makes Deepgram the default provider.
- **`GROQ_API_KEY`**: Groq fallback. With no keys at all, `scribe` uses local faster-whisper.

The extension never needs `FORUM_COOKIE`. It calls Forum same-origin from the page, so your
cookies never leave the browser.

## CLI

```bash
scribe me                                   # prints your name / id / role: the cookie works
scribe caps --class 81321 --section 11427   # which optional endpoints your role can read
scribe sync                                 # every past + current class -> scribe_data/
scribe sync --sections past --limit 50      # resumable; re-run to continue
scribe census --dir scribe_data             # event-type census over everything synced
scribe census --class 81321 [--json] [--sample]
scribe probe --class 81321 [--res highres]  # resolution, duration, codecs, faststart (URL never printed)
scribe transcribe --class 81321             # Deepgram if DEEPGRAM_API_KEY, else Groq, else local
scribe transcribe --class 81321 --provider groq --privacy ids --formats json,csv,html,jsonl
```

`sync` writes `scribe_data/raw/<class_id>/{class,events,polls,poll_responses,
outcome_assessments,focused_outcomes,breakouts,analytics}.json` (whichever your role can read)
plus `listing.json` and a `_complete.json` marker. Completed classes are skipped on the next
run. It stops at once on a 401 or a redirect to login, and after three 403s in a row, so a dead
cookie can't hammer Forum. It
also writes `scribe_data/manifest.csv` with the columns `class_id, section_id, course_code,
term, title, date, recording_started, recording_ended, n_voice, n_my_voice,
n_events_by_type`. `sync` downloads no video.

Useful `transcribe` options:

| flag | what it does |
|---|---|
| `--res lowres\|highres` | which signed recording to use (default lowres, the 600k rendition) |
| `--language en` | Deepgram defaults to `multi`; measure whether `en` is better for a course |
| `--keyterm "Bayes"` (repeatable) / `--no-keyterms` | extra prompt terms / don't send roster names |
| `--no-url-mode` | Deepgram: download + clean + upload instead of sending the signed URL |
| `--bundle-dir scribe_data/raw/81321` | use synced class data instead of refetching |
| `--audio file.mp4` | transcribe a local file instead of the recording |
| `--reuse-asr out/..._asr.json` | skip ASR and re-run calibration and fusion only (free; works offline with `--bundle-dir`) |
| `--hand-log hands.json` | add your exported hand-raise log to the timeline |
| `--name-fixup` | constrained roster-name fix-up of low-confidence words |
| `--privacy names\|ids\|both` | see Privacy |

Outputs land in `scribe_data/transcripts/` as
`<COURSE>_<date>_class<id>_<mode>.{json,csv,md,html,jsonl[,pdf]}`, together with
`..._debug.html` and `..._asr.json`.

## Extension v3 + local backend

1. Run `scribe-server`. It listens on `http://127.0.0.1:5001` and binds to localhost only.
2. In Chrome, open `chrome://extensions`, turn on Developer mode, click Load unpacked, and
   pick `extension_v3/`. It installs as "Minerva's Scribe v3" next to v2 without conflict:
   separate storage, separate widget ids.
3. Open a Forum class page, click the extension, then Start Transcription.

What the extension does:

- In the content script, `forum-client.js` fetches the class bundle (class, class-events,
  polls, poll-responses, outcome-assessments, focused-outcomes, breakouts, analytics if your
  role allows) and the signed recording URL, all same-origin.
- The background worker posts `{forum_class_json, forum_events_json, bundle,
  signed_video_url, privacy_mode, provider, formats}` to the backend. The first two fields keep
  the v2 Modal backend working too.
- If the API path fails, it falls back to the v2 DOM scan and download-button capture.
- The Settings tab covers backend (Local default / Modal (v2 backend) / custom), names vs
  ids, low/high-res, provider, PDF, and the optional hand-raise log.
- No CloudFront host permission is needed: the backend (or Deepgram) fetches the video, not
  the browser. CloudFront would block a browser fetch from the Forum origin anyway (CORS).
- Optional and **off by default**: "Log my own hand raises". On the live classroom page it
  records only *your* `H` key presses (ignored while typing) and a best-effort read of your
  raise/lower button. Nothing is recorded about classmates. Entries stay in
  `chrome.storage.local`; export them as JSON from Settings and pass the file to
  `scribe transcribe --hand-log`. The live-classroom URL and button labels are **unverified**,
  so detection is heuristic: a path containing `/live` or `/classroom`, or at least 2 `<video>`
  tiles.

Hardening compared with v2's `server.py`:

- jobs are named with `secrets.token_urlsafe(16)` ids
- CORS is allowed only for `chrome-extension://` origins (pin yours with
  `SCRIBE_ALLOWED_ORIGINS`)
- the `Host` header must be localhost, which blocks DNS rebinding
- `signed_video_url` must be an https `*.cloudfront.net` URL
- downloads are limited to the job's own files
- jobs persist in `scribe_data/server/jobs.json`, which holds no URLs or class data, and
  jobs that were running during a restart are marked interrupted
- each job gets its own temp workdir
- only local-Whisper jobs are serialized, because remote ASR can run in parallel and signed
  URLs expire

## How speaker attribution works (phase 1)

1. **Forum voice events across every recording session.** Wall-clock time maps onto the video
   as the sessions concatenated in order. Each interval is `end_ms = timestamp`,
   `start_ms = timestamp − event_data.duration`. There is no 1 s minimum any more. Only the
   main room is kept: its `room_signature` is "Main Classroom", and if that's missing, the most
   common voice `room-id` is used instead.
2. **ASR.** Deepgram Nova-3 with `diarize`, `utterances`, `utt_split=0.8`, `smart_format`,
   `punctuate`, `paragraphs`, `filler_words`, `mip_opt_out=true`, the EU endpoint, and
   keyterms: roster names first, then course and session title, capped at about 90 words.
   - **URL mode** sends the signed CloudFront URL to Deepgram. If Deepgram can't fetch it,
     the job downloads, extracts audio, applies `highpass/lowpass/loudnorm`, and uploads
     24 kbps Opus.
   - **Groq and local** get 16 kHz mono 16 kbps Opus. Groq gets back-to-back chunks with no
     overlap, so no words are duplicated at the seams.
3. **Calibration.** Both sources become 10 ms activity signals. The FFT cross-correlation
   finds the best lag in 5-minute windows (step 150 s, ±30 s search). A weighted line
   `lag(t) = offset + drift·t` is fitted through the windows with one round of outlier
   rejection.
   - |drift| > 0.005 is rejected as implausible.
   - A residual above 1.5 s fits two pieces either side of the jump (status `piecewise`).
   - A score below 0.3, or a shift that makes the agreement worse, means **no shift** and the
     transcript says **"timing unverified"**.
   - Otherwise it prints "timing check: 0.82".
4. **Fusion.** Each Deepgram cluster is mapped to a Forum user with a seconds-overlap matrix
   and the Hungarian algorithm (scipy). A leftover cluster still ≥60% one person is treated
   as a split of that person, and merged clusters stay anonymous. Every word gets a `how`:

   | `how` | meaning |
   |---|---|
   | `forum+cluster` | Forum and the mapped cluster agree (strongest) |
   | `forum` | one Forum speaker and no cluster opinion |
   | `cluster` | no Forum coverage, or crosstalk resolved by voice |
   | `conflict` | one Forum speaker but the cluster says otherwise; `cluster_conf > 0.8` wins (mic bleed) |
   | `forum-overlap` | crosstalk the cluster can't resolve; the Forum speaker with most overlap wins |
   | `none` | **Unattributed** |

   Forum intervals get 0.25 s of slack on each edge. A smoothing pass then relabels runs
   shorter than 0.6 s that sit between identical, stronger neighbours.
5. **Turns.** A turn breaks on a speaker change or a pause over 1.2 s. Each turn stores mean
   word confidence, the share of `how` values, and character spans of words under 0.6
   confidence: the HTML underlines them and the PDF greys out unattributed turns.

**With `groq` or `local` there is no diarization, so fusion uses Forum signals only.**
Attribution is weaker in crosstalk, and the transcript carries a warning saying so.

## Outputs

- **`.json`**: the full `scribe.transcript/v2` object. It contains `class`, `asr`,
  `alignment` (timing label, calibration with every window, main-room method, cluster map,
  `how` mix), `speakers` (role, `is_me`, Forum talk time in the main room and in breakouts,
  turns, words), `timeline` (chapters), `utterances`, `events` (chat, bookmark notes, your
  hand-log entries), `attendance`, `participation` (from staff-only analytics when readable),
  `forum_extras`, `quality` (shaky spans, name fix-ups) and `warnings`.
- **`.csv`**: one row per utterance with the columns `class_id, speaker_id, speaker, start,
  end, text, confidence`. `class_id` comes from the class object; v2's bug B11 left it blank.
- **`.md`**: a header, the chapter list, and the transcript under chapter headings.
- **`.html`**: a single self-contained file (no external assets). It has chapters, a speaker
  filter, search, "only my turns", optional inline chat, anchor timestamps, low-confidence
  underlines, and dark mode.
- **`.jsonl`**: one row per utterance (`class_id, start, end, speaker_id, speaker_role,
  is_me, text, text_clean, timeline_segment, confidence`). `text` keeps filler words, because
  an "um" before a contribution is signal for CS156.
- **`.pdf`** (needs `.[pdf]`): the v2 layout, rebuilt from the v2 transcript object. If
  reportlab is missing the job doesn't fail; the summary warns that PDF was skipped.
- **`_debug.html`**: four lanes: Forum voice (raw), Forum voice (calibrated), Deepgram
  clusters, and final turns plus words coloured by `how`. It also has a zoom control and a
  table of calibration windows.
- **`_asr.json`**: the raw ASR words, kept so you can re-run alignment with `--reuse-asr`.

## Measuring it

`scribe_core.metrics` has `attribution_accuracy`, `boundary_error`, `wer`, `name_accuracy`
and `compare_configs(words, voice_raw, gold_turns)`. The last one runs the three plan configs
(**v2 baseline** emulation, **deepgram-only**, **full calibrate+fuse**) on one class, and
`format_table` renders the README table. This table has not been filled in yet: it needs
2 hand-labelled classes plus the saved `_asr.json` and synced bundle.

| config | attribution accuracy | boundary error | timing |
|---|---|---|---|
| v2 baseline | TBD | TBD | n/a |
| deepgram-only | TBD | TBD | n/a |
| full (calibrate+fuse) | TBD | TBD | TBD |

## Privacy rules

- Forum cookies never leave the browser (extension). In the CLI the cookie comes from your
  local `.env` and goes only to forum.minerva.edu.
- Signed video URLs are never logged, printed or written to disk. Query strings are stripped
  or redacted in every log line and error message (ffmpeg stderr included). Jobs files don't
  store them.
- In URL mode the signed URL goes to Deepgram, which is how Deepgram fetches the video.
- The raw video lives only in a per-job temp dir and is deleted right after audio extraction.
- `--privacy ids` keys speakers by a **salted** HMAC pseudonym (`u_xxxxxxxxxx`). A plain hash
  of a small integer id would be reversible. It strips names from speakers, attendance,
  events and the debug view. Names spoken aloud inside the text can't be removed.
- Keyterms send roster names to Deepgram (it already hears them in the audio).
  `--no-keyterms` turns this off.
- The client refuses `/api/v1/class-users/*` outright. It is PATCH-only and is where the live
  classroom saves hand-raise state.

## Verified endpoints (student account, Oct 2026)

| endpoint | result |
|---|---|
| `GET /api/v1/users/self?show_inactive=false` | user object (`id`, `first-name`, `last-name`, `role`, ...) |
| `GET /api/v1/sections?hide-defaults=true` / `&state=past` | current / past sections (`id`, `course{id, course-code, title, term}`, ...) |
| `GET /api/v1/paginated-classes?category=past&page_size=50&filter_section_id={sid}` | `{count, next, previous, results}`. **Only `filter_section_id` filters.** |
| `GET /api/v1/class_grader/classes/{cid}` | `id, title, assessment, section, class-users, recording-sessions, type` |
| `GET .../classes/{cid}/class-events` | voice, message, timeline-segment (+ bookmark-note in the frontend) |
| `GET .../classes/{cid}/polls`, `/poll-responses`, `/outcome-assessments`, `/focused-outcomes` | 200 for students; shapes not inspected; stored raw |
| `GET .../classes/{cid}/breakouts` | breakout groups; stored raw |
| `GET /api/v1/recording-sessions/{CLASS_ID}/generate_recording_urls` | `{highres, lowres}` signed CloudFront URLs (expire) |
| `GET /api/v1/classes/{cid}` | live-classroom model (readable; not used yet) |
| `GET /api/v1/analytics/class/{cid}`, `/analytics/section/{sid}` | 403 for students; staff get `user_engagement` (`hand_raises`, `reactions`, `chat_messages`) |
| `GET /api/v1/sections/{sid}/class-meetings` | 403 |
| `/api/v1/class-users/{id}` | PATCH-only (405 on GET). **Never called.** |

## Plan vs reality (where live checks corrected the plan)

- **The video URL is in the API.** The plan said it wasn't, but
  `recording-sessions/{CLASS_ID}/generate_recording_urls` returns both renditions. That
  replaces DOM scraping, the download click and tab capture, and makes `scribe probe` and
  batch work possible.
- **Hand raises are not visible to students.** They're absent from class-events, and the
  analytics that count them return 403. That's why there's the opt-in, own-data-only
  extension log.
- **Analytics path.** It is `/analytics/class/{id}`, not the Notion doc's
  `/analytics/{session-id}` (which 404s). Students get 403.
- **`timestamp` semantics depend on type.** For `voice` it is the END of the utterance in
  epoch ms (2023 classes have whole-second values, which are flagged as such). For `message`
  and `timeline-segment` it is the START. The `start-time`/`end-time` ISO strings are whole
  seconds, so v3 doesn't use them for voice.
- **`recording-state` is "NOREC" on all 694 sessions checked**, including ones with video, so
  it is never used as a filter.
- **`paginated-classes` ignores `section=`, `section-id=` and `section_id=`.** Without
  `filter_section_id` it returns about 12k org-wide classes going back to 2014. The client
  requires that filter, re-adds it to `next` links, and aborts if a section claims more than
  2000 classes.
- **Key casing is header-driven.** The web app gets snake_case and a plain fetch gets
  kebab-case, so everything is normalised.
- **The main room is identified by `room_signature == "Main Classroom"`.** The count
  heuristic is only a fallback.
- **Breakout voice is not in the recording,** so it is excluded before calibration and
  fusion.

## Open questions (need a real class to answer)

- **Is the downloadable video the recording sessions concatenated in order?** Calibration
  flags a jump (`piecewise`) if not. Check one class that had a restart.
- **Is the 600k (`lowres`) rendition good enough audio for Deepgram, and how long does
  Deepgram take on 90 minutes?** For slow runs, switch to Deepgram's `callback` parameter.
  Also: can Deepgram reach the signed CloudFront URL? The upload fallback covers the case
  where it can't.
- **`language=multi` or `en`?** And does keyterm prompting work with `multi`? If Deepgram
  rejects it, run with `--language en`.
- **Is using AIC Deepgram credits for a personal/student tool appropriate?** That's your
  call, from the plan's §7. Groq or local work without it.
- **What shape does `class-users` have on class_grader?** The parser accepts both a nested
  `user` object and flat fields.

## Not done yet / next

- Fill the 3-config accuracy table above. This needs 2 hand-labelled classes; the metrics
  code is ready.
- Phase 1.6 second opinion: re-transcribe shaky spans through Groq with keyterms as the
  prompt. `find_shaky_spans` already reports the spans and the hook point is ready; Groq
  re-transcription isn't implemented. Constrained name fix-up is done (opt-in).
- HC/LO tagging, class summaries, "my contributions", the side-panel library and full-text
  search, Notion export, and the professor participation dashboard.
- Batch transcription across synced classes. `sync` and per-class `transcribe` exist; a
  queued, throttled `scribe transcribe --all` doesn't.
- Phase 5 cloud hardening: Modal auth tokens, expiring HMAC download links, TTL deletes,
  `@modal.concurrent`. `server_modal.py` (v2) is untouched and still has CORS `*` and no
  auth.
- A Deepgram `callback` mode for long classes, and a GitHub Actions CI (pytest + ruff).
- Not merged with, copied from or ported from any other repo (class-transcriber): no
  scheduler or naming templates. Chrome Web Store packaging is not done.
- The hand-raise logger's live-classroom detection and button-label heuristics need checking
  in a real live class.

## Tests

```bash
.venv/bin/pytest          # offline, synthetic fixtures only (no real class data or names)
.venv/bin/ruff check .    # lints v3 code only (v2 files are excluded in pyproject)
```

Live Forum, Deepgram, Groq and faster-whisper calls are **not** covered by tests. They need
real credentials and were not run.
