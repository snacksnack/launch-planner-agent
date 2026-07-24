// Interactive Gantt for a scheduled launch plan (RC1-188).
//
// Data flow: PRD -> agents -> CPM engine -> GET /api/plan -> this view.
// The backend serves the scheduled flagship golden by default, so this renders
// end-to-end with no credentials. frappe-gantt draws the bars + dependency
// arrows; we layer on epic colours, a critical-path highlight/toggle, deadline
// markers, freeze shading, and a provenance detail panel (the audit trail).
import Gantt from "frappe-gantt";
import "frappe-gantt/dist/frappe-gantt.css";
import "./style.css";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";
// Epic palette — index matches the .epic-N bar rules in style.css.
const PALETTE = ["#4e79a7", "#59a14f", "#b07aa1", "#f28e2b", "#76b7b2", "#edc948", "#ff9da7", "#9c755f"];
const EPIC_COLOURS = PALETTE.length;

let payload = null; // the /api/plan response
let gantt = null;
let byId = new Map(); // id -> task | milestone (for the detail panel)

init();

async function init() {
  try {
    const resp = await fetch(`${API_BASE}/api/plan`);
    if (!resp.ok) throw new Error(`API ${resp.status}`);
    payload = await resp.json();
  } catch (err) {
    document.querySelector("#project-meta").textContent =
      `Could not reach the API at ${API_BASE} — is uvicorn running? (${err.message})`;
    return;
  }
  index();
  renderHeader();
  renderGantt("Week");
  wireControls();
}

function index() {
  byId = new Map();
  for (const t of payload.tasks) byId.set(t.id, { ...t, kind: "task" });
  for (const m of payload.milestones) byId.set(m.id, { ...m, kind: "milestone" });
}

function renderHeader() {
  const p = payload.project;
  document.querySelector("#project-name").textContent = p.name;
  const deadlineNote = payload.deadlines
    .map((d) => `${d.task_id} vs ${d.deadline}: ${d.met ? "OK" : "MISS"} (${signed(d.slack_working_days)}d)`)
    .join(" · ");
  document.querySelector("#project-meta").innerHTML =
    `Projected finish <strong>${p.finish_date}</strong> · ` +
    `${p.duration_working_days} working days from ${p.start_date} · ` +
    `critical path ${p.critical_path_ids.length} tasks` +
    (deadlineNote ? ` · <span class="deadline-note">${deadlineNote}</span>` : "");

  document.querySelector("#legend").innerHTML = payload.epics
    .map(
      (e, i) =>
        `<span class="chip"><span class="dot" style="background:${PALETTE[i % PALETTE.length]}"></span>${escapeHtml(e.name)}</span>`,
    )
    .join("");
}

// --- Gantt rendering -------------------------------------------------------

function epicIndex(epicId) {
  const i = payload.epics.findIndex((e) => e.id === epicId);
  return i < 0 ? 0 : i % EPIC_COLOURS;
}

function ganttTasks() {
  const bars = payload.tasks.map((t) => ({
    id: t.id,
    name: t.name,
    start: t.start,
    end: t.end,
    progress: 0,
    // frappe draws an arrow from each dependency (predecessor) to this bar.
    dependencies: t.predecessors.map((p) => p.from).join(","),
    custom_class: `epic-${epicIndex(t.epic_id)}${t.is_critical ? " critical" : ""}`,
  }));

  // Milestones as markers — only ones the scheduler actually placed. Unlinked
  // milestones (no dependency edge yet — RC1-198) would otherwise stretch the
  // axis out to their far-future target dates and read as if scheduled.
  for (const m of payload.milestones) {
    if (!m.projected_date) continue;
    bars.push({
      id: m.id,
      name: `◆ ${m.name}`,
      start: m.projected_date,
      end: m.projected_date,
      progress: 0,
      custom_class: "milestone",
    });
  }
  return bars;
}

function renderGantt(viewMode) {
  document.querySelector("#gantt").innerHTML = "";
  gantt = new Gantt("#gantt", ganttTasks(), {
    view_mode: viewMode,
    bar_height: 18,
    padding: 14,
    custom_popup_html: () => null, // we use our own detail panel instead
    on_click: (bar) => showDetail(bar.id),
  });
  requestAnimationFrame(drawScheduleOverlays);
}

// Deadline lines + freeze shading, drawn directly into frappe's SVG so they
// scroll with the chart. Positions are calibrated from two real bars, so this
// stays aligned regardless of frappe's internal scale. Best-effort: any failure
// is swallowed — the textual summary in the header carries the same facts.
function drawScheduleOverlays() {
  try {
    const svg = document.querySelector("#gantt svg");
    if (!svg) return;
    const project = svg.querySelector(".grid .grid-background") || svg;
    const height = Number(project.getAttribute("height")) || svg.getBBox().height;

    const map = calibrateDateToX();
    if (!map) return;
    const ns = "http://www.w3.org/2000/svg";

    for (const f of payload.freezes) {
      const x1 = map(f.start);
      const x2 = map(f.end);
      const rect = document.createElementNS(ns, "rect");
      rect.setAttribute("x", Math.min(x1, x2));
      rect.setAttribute("y", 0);
      rect.setAttribute("width", Math.abs(x2 - x1));
      rect.setAttribute("height", height);
      rect.setAttribute("class", "freeze-shade");
      svg.appendChild(rect);
    }

    for (const d of payload.deadlines) {
      const x = map(d.deadline);
      const line = document.createElementNS(ns, "line");
      line.setAttribute("x1", x);
      line.setAttribute("x2", x);
      line.setAttribute("y1", 0);
      line.setAttribute("y2", height);
      line.setAttribute("class", d.met ? "deadline-line met" : "deadline-line miss");
      svg.appendChild(line);
    }
  } catch (err) {
    console.warn("overlay drawing skipped:", err);
  }
}

