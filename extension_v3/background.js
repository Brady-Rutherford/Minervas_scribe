// ============================================================
// background.js — Minerva's Scribe v3 service worker (MV3)
//
// v3 flow: content script fetches the class bundle + a fresh signed recording URL from the
// Forum API (same-origin; cookies stay in the browser) -> POST to the backend -> poll.
// The v2 DOM scan / download-click / tab capture is only used if the API path fails.
// Signed URLs are never logged (query strings stripped).
// ============================================================

const VERBOSE = true;
function log(...args) { if (VERBOSE) console.log("[Scribe3:bg]", ...args); }

const LOCAL_BACKEND = "http://127.0.0.1:5001";
// v2 cloud backend (no auth, CORS *, produces PDF/CSV only). Opt-in from settings.
const MODAL_BACKEND = "https://mintranscriber--minervas-scribe-web.modal.run";
const BACKEND_PRESETS = { local: LOCAL_BACKEND, modal: MODAL_BACKEND };
const DEFAULT_FORMATS = ["html", "md", "csv", "json", "jsonl"];

// --------------- defaults ---------------

const DEFAULT_OPTIONS = {
  backendPreset: "local",
  backendUrl: LOCAL_BACKEND,
  privacyMode: "names",   // names | ids | both
  videoRes: "lowres",     // lowres | highres
  provider: "auto",       // auto | deepgram | groq | local (backend decides what's available)
  includePdf: false,
  logHandRaises: false,   // optional "log my own hand raises" (live classes only)
};

function redactUrl(u) {
  try { const x = new URL(u); return `${x.origin}${x.pathname}`; } catch { return "(url)"; }
}

const DEFAULT_STATE = {
  status: "idle",
  classId: null,
  courseId: null,
  sectionId: null,
  classUrl: null,
  classInfo: null,
  tabId: null,
  jobId: null,
  error: null,
  files: null,
  backendMessage: null,
};

// --------------- persisted state ---------------

async function getState() {
  const r = await chrome.storage.local.get("state");
  return { ...DEFAULT_STATE, ...(r.state || {}) };
}

async function setState(updates) {
  const current = await getState();
  const next = { ...current, ...updates };
  await chrome.storage.local.set({ state: next });
  try { chrome.runtime.sendMessage({ type: "STATE_CHANGED" }); } catch (_) { /* popup closed */ }
  return next;
}

async function getOptions() {
  const r = await chrome.storage.sync.get("options");
  return { ...DEFAULT_OPTIONS, ...(r.options || {}) };
}

async function setOptions(updates) {
  const current = await getOptions();
  const next = { ...current, ...updates };
  await chrome.storage.sync.set({ options: next });
  return next;
}

// --------------- content-script helpers ---------------

function messageContent(tabId, msg) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, msg, (resp) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      resolve(resp);
    });
  });
}

// --------------- forum data (fetched by content script, same-origin) ---------------

async function fetchForumData(tabId, classId) {
  log("Requesting Forum data via content script …");
  const res = await messageContent(tabId, { type: "FETCH_FORUM_DATA", classId });
  if (res && res.error) throw new Error(res.error);
  log("Forum data received — keys:", Object.keys(res || {}));
  return res;
}

// --------------- v3: class bundle + signed URL from the Forum API ---------------

async function getClassBundle(tabId, classId) {
  const res = await messageContent(tabId, { type: "FETCH_CLASS_BUNDLE", classId });
  if (res && res.bundle) {
    log("Bundle received — status:", res.bundle.status);
    return res.bundle;
  }
  if (res && res.status === 401) throw new Error(res.error);
  log("API bundle failed, falling back to the v2 fetch:", res && res.error);
  const legacy = await fetchForumData(tabId, classId);
  return {
    schema: "scribe.bundle/v1", class_id: Number(classId), status: { legacy: true },
    class: legacy.classJson, events: legacy.eventsJson,
  };
}

async function getRecordingUrl(tabId, classId, res) {
  try {
    const r = await messageContent(tabId, { type: "FETCH_RECORDING_URLS", classId });
    const urls = (r && r.urls) || {};
    const other = res === "highres" ? "lowres" : "highres";
    const url = urls[res] || urls[other];
    if (url) {
      log("Recording URL from the Forum API:", redactUrl(url));
      return url;
    }
    log("API gave no recording URL:", r && r.error);
  } catch (e) {
    log("API recording URL failed:", e.message);
  }
  log("Falling back to v2 DOM scan / download-button capture …");
  return getVideoUrl(tabId);
}

