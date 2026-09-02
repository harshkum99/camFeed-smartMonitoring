/* Project Sanjay console.
   No framework and no build step — this has to run from a static directory on an edge box with
   no network, which rules out a CDN and makes a toolchain pure cost. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { cameras: [], zones: [], draft: null, camera: null };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json", "x-sanjay-actor": "console" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return res.json();
}

function showError(msg) {
  $("#error-slot").innerHTML = `<div class="err"><b>Something went wrong.</b> ${esc(msg)}</div>`;
}
const clearError = () => ($("#error-slot").innerHTML = "");

/* ---------- navigation ---------- */

const LOADERS = {
  alerts: loadAlerts, rules: loadRules, cameras: loadCameras,
  audit: loadAudit, zones: loadZonesView,
};

$("#nav").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-view]");
  if (!btn) return;
  const name = btn.dataset.view;
  $$("#nav button").forEach((b) => b.setAttribute("aria-current", String(b === btn)));
  $$(".view").forEach((v) => v.classList.toggle("on", v.id === `view-${name}`));
  clearError();
  LOADERS[name]?.().catch((e) => showError(e.message));
});

/* ---------- site state ---------- */

async function loadSiteState() {
  try {
    const [health, cov] = await Promise.all([api("/api/health"), api("/api/coverage?hours=24")]);
    const pct = Math.round(cov.coverage_pct * 100);
    // Coverage sits in the header on purpose. An operator who cannot see that a camera has been
    // dark since Tuesday reads every empty answer about it as good news.
    const cls = pct >= 95 ? "" : pct >= 80 ? "warn" : "crit";
    $("#site-state").innerHTML =
      `<span class="dot ${cls}"></span>` +
      `<span>${cov.cameras} cameras · ${pct}% covered (24h) · ` +
      `${health.interpreter === "GeminiProvider" ? "Gemini" : "offline interpreter"}</span>`;
  } catch (e) {
    $("#site-state").innerHTML = `<span class="dot crit"></span><span>API unreachable</span>`;
    showError(e.message);
  }
}

/* ---------- ask ---------- */

const EXAMPLES = [
  "How many workers entered Zone B without a helmet yesterday?",
  "Was the fire exit blocked at any point yesterday?",
  "Show me anyone at the canteen door on Tuesday.",
  "How many people came through Gate 3 this morning?",
];

$("#examples").innerHTML = EXAMPLES
  .map((q) => `<button class="btn sm ghost" data-q="${esc(q)}">${esc(q)}</button>`).join("");
$("#examples").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-q]");
  if (!b) return;
  $("#q").value = b.dataset.q;
  doAsk();
});

$("#ask-go").addEventListener("click", doAsk);
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") doAsk(); });

async function doAsk() {
  const question = $("#q").value.trim();
  if (!question) return;
  clearError();
  $("#ask-out").innerHTML = `<div class="card spinner">Searching the camera record…</div>`;
  try {
    renderAnswer(await api("/api/ask", { method: "POST", body: JSON.stringify({ question }) }));
  } catch (e) {
    $("#ask-out").innerHTML = "";
    showError(e.message);
  }
}

const REFUSAL_LABEL = {
  no_coverage: "No coverage", no_evidence: "No evidence",
  not_measured: "Not measured", low_confidence: "Low confidence",
};

