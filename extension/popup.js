const $ = (sel) => document.querySelector(sel);
let pollTimer = null;

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

  if (options) {
    $("#backend-url").value  = options.backendUrl  || "http://localhost:5001";
    $("#privacy-mode").value = options.privacyMode || "names";
    $("#whisper-model").value = options.whisperModel || "medium";
  }

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
    const base = (options?.backendUrl || "http://localhost:5001").replace(/\/+$/, "");
    const list = $("#file-list");
    list.innerHTML = "";
    [...(state.files.pdfs || []), ...(state.files.csvs || [])].forEach((name) => {
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

    const base = (entry.backendUrl || "http://localhost:5001").replace(/\/+$/, "");
    const allFiles = [...(entry.files?.pdfs || []), ...(entry.files?.csvs || [])];
    const links = allFiles.map((name) => {
      const url = `${base}/api/download/${entry.jobId}/${name}`;
      return `<a href="#" data-url="${url}" data-name="${name}">${name}</a>`;
    }).join("");

    card.innerHTML = `
      <div class="he-title">${entry.sessionTitle || "Class Session"}</div>
      <div class="he-date">${formatDate(entry.completedAt)}</div>
      <div class="he-id">Class ID: ${entry.classId || "—"}</div>
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

// --------------- events ---------------

document.addEventListener("DOMContentLoaded", () => {
  render();

  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => switchTab(t.dataset.tab));
  });

  $("#start-btn").addEventListener("click", () => {
    const opts = {
      backendUrl:   $("#backend-url").value.replace(/\/+$/, ""),
      privacyMode:  $("#privacy-mode").value,
      whisperModel: $("#whisper-model").value,
    };
    send({ type: "UPDATE_OPTIONS", options: opts });
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

  ["backend-url", "privacy-mode", "whisper-model"].forEach((id) => {
    $(`#${id}`).addEventListener("change", () => {
      send({
        type: "UPDATE_OPTIONS",
        options: {
          backendUrl:   $("#backend-url").value.replace(/\/+$/, ""),
          privacyMode:  $("#privacy-mode").value,
          whisperModel: $("#whisper-model").value,
        },
      });
    });
  });

  $("#clear-history-btn").addEventListener("click", async () => {
    await chrome.storage.local.remove("transcription_history");
    renderHistory();
  });
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "STATE_CHANGED") render();
});
