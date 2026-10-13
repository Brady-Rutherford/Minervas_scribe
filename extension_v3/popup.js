const $ = (sel) => document.querySelector(sel);
let pollTimer = null;
const LOCAL_BACKEND = "http://127.0.0.1:5001";

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// v3 backends list every output under files.all; v2 backends only send pdfs/csvs.
function allFiles(files) {
  if (!files) return [];
  if (Array.isArray(files.all) && files.all.length) return files.all;
  return [...(files.pdfs || []), ...(files.csvs || [])];
}

function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, resolve);
  });
}

function show(id) { document.getElementById(id).hidden = false; }
function hide(id) { document.getElementById(id).hidden = true; }

// --------------- progress mapping ---------------

const PROGRESS = {
  idle: 0, detected: 0,
  fetching_data: 15, getting_video: 30, sending: 45,
  transcribing: 65, compiling: 85,
  complete: 100, error: 0,
};

const LABELS = {
  idle:           "Waiting for class page …",
  detected:       "Ready — press Start",
  fetching_data:  "Fetching class data from Forum …",
  getting_video:  "Acquiring video URL …",
  sending:        "Sending to backend …",
  transcribing:   "Transcribing — this may take a while …",
  compiling:      "Compiling outputs …",
  complete:       "Done!",
  error:          "Error",
};

// --------------- tabs ---------------

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  document.querySelectorAll(".tab-content").forEach((tc) => {
    tc.hidden = tc.id !== `tab-${name}`;
  });
  if (name === "history") renderHistory();
  if (name === "settings") renderSettings();
}

// --------------- download helper ---------------

function downloadFile(url, filename) {
  if (chrome.downloads) {
    chrome.downloads.download({
      url: url,
      filename: `MinervaTranscripts/${filename}`,
      saveAs: false,
    });
  } else {
    window.open(url, "_blank");
  }
}

// --------------- render (transcribe tab) ---------------

async function render() {
  const resp = await send({ type: "GET_STATE" });
  if (!resp) return;
  const { state, options } = resp;
  if (!state) return;

  const status   = state.status || "idle";
  const isActive = !["idle", "detected", "complete", "error"].includes(status);

  if (status === "idle" && !state.classId) {
    show("no-class");
    hide("class-info"); hide("options-section"); hide("action-section");
    hide("status-section"); hide("files-section"); hide("error-section");
    stopPolling();
    return;
  }

  hide("no-class");
  show("class-info");
  show("options-section");

  $("#class-title").textContent    = state.classInfo?.sessionTitle || "Class Session";
  $("#class-id-display").textContent = `ID: ${state.classId || "—"}`;

  show("action-section");
  const btn = $("#start-btn");
  if (isActive) {
    btn.disabled = true;
    btn.textContent = "Processing …";
  } else {
    btn.disabled = false;
    btn.innerHTML = "Start Transcription &rarr;";
  }

  if (isActive || status === "complete") {
    show("status-section");
    $("#status-text").textContent   = state.backendMessage || LABELS[status] || status;
    $("#progress-fill").style.width = (PROGRESS[status] ?? 0) + "%";
  } else {
    hide("status-section");
  }

  if (status === "complete" && state.files) {
    show("files-section");
    const base = (options?.backendUrl || LOCAL_BACKEND).replace(/\/+$/, "");
    const list = $("#file-list");
    list.innerHTML = "";
    allFiles(state.files).forEach((name) => {
      const a = document.createElement("a");
      const url = `${base}/api/download/${state.jobId}/${name}`;
      a.href = "#";
      a.textContent = name;
      a.addEventListener("click", (e) => { e.preventDefault(); downloadFile(url, name); });
      list.appendChild(a);
    });
  } else {
    hide("files-section");
  }

  if (status === "error") {
    show("error-section");
    $("#error-text").textContent = state.error || "Unknown error";
    hide("status-section");
  } else {
    hide("error-section");
  }

  if (isActive) startPolling(); else stopPolling();
}

function startPolling() {
  if (!pollTimer) pollTimer = setInterval(render, 2500);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// --------------- history tab ---------------

function formatDate(isoStr) {
  try {
    const d = new Date(isoStr);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
      + " at " + d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  } catch { return isoStr; }
}

async function renderHistory() {
  const result = await chrome.storage.local.get("transcription_history");
  const history = result.transcription_history || [];
  const list = document.getElementById("history-list");
  const empty = document.getElementById("history-empty");
  const clearBtn = document.getElementById("clear-history-btn");

  list.innerHTML = "";

  if (history.length === 0) {
    empty.hidden = false;
    clearBtn.hidden = true;
    return;
  }

  empty.hidden = true;
  clearBtn.hidden = false;

  history.forEach((entry) => {
    const card = document.createElement("div");
    card.className = "history-entry";

    const base = (entry.backendUrl || LOCAL_BACKEND).replace(/\/+$/, "");
    const links = allFiles(entry.files).map((name) => {
      const url = `${base}/api/download/${encodeURIComponent(entry.jobId)}/${encodeURIComponent(name)}`;
      return `<a href="#" data-url="${esc(url)}" data-name="${esc(name)}">${esc(name)}</a>`;
    }).join("");

    card.innerHTML = `
      <div class="he-title">${esc(entry.sessionTitle || "Class Session")}</div>
      <div class="he-date">${esc(formatDate(entry.completedAt))}</div>
      <div class="he-id">Class ID: ${esc(entry.classId || "—")}</div>
      <div class="he-files">${links || "<span style='color:#9CA3AF;font-size:11px'>No files</span>"}</div>
    `;

    card.querySelectorAll("a[data-url]").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        downloadFile(a.dataset.url, a.dataset.name);
      });
    });

    list.appendChild(card);
  });
}