// Read two rendered bars, solve x = a*days + b, return a date->x function.
function calibrateDateToX() {
  const bars = payload.tasks
    .map((t) => {
      const rect = document.querySelector(`.bar-wrapper[data-id="${cssEscape(t.id)}"] .bar`);
      return rect ? { date: t.start, x: Number(rect.getAttribute("x")) } : null;
    })
    .filter(Boolean);
  if (bars.length < 2) return null;

  bars.sort((p, q) => day(p.date) - day(q.date));
  const lo = bars[0];
  const hi = bars[bars.length - 1];
  const span = day(hi.date) - day(lo.date);
  if (span === 0) return null;
  const a = (hi.x - lo.x) / span;
  const b = lo.x - a * day(lo.date);
  return (dateStr) => a * day(dateStr) + b;
}

// --- detail panel ----------------------------------------------------------

function showDetail(id) {
  const item = byId.get(id);
  const el = document.querySelector("#detail");
  if (!item) {
    el.innerHTML = `<p class="hint">No detail for ${id}.</p>`;
    return;
  }
  el.innerHTML = item.kind === "task" ? taskDetail(item) : milestoneDetail(item);
}

function taskDetail(t) {
  const est = t.estimate;
  return `
    <h2>${escapeHtml(t.name)}</h2>
    <dl class="facts">
      <dt>Epic</dt><dd>${escapeHtml(t.epic_name ?? "—")}</dd>
      <dt>Owner</dt><dd>${escapeHtml(t.owner_name ?? "unassigned")}</dd>
      <dt>Dates</dt><dd>${t.start} → ${t.end}</dd>
      <dt>Estimate</dt><dd>${est.optimistic} / ${est.likely} / ${est.pessimistic} days (o/l/p)</dd>
      <dt>Float</dt><dd>total ${t.total_float}, free ${t.free_float}${t.is_critical ? " · <span class=\"crit\">critical</span>" : ""}</dd>
    </dl>
    ${provenanceBlock("Why this task", t.provenance)}
    ${dependencyBlock(t.predecessors)}
  `;
}

function milestoneDetail(m) {
  const slack = m.slack_working_days == null ? "—" : `${signed(m.slack_working_days)} working days`;
  return `
    <h2>◆ ${escapeHtml(m.name)}</h2>
    <dl class="facts">
      <dt>Target</dt><dd>${m.target_date ?? "—"}</dd>
      <dt>Projected</dt><dd>${m.projected_date ?? "not scheduled (no dependencies yet)"}</dd>
      <dt>Slack</dt><dd>${slack}</dd>
    </dl>
    ${m.provenance ? provenanceBlock("Why this milestone", m.provenance) : ""}
  `;
}

function dependencyBlock(predecessors) {
  if (!predecessors.length) return "";
  const rows = predecessors
    .map((p) => {
      const name = byId.get(p.from)?.name ?? p.from;
      return `
        <li>
          <div class="dep-head">depends on <strong>${escapeHtml(name)}</strong>
            <span class="conf conf-${p.provenance.confidence}">${p.provenance.confidence}</span>
          </div>
          <blockquote>${escapeHtml(p.provenance.source_quote)}</blockquote>
        </li>`;
    })
    .join("");
  return `<h3>Dependencies</h3><ul class="deps">${rows}</ul>`;
}

function provenanceBlock(title, prov) {
  return `
    <h3>${title}</h3>
    <p class="reasoning">${escapeHtml(prov.reasoning)}</p>
    <blockquote>${escapeHtml(prov.source_quote)}</blockquote>
    <p class="prov-meta">
      <span class="conf conf-${prov.confidence}">${prov.confidence}</span>
      ${prov.source_section ? `· ${escapeHtml(prov.source_section)}` : ""}
      · ${escapeHtml(prov.agent)} / ${escapeHtml(prov.model)}
    </p>
  `;
}

// --- controls --------------------------------------------------------------

function wireControls() {
  document.querySelector("#critical-toggle").addEventListener("change", (e) => {
    document.querySelector("#gantt-wrap").classList.toggle("critical-only", e.target.checked);
  });
  document.querySelector("#view-mode").addEventListener("change", (e) => {
    renderGantt(e.target.value);
  });
  // Own the click handling via delegation rather than frappe's on_click (which
  // is unreliable across versions). #gantt persists across re-renders.
  document.querySelector("#gantt").addEventListener("click", (e) => {
    const wrapper = e.target.closest(".bar-wrapper");
    if (wrapper) showDetail(wrapper.getAttribute("data-id"));
  });
}

// --- helpers ---------------------------------------------------------------

function day(dateStr) {
  return Math.round(new Date(`${dateStr}T00:00:00Z`).getTime() / 86400000);
}

function signed(n) {
  return n > 0 ? `+${n}` : `${n}`;
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : value;
}

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}
