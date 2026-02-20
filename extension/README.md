# Class Transcriber — Chrome Extension

Zero-click Chrome extension that automates the Forum class transcription workflow.
The professor only needs to be logged into Forum and open the class page —
the extension handles everything else.

## Architecture

```
┌──────────────────────────────┐
│  Chrome Extension (MV3)      │
│  ┌──────────┐ ┌───────────┐ │       ┌──────────────────┐
│  │ content   │ │ background│ │ POST  │  Flask backend    │
│  │  .js      │→│  .js      │─────→  │  (server.py)      │
│  │ (Forum    │ │ (service  │ │       │  download → ffmpeg│
│  │  page)    │ │  worker)  │ │ poll  │  → Whisper → PDF  │
│  └──────────┘ └───────────┘ │←───── └──────────────────┘
│       ↑            ↑        │
│  ┌──────────┐      │        │
│  │ popup    │──────┘        │
│  │ .html/js │               │
│  └──────────┘               │
└──────────────────────────────┘
```

**Data flow:**

1. Content script extracts `class_id` from the Forum URL.
2. Content script fetches class metadata + voice events from the Forum API
   (same-origin, cookies auto-included — no cURL needed).
3. Content script + background acquire the signed video `.mp4` URL:
   - **Layer 1:** DOM analysis (links, `<video>` tags, inline scripts).
   - **Layer 2:** Programmatic click of the "Download Class Video" button;
     the background captures the new tab's URL and closes it.
4. Background POSTs `{ class_id, class_json, events_json, video_url }` to the
   local backend.
5. Backend downloads the video, runs Whisper, merges speakers, and compiles
   PDF/CSV outputs.
6. Popup shows progress and provides download links.

**Security:** No cookies or auth headers are ever sent to the backend.
All authenticated requests happen in-browser. Only the resulting JSON data
and the signed video URL are forwarded.

---

## Quick Start

### 1. Load the extension

1. Open Chrome → `chrome://extensions`
2. Enable **Developer mode** (top-right toggle).
3. Click **Load unpacked** → select the `extension/` folder.
4. Pin the "Class Transcriber" extension to your toolbar.

### 2. Run the backend

From the project root:

```bash
source .venv/bin/activate          # or create the venv first — see below
pip install -r requirements.txt    # includes flask + flask-cors
python server.py                   # starts on http://localhost:5000
```

If you don't have the venv yet:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python server.py
```

**Prerequisites:** `ffmpeg` must be on your PATH (needed by Whisper/pydub).

### 3. Transcribe a class

1. Make sure you're **logged into Forum** in Chrome.
2. Navigate to a class session page, e.g.
   `https://forum.minerva.edu/app/courses/3016/sections/11427/classes/81321`
3. Click the extension icon — it should show the detected class.
4. Adjust options if needed (privacy mode, Whisper model).
5. Click **Start Transcription**.
6. Wait for progress updates — Whisper transcription may take 30 min to several
   hours depending on the model and recording length.
7. Download the PDF/CSV files from the popup when done.

---

## Configuration

| Option        | Values                    | Default  | Notes                                       |
|---------------|---------------------------|----------|---------------------------------------------|
| Backend URL   | any URL                   | `http://localhost:5000` | Change if you host the backend elsewhere |
| Privacy mode  | `names` / `ids` / `both`  | `names`  | `both` generates two file sets              |
| Whisper model | `small` / `medium` / `large` | `medium` | Larger = more accurate but much slower   |

Options are saved across sessions via `chrome.storage.sync`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "No class page detected" | Make sure you're on a Forum class URL matching `.../courses/.../sections/.../classes/...` |
| Forum API returns 401/403 | Your Forum session may have expired — refresh the page or log in again |
| Video URL capture times out | The "Download Class Video" button may not be visible; scroll down or expand the section. As a fallback, you can manually copy the video URL and pass it to the backend |
| Backend connection refused | Ensure `python server.py` is running and the backend URL in the popup matches |
| Whisper out-of-memory | Use a smaller model, or ensure you have enough RAM / VRAM |

---

## Permissions Explained

| Permission | Why |
|------------|-----|
| `tabs` | Detect which Forum class page is open; capture video download tab URL |
| `storage` | Persist job state and user options |
| `activeTab` | Interact with the active Forum tab |
| `alarms` | Resume status polling if the service worker restarts |
| `host: forum.minerva.edu` | Content script injection and API access |
| `host: localhost` | Communicate with the local backend server |
