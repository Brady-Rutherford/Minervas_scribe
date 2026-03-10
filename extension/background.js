// ============================================================
// background.js — Service worker for Class Transcriber (MV3)
// ============================================================

const VERBOSE = true;
function log(...args) { if (VERBOSE) console.log("[CT:bg]", ...args); }

const BACKEND_URL = "https://braydon--minervas-scribe-web.modal.run";

// --------------- defaults ---------------

const DEFAULT_OPTIONS = {
  backendUrl: BACKEND_URL,
  privacyMode: "names",
  whisperModel: "medium",
};

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

// --------------- video URL acquisition ---------------

async function getVideoUrl(tabId) {
  // Layer 1 — DOM / embedded data
  log("Layer 1: DOM analysis for video URL …");
  const domRes = await messageContent(tabId, { type: "GET_VIDEO_URL" });
  if (domRes && domRes.url) {
    log("Layer 1 found URL:", domRes.url);
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
      log("New tab created:", tab.id, tab.pendingUrl || tab.url || "(blank)");
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
        log("Layer 2 captured URL:", url);
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
  log("POSTing job to backend:", backendUrl);
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
      await setState({ backendMessage: "Starting up GPU (first launch takes ~30s)…" });
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
  const st = await getState();
  const opts = await getOptions();

  if (!st.classId || !st.tabId) {
    await setState({ status: "error", error: "No class page detected. Navigate to a Forum class page first." });
    return;
  }

  try {
    // 1 — Forum data
    await setState({ status: "fetching_data", error: null, files: null, jobId: null, backendMessage: null });
    // Show progress widget (small icon) on page so user sees feedback after popup closes
    try {
      chrome.tabs.sendMessage(st.tabId, { type: "SHOW_PROGRESS_UI" });
    } catch (_) { /* tab may have closed */ }
    const forum = await fetchForumData(st.tabId, st.classId);
    const sessionTitle = forum.classJson?.title || `Session ${st.classId}`;
    await setState({ classInfo: { sessionTitle } });

    // 2 — Video URL
    await setState({ status: "getting_video" });
    const videoUrl = await getVideoUrl(st.tabId);

    // 3 — Send to backend
    await setState({ status: "sending" });
    const jobId = await sendToBackend(opts.backendUrl, {
      class_id: st.classId,
      class_url: st.classUrl,
      forum_class_json: forum.classJson,
      forum_events_json: forum.eventsJson,
      signed_video_url: videoUrl,
      privacy_mode: opts.privacyMode,
      whisper_model: opts.whisperModel,
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

  if (msg.type === "GET_STATE") {
    (async () => {
      checkJobStatus();                       // fire-and-forget side-effect
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
    setOptions(msg.options).then(() => sendResponse({ ok: true }));
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
