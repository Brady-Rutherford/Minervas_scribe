"""Minerva's Scribe v3 core engine.

One engine, three hosts (CLI, local Flask server, extension backend). The v2 files at the
repo root (logic.py, server.py, server_modal.py, app.py) are untouched and keep working.

Pipeline (see V3_README.md):

    Forum bundle ─► forum_events (all recording sessions, ms timing, main room only)
    audio/video  ─► ASR (Deepgram default; Groq / local faster-whisper fallbacks)
                 ─► calibrate (offset + drift)  ─► fuse (who said each word)
                 ─► turns ─► schema (scribe.transcript/v2) ─► exports
"""

__version__ = "3.0.0"