function renderAnswer(a) {
  const refused = a.refused || a.abstain_reason;
  const cov = Math.round((a.coverage_pct ?? 0) * 100);
  const covCls = cov >= 95 ? "" : cov >= 80 ? "mid" : "low";

  let counts = "";
  // A bare count hides that the database counted whatever rows existed at whatever confidence.
  // The triple is the defensible form, so it is what the UI shows.
  if (!refused && a.total) {
    counts = `<div class="counts">
      <div><b>${a.confident}</b><small>confident</small></div>
      ${a.ambiguous ? `<div><b>${a.ambiguous}</b><small>need a look</small></div>` : ""}
      <div><b>${cov}%</b><small>camera coverage</small>
        <div class="bar"><i class="${covCls}" style="width:${cov}%"></i></div></div>
    </div>`;
  }

  const gaps = (a.gaps || []).length
    ? `<div class="note"><b>Coverage gaps:</b> ` +
      a.gaps.slice(0, 3).map((g) => `${esc(g.camera)} down ${g.minutes} min`).join("; ") +
      (a.gaps.length > 3 ? `, and ${a.gaps.length - 3} more` : "") + `.</div>` : "";

  const unresolved = (a.unresolved || []).length
    ? `<div class="note">Could not identify ${a.unresolved.map(esc).join(", ")} at this site.</div>`
    : "";

  const strip = (a.evidence || []).length ? `
    <div style="margin-top:.9rem">
      <div class="chip">${a.evidence.length} evidence frame(s)</div>
      <div class="strip">${a.evidence.slice(0, 12).map(frameCard).join("")}</div>
    </div>` : "";

  $("#ask-out").innerHTML = `
    <div class="card answer ${refused ? "refused" : ""}">
      ${refused ? `<div class="chip warn">${esc(REFUSAL_LABEL[a.abstain_reason] || "Cannot answer")}</div>` : ""}
      <p class="headline" style="margin-top:${refused ? ".5rem" : "0"}">${esc(a.message)}</p>
      ${counts}${gaps}${unresolved}${strip}
      ${a.describes ? `<div class="why"><b>Understood as:</b> ${esc(a.describes)}
         &nbsp;·&nbsp; ${a.latency_ms} ms</div>` : ""}
    </div>`;
}

function frameCard(e) {
  const t = new Date(e.ts);
  const attrs = Object.entries(e.attrs || {})
    .filter(([, v]) => typeof v === "number")
    .map(([k, v]) => `${k} ${v}`).join(" · ");
  return `<div class="frame">
    <div class="ph">frame</div>
    <div class="meta">
      <b>${t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</b>
      <span>${esc(e.camera)}</span><br>
      <span>conf ${e.confidence.toFixed(2)}${attrs ? " · " + esc(attrs) : ""}</span>
    </div></div>`;
}

/* ---------- alerts ---------- */

async function loadAlerts() {
  const rows = await api("/api/alerts?limit=50");
  if (!rows.length) {
    $("#alerts-out").innerHTML =
      `<div class="card empty">No alerts yet. Rules you activate will appear here.</div>`;
    return;
  }
  $("#alerts-out").innerHTML = rows.map(alertCard).join("");
}

function alertCard(a) {
  const t = new Date(a.ts);
  const done = a.feedback;
  return `<div class="card sev ${esc(a.severity)}">
    <div class="row" style="justify-content:space-between">
      <div>
        <b>${esc(a.rule || "Rule")}</b>
        <span class="chip ${a.severity === "critical" ? "crit" : a.severity === "warn" ? "warn" : ""}">${esc(a.severity)}</span>
        ${a.verified === true ? `<span class="chip ok">AI confirmed</span>` : ""}
        ${a.verified === false ? `<span class="chip warn">AI disagreed</span>` : ""}
        <div style="color:var(--muted);font-size:.83rem">
          ${esc(a.camera)} · <span class="mono">${t.toLocaleString()}</span></div>
      </div>
      <div class="row">
        ${done
          ? `<span class="chip ${done === "true_positive" ? "ok" : ""}">
               ${done === "true_positive" ? "Confirmed" : "Marked not an incident"}</span>`
          : `<button class="btn sm" data-alert="${esc(a.alert_id)}" data-actioned="true">Real incident</button>
             <button class="btn sm" data-alert="${esc(a.alert_id)}" data-actioned="false">Not an incident</button>`}
      </div>
    </div></div>`;
}

$("#alerts-out").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-alert]");
  if (!b) return;
  b.disabled = true;
  try {
    const r = await api(`/api/alerts/${b.dataset.alert}/feedback`, {
      method: "POST",
      body: JSON.stringify({ actioned: b.dataset.actioned === "true" }),
    });
    await loadAlerts();
    if (r.rule_muted) showError(r.message);
  } catch (err) { showError(err.message); }
});

/* ---------- rules ---------- */

