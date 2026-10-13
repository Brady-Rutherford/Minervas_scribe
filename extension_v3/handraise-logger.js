// ============================================================
// handraise-logger.js — OPTIONAL, OFF BY DEFAULT: "log my own hand raises"
//
// Hand raises are not readable through any student API. In the live classroom, pressing H
// toggles your own hand (Forum PATCHes /api/v1/class-users/{id}; this script never calls it).
// When enabled in the popup, this script records ONLY your own H key presses on what looks
// like the live classroom page, with a best-effort read of your raise/lower button state.
// Nothing about classmates is recorded. Entries stay in chrome.storage.local until you
// export (JSON) or clear them from the popup. `scribe transcribe --hand-log FILE` puts them
// on the transcript timeline.
//
// UNVERIFIED: the live-classroom URL pattern and the button labels. Detection is heuristic
// (see isLiveClassroom / raisedIndicator) and documented in V3_README.md.
// ============================================================

(function () {
  if (globalThis.__scribeHandLogger) return;
  globalThis.__scribeHandLogger = true;

  const MAX_ENTRIES = 5000;
  let enabled = false;

  chrome.storage.sync.get("options", (r) => {
    enabled = Boolean(r && r.options && r.options.logHandRaises);
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "sync" && changes.options) {
      enabled = Boolean(changes.options.newValue && changes.options.newValue.logHandRaises);
    }
  });

  function isLiveClassroom() {
    if (/\/(live|classroom)(\/|$)/i.test(location.pathname)) return true;
    // Live classes show many video tiles; the recording page shows one player.
    return document.querySelectorAll("video").length >= 2;
  }

  function isTyping(target) {
    const el = target instanceof Element ? target : document.activeElement;
    if (!el) return false;
    if (el.isContentEditable) return true;
    return Boolean(el.closest("input, textarea, select, [contenteditable=''], [contenteditable='true']"));
  }

  // Best effort: your own toggle usually reads "Lower hand" while raised, "Raise hand" while not.
  function raisedIndicator() {
    const nodes = document.querySelectorAll("[aria-label], [title], button");
    for (const el of nodes) {
      const label = `${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""} ${
        el.tagName === "BUTTON" ? el.textContent || "" : ""}`.toLowerCase();
      if (/lower (my |your )?hand/.test(label)) return true;
      if (/raise (my |your )?hand/.test(label)) return false;
    }
    return null;
  }

  function classIdFromUrl() {
    const m = location.pathname.match(/\/classes\/(\d+)/);
    return m ? m[1] : null;
  }

  function record(entry) {
    chrome.storage.local.get("handRaiseLog", (r) => {
      const log = Array.isArray(r.handRaiseLog) ? r.handRaiseLog : [];
      log.push(entry);
      while (log.length > MAX_ENTRIES) log.shift();
      chrome.storage.local.set({ handRaiseLog: log });
    });
  }

  document.addEventListener("keydown", (e) => {
    if (!enabled || e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key !== "h" && e.key !== "H") return;
    if (isTyping(e.target) || !isLiveClassroom()) return;
    const t = Date.now();
    // Give the page a moment to update its own button, then read the state.
    setTimeout(() => {
      record({
        t,
        iso: new Date(t).toISOString(),
        classId: classIdFromUrl(),
        path: location.pathname,  // no query string
        raisedIndicator: raisedIndicator(),
      });
    }, 600);
  }, { capture: true, passive: true });
})();
