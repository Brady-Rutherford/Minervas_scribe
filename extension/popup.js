// ============================================================
// popup.js — UI logic for Class Transcriber popup
// ============================================================

const $ = (sel) => document.querySelector(sel);
let pollTimer = null;

// --------------- messaging helpers ---------------

function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, resolve);
  });
}

// --------------- visibility helpers ---------------

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

// --------------- render ---------------

async function render() {
  const resp = await send({ type: "GET_STATE" });
  if (!resp) return;
  const { state, options } = resp;
  if (!state) return;

  // Populate saved options
  if (options) {
    $("#backend-url").value  = options.backendUrl  || "http://localhost:5000";
    $("#privacy-mode").value = options.privacyMode || "names";
    $("#whisper-model").value = options.whisperModel || "medium";
  }

  const status   = state.status || "idle";
  const isActive = !["idle", "detected", "complete", "error"].includes(status);

  // ---------- no class ----------
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

  // ---------- class info ----------
  $("#class-title").textContent    = state.classInfo?.sessionTitle || "Class Session";
  $("#class-id-display").textContent = `ID: ${state.classId || "—"}`;

  // ---------- action button ----------
  show("action-section");
  const btn = $("#start-btn");
  if (isActive) {
    btn.disabled = true;
    btn.textContent = "Processing …";
  } else {
    btn.disabled = false;
    btn.textContent = "Start Transcription";
  }

  // ---------- status ----------
  if (isActive || status === "complete") {
    show("status-section");
    $("#status-text").textContent      = state.backendMessage || LABELS[status] || status;
    $("#progress-fill").style.width    = (PROGRESS[status] ?? 0) + "%";
  } else {
    hide("status-section");
  }

  // ---------- files ----------
  if (status === "complete" && state.files) {
    show("files-section");
    const base = (options?.backendUrl || "http://localhost:5000").replace(/\/+$/, "");
    const list = $("#file-list");
    list.innerHTML = "";
    [...(state.files.pdfs || []), ...(state.files.csvs || [])].forEach((name) => {
      const a = document.createElement("a");
      a.href = `${base}/api/download/${state.jobId}/${name}`;
      a.target = "_blank";
      a.textContent = name;
      list.appendChild(a);
    });
  } else {
    hide("files-section");
  }

  // ---------- error ----------
  if (status === "error") {
    show("error-section");
    $("#error-text").textContent = state.error || "Unknown error";
    hide("status-section");
  } else {
    hide("error-section");
  }

  // ---------- polling ----------
  if (isActive) startPolling(); else stopPolling();
}

function startPolling() {
  if (!pollTimer) pollTimer = setInterval(render, 2500);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// --------------- events ---------------

document.addEventListener("DOMContentLoaded", () => {
  render();

  // Start
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

  // Reset
  $("#retry-btn").addEventListener("click", () => {
    send({ type: "RESET" });
    setTimeout(render, 400);
  });

  // Persist option changes
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
});

// Respond to background state broadcasts
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "STATE_CHANGED") render();
});