$("#rule-draft").addEventListener("click", async () => {
  const instruction = $("#rule-text").value.trim();
  if (!instruction) return;
  clearError();
  $("#rule-preview").innerHTML = `<div class="spinner" style="margin-top:.8rem">Compiling…</div>`;
  $("#rule-save").disabled = true;
  try {
    const d = await api("/api/rules/draft", {
      method: "POST", body: JSON.stringify({ instruction }),
    });
    if (!d.ok) {
      state.draft = null;
      $("#rule-preview").innerHTML =
        `<div class="note" style="margin-top:.8rem"><b>I couldn't turn that into a rule.</b>
         ${esc(d.reason || "")} Try naming a place, a thing to watch for, and when.</div>`;
      return;
    }
    state.draft = d.rule;
    // The read-back is the whole safety mechanism: an operator confirms the sentence before
    // anything runs unattended for a month.
    $("#rule-preview").innerHTML = `
      <div class="card" style="margin-top:.9rem;margin-bottom:0">
        <div class="chip">Read this back before saving</div>
        <pre class="explain" style="margin-top:.6rem">${esc(d.explain)}</pre>
        ${(d.unresolved || []).length
          ? `<div class="note">Could not identify ${d.unresolved.map(esc).join(", ")}.</div>` : ""}
      </div>`;
    $("#rule-save").disabled = false;
  } catch (e) {
    $("#rule-preview").innerHTML = "";
    showError(e.message);
  }
});

$("#rule-save").addEventListener("click", async () => {
  if (!state.draft) return;
  try {
    await api("/api/rules", { method: "POST", body: JSON.stringify(state.draft) });
    $("#rule-text").value = "";
    $("#rule-preview").innerHTML = "";
    $("#rule-save").disabled = true;
    state.draft = null;
    await loadRules();
  } catch (e) { showError(e.message); }
});

async function loadRules() {
  const rows = await api("/api/rules");
  if (!rows.length) {
    $("#rules-out").innerHTML = `<div class="card empty">No rules yet.</div>`;
    return;
  }
  $("#rules-out").innerHTML = `<div class="card"><div class="tablewrap"><table>
    <thead><tr><th>Rule</th><th>Watches for</th><th>Severity</th>
      <th>Precision</th><th></th></tr></thead>
    <tbody>${rows.map(ruleRow).join("")}</tbody></table></div></div>`;
}

function ruleRow(r) {
  const t = r.trigger || {};
  const what = [t.type, t.object_class, t.zone && `in ${t.zone}`,
                t.dwell_seconds ? `${t.dwell_seconds}s` : null]
    .filter(Boolean).join(" · ");
  // Precision is in the list because a rule whose precision is falling is the one about to get
  // the whole product muted.
  const p = r.precision === null ? "—" : `${Math.round(r.precision * 100)}%`;
  const pc = r.precision === null ? "" : r.precision >= 0.8 ? "ok" : r.precision >= 0.5 ? "warn" : "crit";
  return `<tr>
    <td><b>${esc(r.name)}</b>${r.enabled ? "" : ` <span class="chip">disabled</span>`}</td>
    <td class="mono" style="font-size:.8rem">${esc(what)}</td>
    <td><span class="chip ${r.severity === "critical" ? "crit" : r.severity === "warn" ? "warn" : ""}">${esc(r.severity)}</span></td>
    <td><span class="chip ${pc}">${p}</span>
        <span style="color:var(--muted);font-size:.75rem">
        ${r.true_positives}✓ ${r.false_positives}✗</span></td>
    <td>${r.enabled ? `<button class="btn sm ghost" data-disable="${esc(r.rule_id)}">Disable</button>` : ""}</td>
  </tr>`;
}

$("#rules-out").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-disable]");
  if (!b) return;
  try {
    await api(`/api/rules/${b.dataset.disable}`, { method: "DELETE" });
    await loadRules();
  } catch (err) { showError(err.message); }
});

/* ---------- cameras ---------- */

const GRADE_TEXT = {
  recognition: ["ok", "Can identify enrolled faces"],
  anpr: ["ok", "Can read number plates"],
  detection: ["", "People, vehicles, zones and PPE"],
  degraded: ["warn", "Works, but costs about twice the appliance capacity"],
  unservable: ["crit", "Cannot be used as configured"],
};

async function loadCameras() {
  const rows = await api("/api/cameras");
  state.cameras = rows;
  if (!rows.length) {
    $("#cameras-out").innerHTML = `<div class="card empty">No cameras onboarded yet.</div>`;
    return;
  }
  $("#cameras-out").innerHTML = `<div class="card"><div class="tablewrap"><table>
    <thead><tr><th>Camera</th><th>What it can do</th><th>Stream</th>
      <th>Keyframe</th><th>Clock</th></tr></thead>
    <tbody>${rows.map(cameraRow).join("")}</tbody></table></div></div>`;
}

