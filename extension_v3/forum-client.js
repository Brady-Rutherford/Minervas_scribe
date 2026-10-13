// ============================================================
// forum-client.js — Minerva's Scribe v3 Forum API client (content script)
//
// Every request is a same-origin fetch from the Forum page, so the session cookies are
// attached by the browser and NEVER leave it (nothing is copied to the backend).
//
// Verified endpoints (student account, Oct 2026):
//   /api/v1/users/self?show_inactive=false
//   /api/v1/class_grader/classes/{id}                 (+ /class-events /polls /poll-responses
//                                                      /outcome-assessments /focused-outcomes
//                                                      /breakouts)
//   /api/v1/recording-sessions/{CLASS_ID}/generate_recording_urls  -> {highres, lowres}
//   /api/v1/analytics/class/{id}, /api/v1/analytics/section/{id}   (403 for students)
// Never touched: /api/v1/class-users/{id} (PATCH-only; it stores live hand-raise state).
// ============================================================

(function () {
  if (globalThis.ScribeForumClient) return;  // re-injection guard

  const API = "/api/v1";
  const FORBIDDEN = /^\/api\/v1\/class-users(\/|$)/;

  async function get(path) {
    if (FORBIDDEN.test(path.split("?")[0])) {
      throw new Error(`Refusing to call ${path.split("?")[0]} (write-only endpoint)`);
    }
    const r = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!r.ok) {
      const e = new Error(`${path.split("?")[0]} returned HTTP ${r.status}`);
      e.status = r.status;
      throw e;
    }
    return r.json();
  }

  async function tryGet(path) {
    try {
      return { data: await get(path), status: 200 };
    } catch (e) {
      if (e.status === 401) {
        e.message = "Your Forum session has expired. Reload the page and log in again.";
        throw e;
      }
      return { data: null, status: e.status || "network" };
    }
  }

  async function me() {
    const u = await get(`${API}/users/self?show_inactive=false`);
    return { id: u.id ?? null, role: u.role ?? null };  // ids only: no names leave here
  }

  const PARTS = {
    class: (id) => `${API}/class_grader/classes/${id}`,
    events: (id) => `${API}/class_grader/classes/${id}/class-events`,
    polls: (id) => `${API}/class_grader/classes/${id}/polls`,
    poll_responses: (id) => `${API}/class_grader/classes/${id}/poll-responses`,
    outcome_assessments: (id) => `${API}/class_grader/classes/${id}/outcome-assessments`,
    focused_outcomes: (id) => `${API}/class_grader/classes/${id}/focused-outcomes`,
    breakouts: (id) => `${API}/class_grader/classes/${id}/breakouts`,
    analytics: (id) => `${API}/analytics/class/${id}`,  // staff only; 403 for students
  };

  function checkId(classId) {
    const id = String(classId || "");
    if (!/^\d+$/.test(id)) throw new Error(`Bad class id: ${id}`);
    return id;
  }

  async function classBundle(classId) {
    const id = checkId(classId);
    const keys = Object.keys(PARTS);
    const results = await Promise.all(keys.map((k) => tryGet(PARTS[k](id))));
    const bundle = {
      schema: "scribe.bundle/v1",
      class_id: Number(id),
      fetched_at: new Date().toISOString(),
      status: {},
    };
    keys.forEach((k, i) => {
      bundle[k] = results[i].data;
      bundle.status[k] = results[i].status;
    });
    try { bundle.me = await me(); } catch (_) { bundle.me = null; }
    if (!bundle.class) {
      const e = new Error(`Could not read class ${id} (HTTP ${bundle.status.class}).`);
      e.status = bundle.status.class;
      throw e;
    }
    return bundle;
  }

  async function recordingUrls(classId) {
    // NOTE: the path parameter is the CLASS id. The URLs are signed and expire: fetch them
    // right before use and never log them.
    const id = checkId(classId);
    const r = await get(`${API}/recording-sessions/${id}/generate_recording_urls`);
    return {
      highres: typeof r?.highres === "string" ? r.highres : null,
      lowres: typeof r?.lowres === "string" ? r.lowres : null,
    };
  }

  async function probeCapabilities(classId, sectionId) {
    const id = checkId(classId);
    const who = await me();
    const probes = {
      outcome_assessments: PARTS.outcome_assessments(id),
      breakouts: PARTS.breakouts(id),
      analytics: PARTS.analytics(id),
    };
    if (sectionId && /^\d+$/.test(String(sectionId))) {
      probes.analytics_section = `${API}/analytics/section/${sectionId}`;
    }
    const endpoints = {};
    for (const [k, p] of Object.entries(probes)) endpoints[k] = (await tryGet(p)).status;
    return { me: who, endpoints };
  }

  globalThis.ScribeForumClient = { get, tryGet, me, classBundle, recordingUrls, probeCapabilities };
})();