// --------------- settings tab (v3) ---------------

async function renderSettings() {
  const resp = await send({ type: "GET_OPTIONS" });
  const o = resp || {};
  const preset = o.backendPreset || "local";
  $("#opt-backend").value = preset;
  $("#opt-url").value = o.backendUrl || LOCAL_BACKEND;
  $("#custom-url-field").hidden = preset !== "custom";
  $("#opt-privacy").value = o.privacyMode || "names";
  $("#opt-res").value = o.videoRes || "lowres";
  $("#opt-provider").value = o.provider || "auto";
  $("#opt-pdf").checked = Boolean(o.includePdf);
  $("#opt-hands").checked = Boolean(o.logHandRaises);
  checkBackend();
  renderHandCount();
}

async function saveSettings() {
  const preset = $("#opt-backend").value;
  $("#custom-url-field").hidden = preset !== "custom";
  const options = {
    backendPreset: preset,
    privacyMode: $("#opt-privacy").value,
    videoRes: $("#opt-res").value,
    provider: $("#opt-provider").value,
    includePdf: $("#opt-pdf").checked,
    logHandRaises: $("#opt-hands").checked,
  };
  if (preset === "custom") options.backendUrl = $("#opt-url").value.trim() || LOCAL_BACKEND;
  await send({ type: "UPDATE_OPTIONS", options });
  $("#settings-saved").hidden = false;
  setTimeout(() => { $("#settings-saved").hidden = true; }, 1200);
  checkBackend();
}

async function checkBackend() {
  const dot = $("#health-dot");
  dot.className = "health-dot health-checking";
  const r = await send({ type: "CHECK_BACKEND" });
  dot.className = "health-dot " + (r && r.ok ? "health-ok" : "health-fail");
  dot.title = r && r.ok ? `backend ok${r.version ? " (v" + r.version + ")" : ""}` : "backend unreachable";
}

async function renderHandCount() {
  const r = await chrome.storage.local.get("handRaiseLog");
  const n = (r.handRaiseLog || []).length;
  $("#hand-count").textContent = `${n} entr${n === 1 ? "y" : "ies"} stored locally`;
}

async function exportHandLog() {
  const r = await chrome.storage.local.get("handRaiseLog");
  const data = { schema: "scribe.handlog/v1", exported_at: new Date().toISOString(),
                 entries: r.handRaiseLog || [] };
  const blob = new Blob([JSON.stringify(data, null, 1)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `scribe-hand-raises-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// --------------- events ---------------

async function ensureContentScript() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url || !tab.url.match(/forum\.minerva\.edu\/app\/courses\/\d+\/sections\/\d+\/classes\/\d+/)) return;
    await new Promise((resolve) => {
      chrome.tabs.sendMessage(tab.id, { type: "PING" }, (resp) => {
        if (chrome.runtime.lastError || !resp || !resp.pong) {
          chrome.scripting.executeScript(
            { target: { tabId: tab.id }, files: ["forum-client.js", "content.js"] }, resolve);
        } else {
          resolve();
        }
      });
    });
    await new Promise((r) => setTimeout(r, 300));
  } catch (_) { /* no permission or not a Forum tab */ }
}

async function refreshClassFromPage() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !tab.url?.match(/forum\.minerva\.edu\/app\/courses\/\d+\/sections\/\d+\/classes\/\d+/)) return;
    const info = await new Promise((resolve) => {
      chrome.tabs.sendMessage(tab.id, { type: "GET_CURRENT_CLASS" }, (resp) => {
        if (chrome.runtime.lastError) { resolve(null); return; }
        resolve(resp);
      });
    });
    if (info && info.classId) {
      await send({
        type: "REFRESH_CLASS",
        classId: info.classId,
        courseId: info.courseId,
        sectionId: info.sectionId,
        url: info.url,
        tabId: tab.id,
      });
    }
  } catch (_) {}
}

document.addEventListener("DOMContentLoaded", async () => {
  await ensureContentScript();
  await refreshClassFromPage();
  render();

  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => switchTab(t.dataset.tab));
  });

  $("#start-btn").addEventListener("click", () => {
    send({ type: "START_TRANSCRIPTION" });
    $("#start-btn").disabled = true;
    $("#start-btn").textContent = "Starting …";
    startPolling();
    setTimeout(render, 600);
  });

  $("#retry-btn").addEventListener("click", () => {
    send({ type: "RESET" });
    setTimeout(render, 400);
  });

  ["#opt-backend", "#opt-privacy", "#opt-res", "#opt-provider", "#opt-pdf", "#opt-hands"]
    .forEach((sel) => $(sel).addEventListener("change", saveSettings));
  $("#opt-url").addEventListener("change", saveSettings);
  $("#hand-export").addEventListener("click", exportHandLog);
  $("#hand-clear").addEventListener("click", async () => {
    await chrome.storage.local.remove("handRaiseLog");
    renderHandCount();
  });

  $("#clear-history-btn").addEventListener("click", async () => {
    await chrome.storage.local.remove("transcription_history");
    renderHistory();
  });
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "STATE_CHANGED") render();
});
