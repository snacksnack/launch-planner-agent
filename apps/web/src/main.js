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
// Subtle blue-family palette for epic identity (bars + legend + column accents).
// Ordered so adjacent epics alternate in lightness/hue; kept in the blue / teal /
// slate range so nothing clashes with the brand (no green / amber / purple).
const PALETTE = ["#2f6cb0", "#6ba0cf", "#3f8fa0", "#86a0b8", "#5f7896", "#4784c2", "#21507f", "#a8c0d4"];
const EPIC_COLOURS = PALETTE.length;
// Chart geometry — must match the frappe options below and --row-h in style.css.
const HEADER_H = 50;
const BAR_H = 18;
const PADDING = 14;
const ROW_H = BAR_H + PADDING;

let payload = null; // the /api/plan response
let gantt = null;
let byId = new Map(); // id -> task | milestone (for the detail panel)
let detailHint = ""; // the panel's initial placeholder, restored on close

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
  detailHint = document.querySelector("#detail").innerHTML;
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

function epicColour(epicId) {
  return PALETTE[epicIndex(epicId)];
}

// The ordered chart rows — tasks, then any scheduled milestones. Both the bars
// and the left task-name column derive from this, so they stay 1:1 aligned.
// (Unlinked milestones are skipped: they'd stretch the axis to far-future target
// dates and read as if scheduled — RC1-198.)
function chartRows() {
  const rows = payload.tasks.map((t) => ({
    id: t.id,
    name: t.name,
    epic_id: t.epic_id,
    is_critical: t.is_critical,
    kind: "task",
    start: t.start,
    end: t.end,
    dependencies: t.predecessors.map((p) => p.from).join(","),
  }));
  for (const m of payload.milestones) {
    if (!m.projected_date) continue;
    rows.push({
      id: m.id,
      name: `◆ ${m.name}`,
      epic_id: null,
      is_critical: false,
      kind: "milestone",
      start: m.projected_date,
      end: m.projected_date,
      dependencies: "",
    });
  }
  return rows;
}

function ganttTasks() {
  return chartRows().map((r) => ({
    id: r.id,
    name: r.name,
    start: r.start,
    end: r.end,
    progress: 0,
    dependencies: r.dependencies, // frappe draws an arrow from each predecessor
    custom_class:
      r.kind === "milestone"
        ? "milestone"
        : `epic-${epicIndex(r.epic_id)}${r.is_critical ? " critical" : ""}`,
  }));
}

function renderGantt(viewMode) {
  document.querySelector("#gantt").innerHTML = "";
  gantt = new Gantt("#gantt", ganttTasks(), {
    view_mode: viewMode,
    header_height: HEADER_H,
    bar_height: BAR_H,
    padding: PADDING,
    custom_popup_html: () => null, // we use our own detail panel instead
    on_click: (bar) => showDetail(bar.id),
  });
  requestAnimationFrame(() => {
    renderTaskColumn();
    drawScheduleOverlays();
  });
}

// The fixed left column of task names, aligned row-for-row with the bars. Task
// names live here (bar labels are hidden), which is the clean, canonical Gantt
// layout. Rows are positioned to the measured bar centres, so alignment holds
// regardless of frappe's internal geometry.
function renderTaskColumn() {
  const inner = document.querySelector("#task-column-inner");
  const svg = document.querySelector("#gantt svg");
  for (const old of inner.querySelectorAll(".task-row")) old.remove();
  if (!svg) return;
  inner.style.height = `${Number(svg.getAttribute("height")) || svg.getBBox().height}px`;

  for (const r of chartRows()) {
    const barRect = document.querySelector(`.bar-wrapper[data-id="${cssEscape(r.id)}"] .bar`);
    if (!barRect) continue;
    const centre = parseFloat(barRect.getAttribute("y")) + parseFloat(barRect.getAttribute("height")) / 2;
    const colour = r.kind === "milestone" ? "var(--text)" : epicColour(r.epic_id);
    barRect.style.fill = colour; // colour the bar by epic from the single palette source
    const row = document.createElement("div");
    row.className = `task-row${r.is_critical ? " critical" : ""}`;
    row.dataset.id = r.id;
    row.style.top = `${centre - ROW_H / 2}px`;
    row.innerHTML =
      `<span class="epic-accent" style="background:${colour}"></span>` +
      `<span class="task-name">${escapeHtml(r.name)}</span>`;
    row.addEventListener("click", () => showDetail(r.id));
    inner.appendChild(row);
  }
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
  setPanelCollapsed(false); // reveal the panel if it was collapsed
  const body = item.kind === "task" ? taskDetail(item) : milestoneDetail(item);
  el.innerHTML = `<button class="detail-close" aria-label="Close details" title="Close">×</button>${body}`;
  el.querySelector(".detail-close").addEventListener("click", clearDetail);

  // Highlight the selection in both the column and the chart.
  for (const sel of document.querySelectorAll(".selected")) sel.classList.remove("selected");
  for (const sel of document.querySelectorAll(`[data-id="${cssEscape(id)}"]`)) {
    sel.classList.add("selected");
  }
}

function clearDetail() {
  document.querySelector("#detail").innerHTML = detailHint;
  for (const sel of document.querySelectorAll(".selected")) sel.classList.remove("selected");
}

// Collapse/expand the right detail panel to reclaim timeline width.
function setPanelCollapsed(collapsed) {
  document.querySelector(".layout").classList.toggle("panel-collapsed", collapsed);
  const btn = document.querySelector("#panel-toggle");
  btn.textContent = collapsed ? "‹" : "›";
  btn.title = collapsed ? "Show details panel" : "Hide details panel";
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
  const wrap = document.querySelector("#gantt-wrap");
  // Dependency arrows web up a dense plan — hide them by default for a clean
  // read (per-task dependencies are always in the detail panel), toggle to show.
  wrap.classList.add("hide-deps");
  document.querySelector("#deps-toggle").addEventListener("change", (e) => {
    wrap.classList.toggle("hide-deps", !e.target.checked);
  });
  document.querySelector("#critical-toggle").addEventListener("change", (e) => {
    wrap.classList.toggle("critical-only", e.target.checked);
  });
  document.querySelector("#view-mode").addEventListener("change", (e) => {
    renderGantt(e.target.value);
  });
  document.querySelector("#panel-toggle").addEventListener("click", () => {
    const collapsed = document.querySelector(".layout").classList.contains("panel-collapsed");
    setPanelCollapsed(!collapsed);
  });
  // Own the click handling via delegation rather than frappe's on_click (which
  // is unreliable across versions). #gantt persists across re-renders.
  document.querySelector("#gantt").addEventListener("click", (e) => {
    const wrapper = e.target.closest(".bar-wrapper");
    if (wrapper) showDetail(wrapper.getAttribute("data-id"));
  });
  // Keep the left task column in vertical lockstep with the chart.
  wrap.addEventListener("scroll", () => {
    document.querySelector("#task-column-inner").style.transform =
      `translateY(${-wrap.scrollTop}px)`;
  });
  // Escape closes the detail panel.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") clearDetail();
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
