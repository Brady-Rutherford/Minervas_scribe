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

// --------------- message handler ---------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  log("Message:", msg.type);

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