// --------------- video URL acquisition (v2 fallback) ---------------

async function getVideoUrl(tabId) {
  // Layer 1 — DOM / embedded data
  log("Layer 1: DOM analysis for video URL …");
  const domRes = await messageContent(tabId, { type: "GET_VIDEO_URL" });
  if (domRes && domRes.url) {
    log("Layer 1 found URL:", redactUrl(domRes.url));
    return domRes.url;
  }

  // Layer 2 — if "Download Class Video" is an <a href="...">, open that URL in a new tab; else simulate click
  const hrefRes = await messageContent(tabId, { type: "GET_DOWNLOAD_BUTTON_HREF" });
  if (hrefRes && hrefRes.href) {
    log("Layer 2: opening download link in new tab (no click)");
    return triggerDownloadAndCapture(tabId, hrefRes.href);
  }
  log("Layer 2: programmatic download-click + tab capture …");
  return triggerDownloadAndCapture(tabId);
}

function triggerDownloadAndCapture(classTabId, openUrl) {
  return new Promise((resolve, reject) => {
    let captured = false;
    let candidateTabId = null;

    const cleanup = () => {
      chrome.tabs.onCreated.removeListener(onCreated);
      chrome.tabs.onUpdated.removeListener(onUpdated);
    };

    const timer = setTimeout(() => {
      if (!captured) {
        cleanup();
        reject(new Error(
          "Timed out (45 s) waiting for the video download tab. " +
          "Make sure you can see a \"Download Class Video\" button on the page."
        ));
      }
    }, 45000);

    function onCreated(tab) {
      log("New tab created:", tab.id, redactUrl(tab.pendingUrl || tab.url || "about:blank"));
      candidateTabId = tab.id;
    }

    function onUpdated(tabId, info, tab) {
      if (tabId !== candidateTabId) return;
      const url = info.url || tab.url;
      if (!url || url === "about:blank" || url.startsWith("chrome://")) return;

      // Accept any URL that isn't the Forum app shell
      if (!url.includes("forum.minerva.edu/app/")) {
        captured = true;
        clearTimeout(timer);
        cleanup();
        log("Layer 2 captured URL:", redactUrl(url));
        chrome.tabs.remove(tabId).catch(() => {});
        resolve(url);
      }
    }

    chrome.tabs.onCreated.addListener(onCreated);
    chrome.tabs.onUpdated.addListener(onUpdated);

    if (openUrl) {
      chrome.tabs.create({ url: openUrl }, (tab) => {
        if (tab && tab.id) candidateTabId = tab.id;
      });
      return;
    }

    messageContent(classTabId, { type: "TRIGGER_DOWNLOAD_CLICK" })
      .then((r) => {
        if (!r || !r.clicked) {
          cleanup();
          clearTimeout(timer);
          reject(new Error("Could not find the \"Download Class Video\" button on the page."));
        }
      })
      .catch((err) => { cleanup(); clearTimeout(timer); reject(err); });
  });
}

// --------------- backend communication ---------------

async function sendToBackend(backendUrl, payload) {
  log("POSTing job to backend:", backendUrl, "(signed URL not logged)");
  const res = await fetch(`${backendUrl}/api/transcribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Backend returned HTTP ${res.status}`);
  }
  const data = await res.json();
  log("Job created:", data.job_id);
  return data.job_id;
}

// --------------- job polling ---------------

let lastPollTs = 0;
let jobStartedAt = 0;
const MIN_POLL_GAP_MS = 3000;
const COLD_START_THRESHOLD_MS = 20000;

