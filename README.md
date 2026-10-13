# Minerva's Scribe

Transcribes Minerva Forum class sessions to PDF/CSV with speaker labels and attendance.

**Download:** Clone the repo (use the `extension` branch for the Chrome extension + backend):
```bash
git clone https://github.com/Brady-Rutherford/Minervas_scribe.git
cd Minervas_scribe
git checkout extension
```
One-time setup: `python3 -m venv .venv`, then `source .venv/bin/activate` and `pip install -r requirements.txt`. You need **Python 3.9+** and **ffmpeg** on your PATH.

---

**1. Streamlit (manual)**  
Paste a Forum cURL and the class video URL (or upload a file), then run the pipeline from the browser. Run `./setup.sh` — app opens at http://localhost:8501. Get the cURL from DevTools and the video URL from "Download Class Video" on the class page.

**2. Chrome extension (automatic)**  
On a Forum class page while logged in, the extension can pull class data and the video URL. No copy-paste. Load the unpacked extension from the `extension/` folder (Chrome → Extensions → Developer mode → Load unpacked). Start the backend: `source .venv/bin/activate && python server.py` (port 5001). Open a class page, click the extension, then Start Transcription. More detail in `extension/README.md`.