function cameraRow(c) {
  const [cls, text] = GRADE_TEXT[c.grade] || ["", c.grade];
  // A keyframe interval above 2s puts a floor under alert latency no matter how fast the
  // detector is, so it is shown rather than buried in a survey PDF.
  const gopBad = c.gop_ms && c.gop_ms > 2000;
  return `<tr>
    <td><b>${esc(c.name)}</b>${c.lens !== "rectilinear" ? ` <span class="chip">${esc(c.lens)}</span>` : ""}</td>
    <td><span class="chip ${cls}">${esc(c.grade)}</span>
        <div style="color:var(--muted);font-size:.78rem">${esc(text)}</div></td>
    <td class="mono" style="font-size:.8rem">${esc(c.resolution || "—")}${c.fps ? ` @ ${c.fps}fps` : ""}</td>
    <td>${c.gop_ms ? `<span class="chip ${gopBad ? "warn" : ""}">${c.gop_ms} ms</span>` : "—"}
        ${gopBad ? `<div style="color:var(--muted);font-size:.75rem">delays alerts</div>` : ""}</td>
    <td>${c.clock_ok ? `<span class="chip ok">ok</span>`
                     : `<span class="chip crit">${Math.round((c.clock_offset_ms || 0) / 1000)}s off</span>`}</td>
  </tr>`;
}

/* ---------- audit ---------- */

async function loadAudit() {
  const rows = await api("/api/audit?limit=60");
  if (!rows.length) {
    $("#audit-out").innerHTML = `<div class="card empty">No questions asked yet.</div>`;
    return;
  }
  $("#audit-out").innerHTML = `<div class="card"><div class="tablewrap"><table>
    <thead><tr><th>When</th><th>Who</th><th>Question</th><th>Outcome</th>
      <th>Coverage</th></tr></thead>
    <tbody>${rows.map((r) => `<tr>
      <td class="mono" style="font-size:.78rem">${new Date(r.at).toLocaleString()}</td>
      <td>${esc(r.actor)}</td>
      <td>${esc(r.question)}</td>
      <td>${r.refused ? `<span class="chip warn">${esc(REFUSAL_LABEL[r.reason] || "refused")}</span>`
                      : `<span class="chip ok">${r.rows} row(s)</span>`}</td>
      <td class="mono">${r.coverage_pct === null ? "—" : Math.round(r.coverage_pct * 100) + "%"}</td>
    </tr>`).join("")}</tbody></table></div></div>`;
}

/* ---------- zone editor ---------- */

const cv = $("#canvas");
const ctx = cv.getContext("2d");
let drawing = [];

const PALETTE = ["#1B6B57", "#A63F1C", "#8A6410", "#3A5FA8", "#6B3FA0", "#0F7B7B"];

async function loadZonesView() {
  if (!state.cameras.length) state.cameras = await api("/api/cameras");
  const sel = $("#zone-cam");
  if (!sel.options.length) {
    sel.innerHTML = state.cameras
      .map((c) => `<option value="${esc(c.camera_id)}">${esc(c.name)}</option>`).join("");
    sel.addEventListener("change", () => selectCamera(sel.value));
  }
  await selectCamera(sel.value || state.cameras[0]?.camera_id);
}

async function selectCamera(id) {
  if (!id) return;
  state.camera = id;
  state.zones = await api(`/api/cameras/${id}/zones`);
  drawing = [];
  renderZoneList();
  draw();
  refreshDrawState();
}

function renderZoneList() {
  const el = $("#zone-list");
  if (!state.zones.length) {
    el.innerHTML = `<div class="empty" style="padding:.8rem 0">No zones on this camera yet.</div>`;
    return;
  }
  el.innerHTML = state.zones.map((z, i) => `
    <div class="zone-item">
      <span class="swatch" style="background:${PALETTE[i % PALETTE.length]}"></span>
      <span class="name">${esc(z.name)}</span>
      <span class="chip">${esc(z.kind)}</span>
      <button class="btn sm ghost" data-del="${i}" title="Remove">×</button>
    </div>`).join("");
}

$("#zone-list").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-del]");
  if (!b) return;
  state.zones.splice(Number(b.dataset.del), 1);
  renderZoneList();
  draw();
});

