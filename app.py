import re
from pathlib import Path
import streamlit as st
from logic import run_pipeline, parse_class_id_from_curl

# -------------------- Page setup & minimal styling --------------------
st.set_page_config(page_title="Class Transcriber", page_icon="📝", layout="centered")
st.markdown(
    """
    <style>
      .block-container { max-width: 820px; padding-top: 2rem; padding-bottom: 5rem; }
      .stTextInput, .stTextArea, .stSelectbox, .stRadio, .stFileUploader { margin-bottom: 0.75rem; }
      .stDownloadButton { margin-right: 0.75rem; }
      .divider { border-bottom: 1px solid #e6e6e6; margin: 1.25rem 0 1.0rem; }
      .subtle { color: #666; font-size: 0.9rem; }
    </style>
    """,
    unsafe_allow_html=True
)

st.title("Class Transcriber")

# 1) Forum cURL 
st.header("1) Forum cURL")
st.markdown(
    """
    **📌 How to get your Forum cURL:**
    1. In your browser, open the exact class session page in Forum  
       e.g. `https://forum.minerva.edu/app/courses/3016/sections/11427/classes/81321`
    2. Press **F12** (or right-click → **Inspect**) to open DevTools.
    3. Go to the **Network** tab and type **api** in the filter bar.
    4. Refresh the page (**⌘R / CTRL+R**).
    5. In the request list, right-click **self** → **Copy** → **Copy as cURL**.
    6. Paste that cURL into the box below.
    """
)
curl = st.text_area("Paste your Forum cURL (DevTools → Network → request → Copy as cURL)", height=160)

class_id_hint, class_url_hint = "", ""
if curl:
    try:
        class_id_hint = parse_class_id_from_curl(curl) or ""
        ref_match = re.search(r"-H\s+['\"](?:referer|Referer):\s*([^'\"\\r\\n]+)", curl)
        if ref_match:
            class_url_hint = ref_match.group(1).strip()
    except Exception:
        class_id_hint, class_url_hint = "", ""

if class_id_hint:
    st.caption(f"Detected Class ID: **{class_id_hint}**")
if class_url_hint:
    st.caption(f"Detected Class URL: {class_url_hint}")

st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

# 2) Audio / Video Source
st.header("2) Audio / Video Source")
st.markdown(
    "📌 **How to use Direct URL:**\n"
    "- Go to your class page in Forum.\n"
    "- Click the **Download Class Video** button.\n"
    "- Copy the link (it should end in `.mp4` with a long token).\n"
    "- Paste that link here. The app will automatically download the file for you."
)

src = st.radio("Choose source", ["Direct URL", "Upload a file"], horizontal=True)

direct_url = None
uploaded = None
tmp_file_path = None

if src == "Direct URL":
    direct_url = st.text_input("Signed/Direct media URL", placeholder="https://.../lecture.mp4?token=...")
else:
    uploaded = st.file_uploader(
        "Upload audio/video file (MP3/MP4/WAV/M4A/AAC/OGG)",
        type=["mp3", "mp4", "wav", "m4a", "aac", "ogg"]
    )

st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

# 3) Output options
st.header("3) Output Options")

privacy = st.selectbox("Privacy mode", ["names", "ids", "both"], index=0)
st.caption("“both” will generate two files: one with names and one with IDs.")

model_choice = st.selectbox(
    "Whisper model",
    [
        "small (fast, moderate accuracy)",
        "medium (balanced speed/accuracy)",
        "large (slowest, overnight but most accurate)"
    ],
    index=1  # default = medium
)
model_name = model_choice.split()[0]

st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

# 4) Run the pipeline
if st.button("Generate Transcript"):
    if not curl:
        st.error("Please paste your Forum cURL.")
        st.stop()

    if uploaded is not None:
        tmp_dir = Path("tmp_inputs"); tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_file_path = str(tmp_dir / uploaded.name)
        with open(tmp_file_path, "wb") as f:
            f.write(uploaded.read())

    try:
        # 👇 NEW: Progress bar and status text
        progress_bar = st.progress(0)        # progress visual
        status_text = st.empty()             # text updates

        def progress_cb(message: str, fraction: float):
            status_text.text(message)
            progress_bar.progress(min(max(fraction, 0.0), 1.0))

        result = run_pipeline(
            class_url=class_url_hint,
            curl=curl.strip(),
            audio_url=(direct_url.strip() if direct_url else ""),
            audio_local_path=tmp_file_path,
            privacy_mode=privacy,
            output_root="outputs",
            model_name=model_name,
            progress_cb=progress_cb   # 👈 pass progress callback
        )

        progress_bar.progress(1.0)
        status_text.text("✅ Done!")

        pdf_paths = result.get("pdf_paths", [])
        csv_paths = result.get("csv_paths", [])
        meta = result.get("meta", {})

        st.markdown("### Summary")
        if meta.get("class_id"):
            st.write({"class_id": meta["class_id"]})
        if meta.get("session_title"):
            st.write({"session_title": meta["session_title"]})
        if meta.get("class_datetime"):
            st.write({"class_datetime": meta["class_datetime"]})
        if meta.get("class_link"):
            st.write({"class_link": meta["class_link"]})

        for p in pdf_paths:
            with open(p, "rb") as fh:
                st.download_button(
                    label=f"Download PDF ({Path(p).name})",
                    data=fh.read(),
                    file_name=Path(p).name,
                    mime="application/pdf"
                )
        for c in csv_paths:
            with open(c, "rb") as fh:
                st.download_button(
                    label=f"Download CSV ({Path(c).name})",
                    data=fh.read(),
                    file_name=Path(c).name,
                    mime="text/csv"
                )

    except Exception as e:
        st.error(f"Error: {e}")
