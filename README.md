# Minerva's Scribe

Transcribes Minerva Forum class sessions to PDF/CSV with speaker labels and attendance.

## Getting started

**Clone the repo:**
```bash
git clone https://github.com/Brady-Rutherford/Minervas_scribe.git
cd Minervas_scribe
```

If you already have the repo, pull the latest: `git pull`.

If you want the Chrome extension + backend, use the `extension` branch:
```bash
git checkout extension
```

**One-time setup:** Python 3.9+ and ffmpeg must be installed. From the project root:
```bash
python3 -m venv .venv
source .venv/bin/activate   # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Then pick one of the two ways to run it below.

---

## Two ways to run it

**1. Streamlit (manual)**  
Paste a Forum cURL and the class video URL (or upload a file), then run the pipeline from the browser.

```bash
./setup.sh
```

Opens the app at http://localhost:8501. You need the cURL from DevTools and the video URL from the "Download Class Video" button.

**2. Chrome extension (automatic)**  
If you're on a Forum class page and logged in, the extension can pull class data and the video URL for you. No copy-paste.

- Load the unpacked extension from the `extension/` folder (Chrome → Extensions → Developer mode → Load unpacked).
- Start the backend: `source .venv/bin/activate && pip install -r requirements.txt && python server.py` (runs on port 5001).
- Open a class page on forum.minerva.edu, click the extension, then Start Transcription.

Details and permissions are in `extension/README.md`.

**Requirements**  
Python 3.9+, ffmpeg on your PATH, and (for the extension) the dependencies in `requirements.txt` (includes Whisper, Flask, etc.). The backend does the heavy work: download video, convert to audio, run Whisper, merge with Forum voice events, then build the PDF/CSV.
