// ============================================================
// content.js — Minerva's Scribe v3, runs on Forum class pages
// Match: https://forum.minerva.edu/app/courses/*/sections/*/classes/*
//
// v3: class data AND the signed recording URL come from the Forum API via
// forum-client.js (same-origin fetches, so cookies never leave the browser).
// The v2 DOM scan / "Download Class Video" click capture is kept only as a fallback.
// ============================================================

const VERBOSE = true;
function log(...args) { if (VERBOSE) console.log("[Scribe3:content]", ...args); }

// --------------- class-ID extraction ---------------

function extractClassInfo() {
  const m = location.pathname.match(
    /\/app\/courses\/(\d+)\/sections\/(\d+)\/classes\/(\d+)/
  );
  if (!m) return null;
  return { courseId: m[1], sectionId: m[2], classId: m[3], url: location.href };
}

// --------------- Forum API fetch (same-origin → cookies auto-included) ---------------

async function fetchForumData(classId) {
  const [classRes, eventsRes] = await Promise.all([
    fetch(`/api/v1/class_grader/classes/${classId}`, { credentials: "same-origin" }),
    fetch(`/api/v1/class_grader/classes/${classId}/class-events`, { credentials: "same-origin" }),
  ]);

  if (!classRes.ok) throw new Error(`Class API returned ${classRes.status}`);
  if (!eventsRes.ok) throw new Error(`Events API returned ${eventsRes.status}`);

  return {
    classJson: await classRes.json(),
    eventsJson: await eventsRes.json(),
  };
}

// --------------- video URL — Layer 1: DOM / embedded data ---------------

function searchDOMForVideoUrl() {
  // 1. <video> / <source> tags
  for (const el of document.querySelectorAll("video source[src], video[src]")) {
    const src = el.src || el.getAttribute("src");
    if (src && /\.mp4/i.test(src)) return src;
  }

  // 2. Links / buttons whose visible text mentions "download" + "video"
  for (const el of document.querySelectorAll("a[href], button, [role='button']")) {
    const text = (el.textContent || "").toLowerCase();
    if (text.includes("download") && text.includes("video")) {
      const href = el.href || el.getAttribute("href");
      if (href && /\.mp4/i.test(href)) return href;
      if (href && !href.includes("forum.minerva.edu/app/")) return href;

      for (const attr of el.attributes) {
        if (/\.mp4/i.test(attr.value)) return attr.value;
      }
    }
  }

  // 3. <a> tags with href containing ".mp4"
  for (const a of document.querySelectorAll("a[href]")) {
    if (/\.mp4/i.test(a.href)) return a.href;
  }

  // 4. Inline <script> blocks that embed a video URL
  for (const s of document.querySelectorAll("script:not([src])")) {
    const match = s.textContent.match(/https?:\/\/[^\s"']+\.mp4[^\s"']*/);
    if (match) return match[0];
  }

  return null;
}

async function findVideoUrl() {
  // Retry a few times — Forum is a SPA and may render lazily
  for (let i = 0; i < 3; i++) {
    const url = searchDOMForVideoUrl();
    if (url) return url;
    if (i < 2) await new Promise((r) => setTimeout(r, 1000));
  }
  return null;
}

// --------------- video URL — Layer 2 helper: find the download button ---------------

function findDownloadButton() {
  // Text-based search
  for (const el of document.querySelectorAll("a, button, [role='button']")) {
    const text = (el.textContent || "").toLowerCase().trim();
    if (text.includes("download") && (text.includes("video") || text.includes("class video"))) {
      return el;
    }
  }
  // Attribute-based fallback
  for (const el of document.querySelectorAll("[title], [aria-label]")) {
    const label = (el.title || el.getAttribute("aria-label") || "").toLowerCase();
    if (label.includes("download") && label.includes("video")) return el;
  }
  return null;
}

/** If the download button is an <a> with href, return absolute URL; else null. */
function getDownloadButtonHref() {
  const btn = findDownloadButton();
  if (!btn) return null;
  const href = btn.href || btn.getAttribute("href");
  if (!href || href === "#" || href.startsWith("javascript:")) return null;
  try {
    return new URL(href, location.href).href;
  } catch {
    return null;
  }
}

// --------------- progress widget: icon + expandable panel (bottom-right) ---------------

// Distinct ids so v3 can run next to an installed v2 without fighting over the same widget.
const WIDGET_ID = "scribe-v3-widget";
const WIDGET_STYLE_ID = "scribe-v3-widget-styles";
const PROGRESS_PCT = {
  idle: 0, detected: 0, fetching_data: 15, getting_video: 30, sending: 45,
  transcribing: 65, compiling: 85, complete: 100, error: 0,
};

let progressPollTimer = null;

function getStateFromBackground() {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "GET_STATE" }, (resp) => {
      resolve(resp && resp.state ? resp.state : null);
    });
  });
}