async function checkJobStatus() {
  const now = Date.now();
  if (now - lastPollTs < MIN_POLL_GAP_MS) return;
  lastPollTs = now;

  const { activeJob } = await chrome.storage.local.get("activeJob");
  if (!activeJob) return;

  if (!jobStartedAt) jobStartedAt = now;

  try {
    const res = await fetch(`${activeJob.backendUrl}/api/status/${activeJob.jobId}`);
    if (!res.ok) return;
    const data = await res.json();

    log("Poll:", data.status, data.message);

    const earlyStages = ["queued", "downloading"];
    const elapsed = now - jobStartedAt;
    if (earlyStages.includes(data.status) && elapsed > COLD_START_THRESHOLD_MS) {
      await setState({ backendMessage: "Starting up server (first launch takes ~30s)…" });
    } else {
      await setState({ backendMessage: data.message });
    }

    const statusMap = {
      queued: "transcribing",
      downloading: "transcribing",
      preparing_audio: "transcribing",
      transcribing: "transcribing",
      processing: "compiling",
      compiling: "compiling",
    };
    if (statusMap[data.status]) {
      await setState({ status: statusMap[data.status] });
    }

    if (data.status === "complete") {
      chrome.alarms.clear("poll_job");
      await chrome.storage.local.remove("activeJob");
      jobStartedAt = 0;
      await setState({ status: "complete", files: data.files, backendMessage: "Transcription complete!" });
      const st = await getState();
      const opts = await getOptions();
      await saveToHistory({
        id: `job_${activeJob.jobId}`,
        classId: st.classId,
        sessionTitle: st.classInfo?.sessionTitle || `Session ${st.classId}`,
        completedAt: new Date().toISOString(),
        files: data.files,
        backendUrl: activeJob.backendUrl,
        jobId: activeJob.jobId,
      });
    } else if (data.status === "error") {
      chrome.alarms.clear("poll_job");
      await chrome.storage.local.remove("activeJob");
      await setState({ status: "error", error: data.message || "Backend processing failed" });
    }
  } catch (err) {
    log("Poll error (will retry):", err.message);
  }
}

// --------------- history persistence ---------------

async function saveToHistory(jobData) {
  const existing = await chrome.storage.local.get("transcription_history");
  const history = existing.transcription_history || [];
  history.unshift(jobData);
  if (history.length > 50) history.pop();
  await chrome.storage.local.set({ transcription_history: history });
  log("Saved to history:", jobData.id);
}

// --------------- main workflow ---------------

async function startTranscription() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) {
    try {
      const info = await messageContent(tab.id, { type: "GET_CURRENT_CLASS" });
      if (info && info.classId) {
        await setState({
          status: "detected",
          classId: info.classId,
          courseId: info.courseId ?? null,
          sectionId: info.sectionId ?? null,
          classUrl: info.url ?? null,
          tabId: tab.id,
          error: null,
          files: null,
          jobId: null,
          backendMessage: null,
          classInfo: null,
        });
      }
    } catch (_) { /* not a class page */ }
  }

  const st = await getState();
  const opts = await getOptions();

  if (!st.classId || !st.tabId) {
    await setState({ status: "error", error: "No class page detected. Navigate to a Forum class page first." });
    return;
  }

  try {
    // 1 — Forum data (API bundle; v2 fetch as fallback)
    await setState({ status: "fetching_data", error: null, files: null, jobId: null, backendMessage: null });
    // Show progress widget (small icon) on page so user sees feedback after popup closes
    try {
      chrome.tabs.sendMessage(st.tabId, { type: "SHOW_PROGRESS_UI" });
    } catch (_) { /* tab may have closed */ }
    const bundle = await getClassBundle(st.tabId, st.classId);
    const sessionTitle = bundle.class?.title || `Session ${st.classId}`;
    await setState({ classInfo: { sessionTitle } });

    // 2 — Signed recording URL, fetched right before use (it expires)
    await setState({ status: "getting_video" });
    const videoUrl = await getRecordingUrl(st.tabId, st.classId, opts.videoRes);

    // 3 — Send to backend. forum_class_json / forum_events_json keep v2 backends working;
    //     `bundle` carries the v3 extras (polls, breakouts, me, endpoint statuses...).
    await setState({ status: "sending" });
    const extras = { ...bundle };
    delete extras.class;
    delete extras.events;
    const formats = opts.includePdf ? [...DEFAULT_FORMATS, "pdf"] : DEFAULT_FORMATS;
    const jobId = await sendToBackend(opts.backendUrl, {
      class_id: st.classId,
      class_url: st.classUrl,
      forum_class_json: bundle.class,
      forum_events_json: bundle.events || [],
      bundle: extras,
      signed_video_url: videoUrl,
      privacy_mode: opts.privacyMode,
      provider: opts.provider,
      formats,
    });

    // 4 — Start polling
    await setState({ status: "transcribing", jobId });
    await chrome.storage.local.set({ activeJob: { jobId, backendUrl: opts.backendUrl } });
    chrome.alarms.create("poll_job", { periodInMinutes: 0.5 });
    await checkJobStatus();

  } catch (err) {
    log("Workflow error:", err);
    chrome.alarms.clear("poll_job");
    await chrome.storage.local.remove("activeJob");
    await setState({ status: "error", error: err.message });
  }
}