function draw() {
  const w = cv.width, h = cv.height;
  ctx.clearRect(0, 0, w, h);

  // Placeholder frame. A real deployment paints the camera's latest keyframe here; the grid
  // exists so the editor is usable before any camera is connected.
  ctx.fillStyle = "#20262b";
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = "rgba(255,255,255,.06)";
  ctx.lineWidth = 1;
  for (let x = 0; x < w; x += 48) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
  for (let y = 0; y < h; y += 48) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  ctx.fillStyle = "rgba(255,255,255,.25)";
  ctx.font = "13px 'IBM Plex Mono', monospace";
  ctx.fillText("live frame appears here once the camera is connected", 18, h - 18);

  state.zones.forEach((z, i) => paint(z.polygon, PALETTE[i % PALETTE.length], z.name, z.kind));
  if (drawing.length) paint(drawing, "#E6EBE8", "", $("#zone-kind").value, true);
}

function paint(points, colour, label, kind, live = false) {
  if (!points.length) return;
  const pts = points.map(([x, y]) => [x * cv.width, y * cv.height]);
  ctx.lineWidth = 2;
  ctx.strokeStyle = colour;
  ctx.setLineDash(live ? [6, 4] : []);
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  if (kind !== "line" && !live) ctx.closePath();
  ctx.stroke();
  if (kind !== "line") {
    ctx.fillStyle = colour + "33";
    ctx.fill();
  }
  ctx.setLineDash([]);
  pts.forEach(([x, y]) => {
    ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fillStyle = colour; ctx.fill();
  });
  if (label) {
    ctx.fillStyle = colour;
    ctx.font = "600 14px 'IBM Plex Sans', sans-serif";
    ctx.fillText(label, pts[0][0] + 8, pts[0][1] - 8);
  }
}

cv.addEventListener("click", (e) => {
  const r = cv.getBoundingClientRect();
  // Stored normalised 0-1, so a zone keeps its meaning if the camera's resolution changes.
  drawing.push([(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height]);
  draw();
  refreshDrawState();
});

// Enter is a shortcut, never the only way through. Requiring a keystroke to finish a shape
// fails silently whenever focus is somewhere unexpected, and the operator is left clicking at a
// canvas that quietly does nothing.
document.addEventListener("keydown", (e) => {
  if (!$("#view-zones").classList.contains("on")) return;
  if (e.target.matches("input, textarea, select")) return;
  if (e.key === "Enter") { e.preventDefault(); commitZone(); }
  if (e.key === "Backspace" && drawing.length) {
    e.preventDefault(); drawing.pop(); draw(); refreshDrawState();
  }
  if (e.key === "Escape") { drawing = []; draw(); refreshDrawState(); }
});

$("#zone-add").addEventListener("click", commitZone);
$("#zone-name").addEventListener("input", refreshDrawState);
$("#zone-kind").addEventListener("change", () => { draw(); refreshDrawState(); });

function minPoints() {
  return $("#zone-kind").value === "line" ? 2 : 3;
}

/** Keep the button and the hint honest about what is still missing. Telling someone *why* the
 *  button is disabled is the difference between a UI that guides and one that stonewalls. */
function refreshDrawState() {
  const need = minPoints();
  const have = drawing.length;
  const named = $("#zone-name").value.trim().length > 0;
  const ready = have >= need && named;
  $("#zone-add").disabled = !ready;

  let msg;
  if (!have) msg = "Click on the frame to place points.";
  else if (have < need) msg = `${have} of ${need} points placed.`;
  else if (!named) msg = `${have} points placed — now give the zone a name.`;
  else msg = `${have} points placed. Add the shape, or press Enter.`;
  $("#draw-state").textContent = msg;
}

function commitZone() {
  const kind = $("#zone-kind").value;
  const need = minPoints();
  if (drawing.length < need) {
    showError(`A ${kind} needs at least ${need} points — click on the frame to place them.`);
    return;
  }
  const name = $("#zone-name").value.trim();
  if (!name) { showError("Give the zone a name first — rules refer to it by name."); return; }
  clearError();
  state.zones.push({ name, kind, polygon: drawing.slice(), direction: null });
  drawing = [];
  $("#zone-name").value = "";
  renderZoneList();
  draw();
  refreshDrawState();
}

$("#zone-save").addEventListener("click", async () => {
  try {
    await api(`/api/cameras/${state.camera}/zones`, {
      method: "PUT", body: JSON.stringify(state.zones),
    });
    clearError();
    $("#canvas-hint").innerHTML = `<b>Saved.</b> Rules can now refer to these zones by name.`;
  } catch (e) { showError(e.message); }
});

/* ---------- boot ---------- */
loadSiteState();
draw();
refreshDrawState();
