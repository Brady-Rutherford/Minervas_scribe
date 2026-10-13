// ============================================================
// content.js — Runs on Forum class pages
// Match: https://forum.minerva.edu/app/courses/*/sections/*/classes/*
// ============================================================

const VERBOSE = true;
function log(...args) { if (VERBOSE) console.log("[CT:content]", ...args); }

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

const WIDGET_ID = "class-transcriber-widget";
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
  const panel = document.getElementById("ct-panel");
  if (!panel) return;
  const status = (state && state.status) || "idle";
  const message = (state && state.backendMessage) || (state && state.error) || status;
  const pct = PROGRESS_PCT[status] ?? 0;

  const textEl = panel.querySelector(".ct-panel-status");
  const barEl = panel.querySelector(".ct-panel-bar-inner");
  if (textEl) textEl.textContent = message;
  if (barEl) barEl.style.width = pct + "%";
}

function createProgressWidget() {
  if (document.getElementById(WIDGET_ID)) return;

  const root = document.createElement("div");
  root.id = WIDGET_ID;
  root.style.cssText = [
    "position:fixed",
    "bottom:20px",
    "right:20px",
    "z-index:2147483647",
    "fontFamily:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif",
    "display:flex",
    "flexDirection:column",
    "alignItems:flex-end",
    "gap:8px",
  ].join(";");

  // Panel (hidden by default)
  const panel = document.createElement("div");
  panel.id = "ct-panel";
  panel.className = "ct-panel";
  panel.style.cssText = [
    "display:none",
    "minWidth:280px",
    "maxWidth:340px",
    "padding:14px 16px",
    "background:#1a1a2e",
    "color:#fff",
    "fontSize:13px",
    "lineHeight:1.4",
    "borderRadius:10px",
    "boxShadow:0 4px 20px rgba(0,0,0,.3)",
  ].join(";");
  panel.innerHTML = [
    '<div style="fontWeight:700;marginBottom:8px;fontSize:12px;textTransform:uppercase;letterSpacing:.5px;color:#FDBA74">Minerva\'s Scribe</div>',
    '<div class="ct-panel-status" style="marginBottom:10px">Starting…</div>',
    '<div class="ct-panel-track" style="height:6px;background:rgba(255,255,255,.2);borderRadius:3px;overflow:hidden;marginBottom:12px">',
    '  <div class="ct-panel-bar-inner" style="height:100%;width:0;background:linear-gradient(90deg,#F97316,#EA580C);borderRadius:3px;transition:width .3s ease"></div>',
    "</div>",
    '<div style="display:flex;gap:8px;justifyContent:flex-end">',
    '  <button type="button" class="ct-btn ct-btn-dismiss" style="padding:6px 12px;background:rgba(255,255,255,.15);color:#fff;border:none;borderRadius:6px;fontSize:12px;fontWeight:600;cursor:pointer">Dismiss</button>',
    '  <button type="button" class="ct-btn ct-btn-close" style="padding:6px 12px;background:#F97316;color:#fff;border:none;borderRadius:6px;fontSize:12px;fontWeight:600;cursor:pointer">Close</button>',
    "</div>",
  ].join("");

  const dismissBtn = panel.querySelector(".ct-btn-dismiss");
  const closeBtn = panel.querySelector(".ct-btn-close");
  dismissBtn.addEventListener("click", () => {
    panel.style.display = "none";
    if (progressPollTimer) {
      clearInterval(progressPollTimer);
      progressPollTimer = null;
    }
  });
  closeBtn.addEventListener("click", () => {
    root.remove();
    if (progressPollTimer) {
      clearInterval(progressPollTimer);
      progressPollTimer = null;
    }
  });

  // Icon button
  const iconBtn = document.createElement("button");
  iconBtn.type = "button";
  iconBtn.className = "ct-icon-btn";
  iconBtn.setAttribute("aria-label", "Class Transcriber progress");
  iconBtn.style.cssText = [
    "width:48px",
    "height:48px",
    "borderRadius:50%",
    "background:#F97316",
    "color:#fff",
    "border:none",
    "cursor:pointer",
    "display:flex",
    "alignItems:center",
    "justifyContent:center",
    "boxShadow:0 2px 12px rgba(249,115,22,.4)",
  ].join(";");
  iconBtn.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>';

  iconBtn.addEventListener("click", () => {
    const isOpen = panel.style.display !== "none";
    if (isOpen) {
      panel.style.display = "none";
      if (progressPollTimer) {
        clearInterval(progressPollTimer);
        progressPollTimer = null;
      }
    } else {
      panel.style.display = "block";
      getStateFromBackground().then(renderPanel);
      if (!progressPollTimer) {
        progressPollTimer = setInterval(() => {
          getStateFromBackground().then((s) => {
            renderPanel(s);
            if (s && (s.status === "complete" || s.status === "error")) clearInterval(progressPollTimer);
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

  if (msg.type === "FETCH_FORUM_DATA") {
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
      log("Clicking download button:", btn.textContent.trim());
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