// --------------- alarm handler ---------------

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "poll_job") await checkJobStatus();
});

// --------------- message router ---------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "PAGE_DETECTED") {
    (async () => {
      await setState({
        status: "detected",
        classId: msg.classId,
        courseId: msg.courseId,
        sectionId: msg.sectionId,
        classUrl: msg.url,
        tabId: sender.tab?.id ?? null,
        error: null,
        files: null,
        jobId: null,
        backendMessage: null,
      });
      sendResponse({ ok: true });
    })();
    return true;
  }

  if (msg.type === "REFRESH_CLASS") {
    (async () => {
      const current = await getState();
      const isActive = !["idle", "detected", "complete", "error"].includes(current.status);
      if (!isActive && msg.classId) {
        await setState({
          status: "detected",
          classId: msg.classId,
          courseId: msg.courseId ?? current.courseId,
          sectionId: msg.sectionId ?? current.sectionId,
          classUrl: msg.url ?? current.classUrl,
          tabId: msg.tabId ?? current.tabId,
          error: null,
          files: null,
          jobId: null,
          backendMessage: null,
          classInfo: null,
        });
      }
      sendResponse({ ok: true });
    })();
    return true;
  }

  if (msg.type === "GET_STATE") {
    (async () => {
      checkJobStatus();
      const state = await getState();
      const options = await getOptions();
      sendResponse({ state, options });
    })();
    return true;
  }

  if (msg.type === "GET_OPTIONS") {
    getOptions().then(sendResponse);
    return true;
  }

  if (msg.type === "UPDATE_OPTIONS") {
    const next = { ...msg.options };
    if (next.backendPreset && BACKEND_PRESETS[next.backendPreset]) {
      next.backendUrl = BACKEND_PRESETS[next.backendPreset];
    }
    if (next.backendUrl) next.backendUrl = String(next.backendUrl).replace(/\/+$/, "");
    setOptions(next).then((o) => sendResponse({ ok: true, options: o }));
    return true;
  }

  if (msg.type === "CHECK_BACKEND") {
    (async () => {
      const opts = await getOptions();
      try {
        const r = await fetch(`${opts.backendUrl}/api/health`, { cache: "no-store" });
        const body = await r.json().catch(() => ({}));
        sendResponse({ ok: r.ok, version: body.version || null });
      } catch (_) {
        sendResponse({ ok: false });
      }
    })();
    return true;
  }

  if (msg.type === "START_TRANSCRIPTION") {
    startTranscription();
    sendResponse({ ok: true });
    return false;
  }

  if (msg.type === "RESET") {
    (async () => {
      chrome.alarms.clear("poll_job");
      await chrome.storage.local.remove("activeJob");
      const prev = await getState();
      await setState({
        ...DEFAULT_STATE,
        classId: prev.classId,
        courseId: prev.courseId,
        sectionId: prev.sectionId,
        classUrl: prev.classUrl,
        tabId: prev.tabId,
        status: prev.classId ? "detected" : "idle",
      });
      sendResponse({ ok: true });
    })();
    return true;
  }

  return false;
});

// --------------- tab lifecycle ---------------

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const st = await getState();
  if (st.tabId === tabId && !["transcribing", "compiling"].includes(st.status)) {
    await setState({ ...DEFAULT_STATE });
  }
});

// --------------- startup ---------------

log("Service worker started");
(async () => {
  const { activeJob } = await chrome.storage.local.get("activeJob");
  if (activeJob) {
    log("Active job found on startup — resuming poll:", activeJob.jobId);
    chrome.alarms.create("poll_job", { periodInMinutes: 0.5 });
  }
})();