function renderPanel(state) {
  const panel = document.getElementById("s3-panel");
  if (!panel) return;
  const status = (state && state.status) || "idle";
  const message = (state && state.backendMessage) || (state && state.error) || status;
  const pct = PROGRESS_PCT[status] ?? 0;
  const isError = status === "error";
  const isDone = status === "complete";

  const textEl = panel.querySelector(".s3-panel-status");
  const barEl = panel.querySelector(".s3-panel-bar-inner");
  const iconEl = panel.querySelector(".s3-panel-icon");

  if (textEl) textEl.textContent = message;
  if (barEl) {
    barEl.style.width = pct + "%";
    if (isError) barEl.style.background = "#DC2626";
    else if (isDone) barEl.style.background = "#16A34A";
    else barEl.style.background = "linear-gradient(90deg, #F97316, #EA580C)";
  }
  if (iconEl) {
    if (isDone) iconEl.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#16A34A" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
    else if (isError) iconEl.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#DC2626" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>';
    else iconEl.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#F97316" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
  }
}

function injectWidgetStyles() {
  if (document.getElementById(WIDGET_STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = WIDGET_STYLE_ID;
  style.textContent = `
    #${WIDGET_ID} {
      position: fixed;
      bottom: 20px;
      right: 20px;
      z-index: 2147483647;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 10px;
    }
    #s3-panel {
      display: none;
      min-width: 300px;
      max-width: 360px;
      background: #fff;
      border: 1px solid #E5E7EB;
      border-radius: 14px;
      padding: 16px 18px;
      box-shadow: 0 8px 30px rgba(0,0,0,.12), 0 2px 8px rgba(0,0,0,.06);
      color: #111827;
      font-size: 13px;
      line-height: 1.45;
      animation: s3-slide-up .2s ease-out;
    }
    @keyframes s3-slide-up {
      from { opacity: 0; transform: translateY(8px); }
      to   { opacity: 1; transform: translateY(0); }
    }
    .s3-panel-header {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 12px;
    }
    .s3-panel-icon {
      width: 28px;
      height: 28px;
      border-radius: 8px;
      background: #FFF7ED;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }
    .s3-panel-title {
      font-size: 13px;
      font-weight: 700;
      color: #111827;
      letter-spacing: -0.2px;
    }
    .s3-panel-subtitle {
      font-size: 10px;
      font-weight: 500;
      color: #9CA3AF;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .s3-panel-status {
      font-size: 12px;
      color: #374151;
      margin-bottom: 10px;
    }
    .s3-panel-track {
      width: 100%;
      height: 5px;
      background: #E5E7EB;
      border-radius: 3px;
      overflow: hidden;
      margin-bottom: 14px;
    }
    .s3-panel-bar-inner {
      height: 100%;
      width: 0;
      background: linear-gradient(90deg, #F97316, #EA580C);
      border-radius: 3px;
      transition: width .4s ease;
    }
    .s3-panel-actions {
      display: flex;
      gap: 8px;
      justify-content: flex-end;
    }
    .s3-btn-dismiss {
      padding: 6px 14px;
      background: #F3F4F6;
      color: #6B7280;
      border: 1px solid #E5E7EB;
      border-radius: 8px;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      transition: background .15s, color .15s;
    }
    .s3-btn-dismiss:hover { background: #E5E7EB; color: #374151; }
    .s3-btn-close {
      padding: 6px 14px;
      background: #F97316;
      color: #fff;
      border: none;
      border-radius: 8px;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      box-shadow: 0 1px 3px rgba(249,115,22,.3);
      transition: background .15s;
    }
    .s3-btn-close:hover { background: #EA580C; }
    .s3-icon-btn {
      width: 46px;
      height: 46px;
      border-radius: 50%;
      background: #F97316;
      color: #fff;
      border: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 14px rgba(249,115,22,.35);
      transition: transform .15s, box-shadow .15s;
    }
    .s3-icon-btn:hover {
      transform: scale(1.06);
      box-shadow: 0 6px 20px rgba(249,115,22,.45);
    }
    .s3-icon-btn:active { transform: scale(0.97); }
  `;
  document.head.appendChild(style);
}

function createProgressWidget() {
  if (document.getElementById(WIDGET_ID)) return;
  injectWidgetStyles();

  const root = document.createElement("div");
  root.id = WIDGET_ID;

  const panel = document.createElement("div");
  panel.id = "s3-panel";
  panel.innerHTML = `
    <div class="s3-panel-header">
      <div class="s3-panel-icon">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#F97316" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
      </div>
      <div>
        <div class="s3-panel-title">Minerva's Scribe v3</div>
        <div class="s3-panel-subtitle">Transcription in progress</div>
      </div>
    </div>
    <div class="s3-panel-status">Starting…</div>
    <div class="s3-panel-track">
      <div class="s3-panel-bar-inner"></div>
    </div>
    <div class="s3-panel-actions">
      <button type="button" class="s3-btn-dismiss">Dismiss</button>
      <button type="button" class="s3-btn-close">Close</button>
    </div>
  `;

  panel.querySelector(".s3-btn-dismiss").addEventListener("click", () => {
    panel.style.display = "none";
    if (progressPollTimer) { clearInterval(progressPollTimer); progressPollTimer = null; }
  });
  panel.querySelector(".s3-btn-close").addEventListener("click", () => {
    root.remove();
    if (progressPollTimer) { clearInterval(progressPollTimer); progressPollTimer = null; }
  });

  const iconBtn = document.createElement("button");
  iconBtn.type = "button";
  iconBtn.className = "s3-icon-btn";
  iconBtn.setAttribute("aria-label", "Minerva's Scribe v3 progress");
  iconBtn.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>';

  iconBtn.addEventListener("click", () => {
    const isOpen = panel.style.display !== "none";
    if (isOpen) {
      panel.style.display = "none";
      if (progressPollTimer) { clearInterval(progressPollTimer); progressPollTimer = null; }
    } else {
      panel.style.display = "block";
      getStateFromBackground().then(renderPanel);
      if (!progressPollTimer) {
        progressPollTimer = setInterval(() => {
          getStateFromBackground().then((s) => {
            renderPanel(s);
            if (s && (s.status === "complete" || s.status === "error")) { clearInterval(progressPollTimer); progressPollTimer = null; }
          });
        }, 2000);
      }
    }
  });

  root.appendChild(panel);
  root.appendChild(iconBtn);
  document.body.appendChild(root);
}

function showProgressWidget() {
  createProgressWidget();
}

// --------------- message handler ---------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  log("Message:", msg.type);

  if (msg.type === "SHOW_PROGRESS_UI") {
    showProgressWidget();
    sendResponse({ ok: true });
    return false;
  }

  if (msg.type === "FETCH_CLASS_BUNDLE") {
    globalThis.ScribeForumClient.classBundle(msg.classId)
      .then((bundle) => sendResponse({ bundle }))
      .catch((err) => sendResponse({ error: err.message, status: err.status || null }));
    return true; // async
  }

  if (msg.type === "FETCH_RECORDING_URLS") {
    // Signed CloudFront URLs: handed to the background worker, never logged.
    globalThis.ScribeForumClient.recordingUrls(msg.classId)
      .then((urls) => sendResponse({ urls }))
      .catch((err) => sendResponse({ error: err.message, status: err.status || null }));
    return true;
  }

  if (msg.type === "PROBE_CAPABILITIES") {
    globalThis.ScribeForumClient.probeCapabilities(msg.classId, msg.sectionId)
      .then((caps) => sendResponse({ caps }))
      .catch((err) => sendResponse({ error: err.message }));
    return true;
  }

  if (msg.type === "FETCH_FORUM_DATA") {  // v2 path, kept as a fallback
    fetchForumData(msg.classId)
      .then((data) => sendResponse(data))
      .catch((err) => sendResponse({ error: err.message }));
    return true; // async
  }

  if (msg.type === "GET_VIDEO_URL") {
    findVideoUrl()
      .then((url) => sendResponse({ url }))
      .catch(() => sendResponse({ url: null }));
    return true;
  }

  if (msg.type === "GET_DOWNLOAD_BUTTON_HREF") {
    const href = getDownloadButtonHref();
    sendResponse({ href: href || null });
    return false;
  }

  if (msg.type === "TRIGGER_DOWNLOAD_CLICK") {
    const btn = findDownloadButton();
    if (btn) {
      log("Fallback: clicking the download button");
      btn.click();
      sendResponse({ clicked: true });
    } else {
      log("Download button NOT found");
      sendResponse({ clicked: false, error: "Download button not found in DOM" });
    }
    return false;
  }

  if (msg.type === "PING") {
    sendResponse({ pong: true });
    return false;
  }

  if (msg.type === "GET_CURRENT_CLASS") {
    const info = extractClassInfo();
    sendResponse(info || null);
    return false;
  }

  return false;
});

// --------------- auto-detect on page load ---------------

(function init() {
  const info = extractClassInfo();
  if (info) {
    log("Class page detected:", info);
    chrome.runtime.sendMessage({ type: "PAGE_DETECTED", ...info });
  }
})();
