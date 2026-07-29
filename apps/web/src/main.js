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

let payload = null; // the baseline /api/plan response
let view = null; // the payload currently rendered (baseline, or the simulated one)
let gantt = null;
let byId = new Map(); // id -> task | milestone (for the detail panel)
let depById = new Map(); // dep id -> {from, to} (to name dependency flags)
let detailHint = ""; // the panel's initial placeholder, restored on close
let currentViewMode = "Week";

// Simulation (RC1-190) state.
let simActive = false;
let simResult = null; // { baseline, simulated, delta, warnings }
let scenarioChanges = []; // the what-if changes being composed

// Baseline / plan-vs-actual (RC1-192) state.
let baselineActive = false;
let baselineResult = null; // { baseline, current, comparison }

// Both simulate and baseline render current bars over ghosted reference bars, and
// both take over the right rail — so bar/row clicks defer to their panels.
function inOverlayMode() {
  return simActive || baselineActive;
}

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
  view = payload;
  detailHint = document.querySelector("#detail").innerHTML;
  index();
  renderHeader();
  renderGantt(currentViewMode);
  wireControls();
}

function index() {
  byId = new Map();
  for (const t of view.tasks) byId.set(t.id, { ...t, kind: "task" });
  for (const m of view.milestones) byId.set(m.id, { ...m, kind: "milestone" });
  // dep id -> its endpoints, so a flag on a dependency can read as "A → B"
  // (task names) instead of an opaque id like "dep-plan-tooling".
  depById = new Map();
  for (const t of view.tasks) {
    for (const p of t.predecessors) depById.set(p.id, { from: p.from, to: t.id });
  }
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
  const rows = view.tasks.map((t) => ({
    id: t.id,
    name: t.name,
    epic_id: t.epic_id,
    is_critical: t.is_critical,
    kind: "task",
    start: t.start,
    end: t.end,
    dependencies: t.predecessors.map((p) => p.from).join(","),
  }));
  for (const m of view.milestones) {
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
  currentViewMode = viewMode;
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
    if (simActive && simResult) {
      drawGhostOverlay(simResult.baseline.tasks, simResult.delta.task_shifts);
    } else if (baselineActive && baselineResult) {
      drawGhostOverlay(
        baselineResult.baseline.payload.tasks,
        baselineResult.comparison.schedule_delta.task_shifts,
      );
    }
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
    // Low-confidence extractions carry a subtle amber flag (honest-gaps ethos),
    // so a reviewer sees where the agent was unsure without opening each item.
    const lowConf = byId.get(r.id)?.provenance?.confidence === "low";
    const row = document.createElement("div");
    row.className = `task-row${r.is_critical ? " critical" : ""}${lowConf ? " low-conf" : ""}`;
    row.dataset.id = r.id;
    row.style.top = `${centre - ROW_H / 2}px`;
    row.innerHTML =
      `<span class="epic-accent" style="background:${colour}"></span>` +
      `<span class="task-name">${escapeHtml(r.name)}</span>` +
      (lowConf ? `<span class="low-conf-flag" title="low-confidence extraction">⚑</span>` : "");
    row.addEventListener("click", () => {
      if (!inOverlayMode()) showDetail(r.id); // overlay modes own the rail
    });
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

    for (const f of view.freezes) {
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

    for (const d of view.deadlines) {
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
  const bars = view.tasks
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

// --- decision record (the audit of how the plan was built) -----------------

// Total number of items a reviewer might want to look at.
function decisionCount() {
  const d = payload?.decisions;
  if (!d) return 0;
  return d.rejected_edges.length + d.cycle_breaks.length + d.flagged.length + d.coverage_gaps.length;
}

// Friendly labels for validation codes; unknown codes fall back to the raw code.
const FLAG_LABELS = {
  "low-confidence": "Low confidence",
  "unverifiable-quote": "Unverifiable quote",
  "unenforced-gate": "Unenforced gate",
  "orphan-task": "No dependencies",
  "unassigned-task": "Unassigned",
  "dependency-cycle": "Dependency cycle",
  "unknown-owner": "Unknown owner",
  "unknown-epic": "Unknown epic",
};

function nameFor(id) {
  return byId.get(id)?.name ?? id;
}

// Turn a flag's entity id into a human, clickable subject: a task/milestone name
// (jumps to its detail), or a dependency's "Predecessor → Successor" names
// (jumps to the successor, whose panel lists the edge). Falls back to the raw id.
function flagSubject(entityId) {
  if (!entityId) return "";
  if (byId.has(entityId)) {
    return `<a href="#" data-jump="${escapeHtml(entityId)}">${escapeHtml(nameFor(entityId))}</a>`;
  }
  const edge = depById.get(entityId);
  if (edge) {
    return `<a href="#" data-jump="${escapeHtml(edge.to)}">${escapeHtml(nameFor(edge.from))} → ${escapeHtml(nameFor(edge.to))}</a>`;
  }
  return escapeHtml(entityId);
}

// Render the decision record into the detail panel: dropped/cut edges, the
// deterministic validation flags, and PRD sections nothing cited.
function showDecisions() {
  setPanelCollapsed(false);
  const d = payload?.decisions;
  const el = document.querySelector("#detail");
  for (const sel of document.querySelectorAll(".selected")) sel.classList.remove("selected");

  const sections = [];

  if (d?.rejected_edges.length) {
    const rows = d.rejected_edges
      .map(
        (r) =>
          `<li><div class="dec-head"><strong>${escapeHtml(nameFor(r.predecessor_id))}</strong> → <strong>${escapeHtml(nameFor(r.successor_id))}</strong>
            <span class="dec-code">${escapeHtml(r.code)}</span></div>
            <p class="dec-reason">${escapeHtml(r.reason)}</p></li>`,
      )
      .join("");
    sections.push(`<h3>Edges dropped (${d.rejected_edges.length})</h3><ul class="dec-list">${rows}</ul>`);
  }

  if (d?.cycle_breaks.length) {
    const rows = d.cycle_breaks
      .map(
        (c) =>
          `<li><div class="dec-head">broke cycle <span class="dec-code">cut ${escapeHtml(c.predecessor_id)} → ${escapeHtml(c.successor_id)}</span></div>
            <p class="dec-reason">${escapeHtml(c.cycle.join(" → "))} → ${escapeHtml(c.cycle[0] ?? "")}</p></li>`,
      )
      .join("");
    sections.push(`<h3>Cycles broken (${d.cycle_breaks.length})</h3><ul class="dec-list">${rows}</ul>`);
  }

  if (d?.flagged.length) {
    const rows = d.flagged
      .map((f) => {
        const label = FLAG_LABELS[f.code] ?? f.code;
        return `<li><div class="dec-head"><span class="conf conf-${f.severity === "error" ? "low" : "medium"}">${escapeHtml(label)}</span> ${flagSubject(f.entity_id)}</div>
          <p class="dec-reason">${escapeHtml(f.message)}</p></li>`;
      })
      .join("");
    sections.push(`<h3>Validation flags (${d.flagged.length})</h3><ul class="dec-list">${rows}</ul>`);
  }

  if (d?.coverage_gaps.length) {
    const rows = d.coverage_gaps.map((g) => `<li>${escapeHtml(g)}</li>`).join("");
    sections.push(`<h3>PRD sections uncited (${d.coverage_gaps.length})</h3><ul class="dec-gaps">${rows}</ul>`);
  }

  const body = sections.length
    ? sections.join("")
    : `<p class="hint">No decisions to review — the plan validated clean: no dropped edges, no low-confidence extractions, every quote verbatim.</p>`;

  el.innerHTML =
    `<button class="detail-close" aria-label="Close" title="Close">×</button>` +
    `<h2>Decisions & validation</h2>` +
    `<p class="reasoning">What the agents proposed and what Python accepted, rejected, or flagged — the audit behind this plan.</p>` +
    body;
  el.querySelector(".detail-close").addEventListener("click", clearDetail);
  for (const a of el.querySelectorAll("[data-jump]")) {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      showDetail(a.getAttribute("data-jump"));
    });
  }
}

// Collapse/expand the right detail panel to reclaim timeline width.
function setPanelCollapsed(collapsed) {
  document.querySelector(".layout").classList.toggle("panel-collapsed", collapsed);
  const btn = document.querySelector("#panel-toggle");
  btn.textContent = collapsed ? "‹" : "›";
  btn.title = collapsed ? "Show details panel" : "Hide details panel";
}

// A "Jira" row once a task has been pushed to the board (RC1-200): a clickable
// link when we know the site URL, or the bare key otherwise.
function jiraRow(t) {
  if (!t.jira_key) return "";
  const value = t.jira_url
    ? `<a class="jira-link" href="${escapeHtml(t.jira_url)}" target="_blank" rel="noopener">${escapeHtml(t.jira_key)} ↗</a>`
    : escapeHtml(t.jira_key);
  return `<dt>Jira</dt><dd>${value}</dd>`;
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
      ${jiraRow(t)}
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

// --- audit trail: "How this plan was made" (RC1-195) -----------------------

const AGENT_LABEL = {
  "work-breakdown": "Work Breakdown agent",
  dependency: "Dependency agent",
  raid: "RAID agent",
  status: "Status agent",
};
const KIND_LABEL = { epic: "epic", task: "task", dependency: "dependency", milestone: "milestone", raid: "RAID item" };
const SNAP_LABEL = { proposal: "Agent proposal", commit: "Committed", baseline: "Baseline set" };

function plural(n, word) {
  if (n === 1) return `${n} ${word}`;
  const p = /[^aeiou]y$/.test(word) ? `${word.slice(0, -1)}ies` : `${word}s`;
  return `${n} ${p}`;
}

async function showAudit() {
  setPanelCollapsed(false);
  const el = document.querySelector("#detail");
  for (const sel of document.querySelectorAll(".selected")) sel.classList.remove("selected");
  el.innerHTML =
    `<button class="detail-close" title="Close">×</button><h2>How this plan was made</h2>` +
    `<p class="hint">Loading…</p>`;
  el.querySelector(".detail-close").addEventListener("click", clearDetail);

  let data;
  try {
    const resp = await fetch(`${API_BASE}/api/audit`);
    if (!resp.ok) throw new Error(`API ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    el.innerHTML =
      `<button class="detail-close" title="Close">×</button><h2>How this plan was made</h2>` +
      `<p class="hint">Couldn't load: ${escapeHtml(err.message)}</p>`;
    el.querySelector(".detail-close").addEventListener("click", clearDetail);
    return;
  }
  renderAuditPanel(data);
}

function renderAuditPanel(data) {
  const el = document.querySelector("#detail");
  const d = data.decisions;

  const agentRows = data.agents
    .map((a) => {
      const kinds = Object.entries(a.kinds)
        .map(([k, n]) => plural(n, KIND_LABEL[k] ?? k))
        .join(" · ");
      return `<li><div class="audit-head"><strong>${escapeHtml(AGENT_LABEL[a.agent] ?? a.agent)}</strong>
        <span class="audit-model">${escapeHtml(a.model)}</span></div>
        <p class="audit-detail">proposed ${escapeHtml(kinds)}</p></li>`;
    })
    .join("");

  const dropped = (d.rejected_edges?.length ?? 0) + (d.cycle_breaks?.length ?? 0);
  const valBits = [
    plural(d.flagged?.length ?? 0, "flag"),
    `${dropped} edge${dropped === 1 ? "" : "s"} dropped/cut`,
    plural(d.coverage_gaps?.length ?? 0, "PRD section") + " uncited",
  ];

  const timeline = data.history.length
    ? `<ul class="audit-timeline">${data.history
        .map(
          (h) =>
            `<li><span class="audit-snap audit-snap-${h.kind}">${SNAP_LABEL[h.kind] ?? h.kind}</span>
              <div><strong>v${h.version}</strong>${h.approved_by ? ` · ${escapeHtml(h.approved_by)}` : ""}
              <span class="audit-hash">${escapeHtml(h.content_hash)}</span>
              ${h.message ? `<div class="audit-detail">${escapeHtml(h.message)}</div>` : ""}</div></li>`,
        )
        .join("")}</ul>`
    : `<p class="hint">No committed history on this instance yet.</p>`;

  el.innerHTML =
    `<button class="detail-close" aria-label="Close" title="Close">×</button>` +
    `<h2>How this plan was made</h2>` +
    `<p class="reasoning">The reasoning chain, end to end: agents proposed, Python validated, a human approved — every step inspectable.</p>` +
    `<h3>1 · Agents proposed</h3><ul class="audit-list">${agentRows}</ul>` +
    `<h3>2 · Python validated</h3>` +
    `<p class="audit-detail">${valBits.join(" · ")}. <a href="#" id="audit-to-decisions">See the decisions →</a></p>` +
    `<h3>3 · Human approved</h3>${timeline}`;

  el.querySelector(".detail-close").addEventListener("click", clearDetail);
  const toDec = el.querySelector("#audit-to-decisions");
  if (toDec) toDec.addEventListener("click", (e) => { e.preventDefault(); showDecisions(); });
}

// --- RAID log (RC1-191) ----------------------------------------------------

let raidFilter = "all"; // all | risk | assumption | issue | decision

function raidCount() {
  return payload?.raid?.length ?? 0;
}

const RAID_LABEL = { risk: "Risk", assumption: "Assumption", issue: "Issue", decision: "Decision" };

function severityBand(sev) {
  if (sev == null) return null;
  if (sev >= 15) return "high";
  if (sev >= 8) return "med";
  return "low";
}

// Render the RAID log into the detail panel: filter chips by type, sorted by
// severity, each item with its evidence (PRD quote or schedule fact).
function showRaid() {
  setPanelCollapsed(false);
  const el = document.querySelector("#detail");
  for (const sel of document.querySelectorAll(".selected")) sel.classList.remove("selected");
  const items = payload?.raid ?? [];

  const counts = items.reduce((m, r) => ((m[r.type] = (m[r.type] || 0) + 1), m), {});
  const chip = (key, label, n) =>
    `<button class="raid-chip${raidFilter === key ? " active" : ""}" data-filter="${key}">${label}${n != null ? ` <span class="raid-n">${n}</span>` : ""}</button>`;
  const chips = [
    chip("all", "All", items.length),
    ...["risk", "assumption", "issue", "decision"]
      .filter((t) => counts[t])
      .map((t) => chip(t, RAID_LABEL[t], counts[t])),
  ].join("");

  el.innerHTML =
    `<button class="detail-close" aria-label="Close" title="Close">×</button>` +
    `<h2>RAID log</h2>` +
    `<p class="reasoning">Risks, assumptions, issues, and decisions — each traceable to a PRD quote or a computed schedule fact.</p>` +
    `<div class="raid-toolbar"><div class="raid-chips">${chips}</div><button id="raid-export" class="toolbtn">Copy as Markdown</button></div>` +
    `<div id="raid-list">${renderRaidList()}</div>`;

  el.querySelector(".detail-close").addEventListener("click", clearDetail);
  for (const b of el.querySelectorAll("[data-filter]")) {
    b.addEventListener("click", () => {
      raidFilter = b.dataset.filter;
      showRaid();
    });
  }
  el.querySelector("#raid-export").addEventListener("click", copyRaidMarkdown);
}

function filteredRaid() {
  const items = (payload?.raid ?? []).filter((r) => raidFilter === "all" || r.type === raidFilter);
  // Risks first (by severity desc), then the rest in a stable type order.
  const order = { risk: 0, issue: 1, assumption: 2, decision: 3 };
  return items.slice().sort((a, b) => {
    if (a.type !== b.type) return order[a.type] - order[b.type];
    return (b.severity ?? 0) - (a.severity ?? 0);
  });
}

function renderRaidList() {
  const items = filteredRaid();
  if (!items.length) return `<p class="hint">No items for this filter.</p>`;
  return items.map(raidCard).join("");
}

function raidCard(r) {
  const band = severityBand(r.severity);
  const sev =
    r.severity != null
      ? `<span class="raid-sev raid-sev-${band}">P×I ${r.severity}</span>`
      : "";
  const ev = r.provenance.evidence;
  const evidence =
    ev.kind === "prd"
      ? `<blockquote>${escapeHtml(ev.source_quote)}</blockquote>`
      : `<p class="raid-fact">⛓ ${escapeHtml(ev.statement)}</p>`;
  const owner = r.suggested_owner_name
    ? `<div class="raid-meta"><dt>Owner</dt><dd>${escapeHtml(r.suggested_owner_name)}</dd></div>`
    : "";
  const action =
    r.type === "risk" && r.mitigation
      ? `<div class="raid-meta"><dt>Mitigation</dt><dd>${escapeHtml(r.mitigation)}</dd></div>`
      : r.type === "decision" && r.rationale
        ? `<div class="raid-meta"><dt>Rationale</dt><dd>${escapeHtml(r.rationale)}</dd></div>`
        : "";
  return `
    <div class="raid-card">
      <div class="raid-head">
        <span class="raid-type raid-type-${r.type}">${RAID_LABEL[r.type]}</span>
        <strong>${escapeHtml(r.title)}</strong>
        ${sev}
      </div>
      <p class="raid-desc">${escapeHtml(r.description)}</p>
      ${owner}
      ${action}
      ${evidence}
      <p class="prov-meta">
        <span class="conf conf-${r.provenance.confidence}">${r.provenance.confidence}</span>
        · ${ev.kind === "prd" ? escapeHtml(ev.source_section ?? "PRD") : escapeHtml(ev.fact_code)}
      </p>
    </div>`;
}

// Export the (filtered) RAID log as Markdown to the clipboard.
function raidMarkdown() {
  const items = filteredRaid();
  const lines = [`# RAID log — ${payload.project.name}`, ""];
  for (const r of items) {
    const sev = r.severity != null ? ` (P×I ${r.severity})` : "";
    lines.push(`## [${RAID_LABEL[r.type]}] ${r.title}${sev}`);
    lines.push("");
    lines.push(r.description);
    if (r.suggested_owner_name) lines.push(`- **Owner:** ${r.suggested_owner_name}`);
    if (r.type === "risk" && r.mitigation) lines.push(`- **Mitigation:** ${r.mitigation}`);
    if (r.type === "decision" && r.rationale) lines.push(`- **Rationale:** ${r.rationale}`);
    const ev = r.provenance.evidence;
    lines.push(
      ev.kind === "prd"
        ? `- **Evidence (PRD):** "${ev.source_quote}"`
        : `- **Evidence (schedule):** ${ev.statement}`,
    );
    lines.push(`- **Confidence:** ${r.provenance.confidence}`);
    lines.push("");
  }
  return lines.join("\n");
}

async function copyRaidMarkdown() {
  const btn = document.querySelector("#raid-export");
  try {
    await navigator.clipboard.writeText(raidMarkdown());
    const prev = btn.textContent;
    btn.textContent = "Copied ✓";
    setTimeout(() => (btn.textContent = prev), 1500);
  } catch {
    btn.textContent = "Copy failed";
  }
}

// --- Jira generation preview (RC1-193) -------------------------------------

let jiraGen = null; // the generation plan from /api/jira
let jiraSelected = null; // Set of selected local_ids (partial approval)

async function showJira() {
  setPanelCollapsed(false);
  const el = document.querySelector("#detail");
  for (const sel of document.querySelectorAll(".selected")) sel.classList.remove("selected");
  el.innerHTML =
    `<button class="detail-close" title="Close">×</button><h2>Generate Jira</h2>` +
    `<p class="hint">Loading preview…</p>`;
  el.querySelector(".detail-close").addEventListener("click", clearDetail);

  let data;
  try {
    const resp = await fetch(`${API_BASE}/api/jira`);
    if (!resp.ok) throw new Error(`API ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    el.innerHTML =
      `<button class="detail-close" title="Close">×</button><h2>Generate Jira</h2>` +
      `<p class="hint">Couldn't load the preview: ${escapeHtml(err.message)}</p>`;
    el.querySelector(".detail-close").addEventListener("click", clearDetail);
    return;
  }

  jiraGen = data.generation;
  jiraSelected = new Set(jiraGen.issues.map((op) => op.local_id)); // default: all approved
  renderJiraPanel(data);
}

function renderJiraPanel(data) {
  const el = document.querySelector("#detail");
  const gen = jiraGen;
  const epics = gen.issues.filter((op) => op.issue_type === "Epic");
  const stories = gen.issues.filter((op) => op.issue_type === "Story");

  const issueRow = (op) =>
    `<li class="jira-op">
      <label>
        <input type="checkbox" data-op="${escapeHtml(op.local_id)}" ${jiraSelected.has(op.local_id) ? "checked" : ""} />
        <span class="jira-type jira-type-${op.issue_type.toLowerCase()}">${op.action === "update" ? "~" : "+"} ${op.issue_type}</span>
        <span class="jira-summary">${escapeHtml(op.summary)}</span>
      </label>
      ${op.due_date ? `<span class="jira-due">due ${op.due_date}</span>` : ""}
    </li>`;

  el.innerHTML =
    `<button class="detail-close" aria-label="Close" title="Close">×</button>` +
    `<h2>Generate Jira</h2>` +
    `<p class="jira-note">🔒 Mock preview — no writes. ${data.has_credentials ? "" : "Real mode needs Jira credentials and "}runs only via the gated CLI below.</p>` +
    `<p class="reasoning">${data.creates} create · ${data.updates} update · ${data.links} link(s) into <strong>${escapeHtml(gen.project_key)}</strong>. Every description carries the provenance audit.</p>` +
    (epics.length
      ? `<h3>Epics (${epics.length})</h3><ul class="jira-list">${epics.map(issueRow).join("")}</ul>`
      : "") +
    (stories.length
      ? `<h3>Stories (${stories.length})</h3><ul class="jira-list">${stories.map(issueRow).join("")}</ul>`
      : "") +
    (gen.links.length
      ? `<h3>Links (${gen.links.length})</h3><ul class="jira-links">${gen.links
          .map(
            (l) =>
              `<li>${escapeHtml(nameFor(l.outward_local_id))} <span class="jira-blocks">blocks</span> ${escapeHtml(nameFor(l.inward_local_id))}</li>`,
          )
          .join("")}</ul>`
      : "") +
    `<h3>Apply the selection</h3><pre class="jira-cmd" id="jira-cmd"></pre>` +
    `<button id="jira-copy" class="toolbtn">Copy command</button>`;

  el.querySelector(".detail-close").addEventListener("click", clearDetail);
  for (const cb of el.querySelectorAll("[data-op]")) {
    cb.addEventListener("change", () => {
      if (cb.checked) jiraSelected.add(cb.dataset.op);
      else jiraSelected.delete(cb.dataset.op);
      updateJiraCommand();
    });
  }
  el.querySelector("#jira-copy").addEventListener("click", copyJiraCommand);
  updateJiraCommand();
}

function jiraCommand() {
  const partial = jiraSelected.size < jiraGen.issues.length;
  const only = partial ? ` --only ${[...jiraSelected].join(",")}` : "";
  return `plan jira <plan.json> --start-date ${payload.project.start_date} --project ${jiraGen.project_key}${only} --real --confirm`;
}

function updateJiraCommand() {
  const el = document.querySelector("#jira-cmd");
  if (el) el.textContent = jiraSelected.size ? jiraCommand() : "(nothing selected)";
}

async function copyJiraCommand() {
  const btn = document.querySelector("#jira-copy");
  try {
    await navigator.clipboard.writeText(jiraCommand());
    const prev = btn.textContent;
    btn.textContent = "Copied ✓";
    setTimeout(() => (btn.textContent = prev), 1500);
  } catch {
    btn.textContent = "Copy failed";
  }
}

// --- weekly status update (RC1-194) ----------------------------------------

const STATUS_LABEL = { green: "On track", yellow: "At risk", red: "Off track" };
let statusMarkdown = "";

async function showStatus() {
  setPanelCollapsed(false);
  const el = document.querySelector("#detail");
  for (const sel of document.querySelectorAll(".selected")) sel.classList.remove("selected");
  el.innerHTML =
    `<button class="detail-close" title="Close">×</button><h2>Status update</h2>` +
    `<p class="hint">Loading…</p>`;
  el.querySelector(".detail-close").addEventListener("click", clearDetail);

  let data;
  try {
    const resp = await fetch(`${API_BASE}/api/status`);
    if (!resp.ok) throw new Error(`API ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    el.innerHTML =
      `<button class="detail-close" title="Close">×</button><h2>Status update</h2>` +
      `<p class="hint">Couldn't load: ${escapeHtml(err.message)}</p>`;
    el.querySelector(".detail-close").addEventListener("click", clearDetail);
    return;
  }

  if (!data.baseline) {
    el.innerHTML =
      `<button class="detail-close" title="Close">×</button><h2>Status update</h2>` +
      `<p class="hint">No baseline set yet. Commit one with <code>plan baseline &lt;plan&gt; --by &lt;you&gt; --note "initial"</code>; the weekly status is measured against it.</p>`;
    el.querySelector(".detail-close").addEventListener("click", clearDetail);
    return;
  }
  renderStatusPanel(data);
}

function renderStatusPanel(data) {
  const el = document.querySelector("#detail");
  const f = data.facts;
  const n = data.narrative;
  statusMarkdown = data.markdown;

  const changed = n.points.length
    ? `<ul class="status-points">${n.points.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`
    : `<p class="hint">No material changes since the baseline.</p>`;

  el.innerHTML =
    `<button class="detail-close" aria-label="Close" title="Close">×</button>` +
    `<h2>Status update</h2>` +
    `<p class="reasoning">${escapeHtml(f.period_label)} · vs baseline v${data.baseline.version}. Health is set by rule, not the LLM.</p>` +
    `<div class="status-health status-${f.health}">
       <span class="status-badge">${STATUS_LABEL[f.health]}</span>
       <span class="status-reasons">${escapeHtml(f.health_reasons.join("; "))}</span>
     </div>` +
    `<p class="status-summary">${escapeHtml(n.exec_summary)}</p>` +
    `<h3>What changed since last week</h3>${changed}` +
    `<button id="status-copy" class="toolbtn">Copy as Markdown</button>`;

  el.querySelector(".detail-close").addEventListener("click", clearDetail);
  el.querySelector("#status-copy").addEventListener("click", copyStatusMarkdown);
}

async function copyStatusMarkdown() {
  const btn = document.querySelector("#status-copy");
  try {
    await navigator.clipboard.writeText(statusMarkdown);
    const prev = btn.textContent;
    btn.textContent = "Copied ✓";
    setTimeout(() => (btn.textContent = prev), 1500);
  } catch {
    btn.textContent = "Copy failed";
  }
}

// --- slippage simulator (RC1-190) ------------------------------------------

function toggleSimMode() {
  if (simActive) exitSimMode();
  else enterSimMode();
}

function enterSimMode() {
  if (baselineActive) exitBaselineMode();
  simActive = true;
  simResult = null;
  scenarioChanges = [];
  document.querySelector("#simulate-btn").classList.add("active");
  updateSimBanner();
  renderSimPanel();
}

function exitSimMode() {
  simActive = false;
  simResult = null;
  scenarioChanges = [];
  document.querySelector("#simulate-btn").classList.remove("active");
  document.querySelector("#sim-banner").hidden = true;
  view = payload;
  index();
  renderGantt(currentViewMode);
  clearDetail();
}

function describeChange(c) {
  if (c.kind === "delay_task") return `${nameFor(c.task_id)} slips ${c.days}d`;
  const verb = c.kind === "add_dependency" ? "add" : "remove";
  return `${verb} ${nameFor(c.predecessor_id)} → ${nameFor(c.successor_id)}`;
}

function describeScenario() {
  return scenarioChanges.map(describeChange).join("; ");
}

// POST the composed scenario, swap the rendered view to the simulated schedule,
// and refresh the banner + panel. Empty scenario falls back to the baseline.
async function runSimulation() {
  if (!scenarioChanges.length) {
    simResult = null;
    view = payload;
    index();
    renderGantt(currentViewMode);
    updateSimBanner();
    renderSimPanel();
    return;
  }
  try {
    const resp = await fetch(`${API_BASE}/api/simulate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ changes: scenarioChanges }),
    });
    if (!resp.ok) throw new Error(`API ${resp.status}`);
    simResult = await resp.json();
  } catch (err) {
    document.querySelector("#sim-impact").innerHTML =
      `<p class="hint">Simulation failed: ${escapeHtml(err.message)}</p>`;
    return;
  }
  view = simResult.simulated;
  index();
  renderGantt(currentViewMode); // rAF draws the ghost baseline overlay
  updateSimBanner();
  renderSimPanel();
}

function updateSimBanner() {
  const banner = document.querySelector("#sim-banner");
  const text = document.querySelector("#sim-banner-text");
  if (!simActive) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  if (!simResult) {
    banner.className = "";
    text.textContent = "Simulate mode — compose a what-if in the panel to see its impact.";
    return;
  }
  const d = simResult.delta;
  const missed = d.deadline_flips.some((f) => f.met_before && !f.met_after);
  banner.className = missed ? "miss" : d.finish_shift_days > 0 ? "slip" : "ok";
  text.innerHTML = `<strong>${escapeHtml(describeScenario())}</strong> — ${escapeHtml(d.headline)}`;
}

function renderSimPanel() {
  setPanelCollapsed(false);
  const el = document.querySelector("#detail");
  const taskOpts = payload.tasks
    .map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name)}</option>`)
    .join("");
  const nodeOpts = [...payload.tasks, ...payload.milestones]
    .map((n) => `<option value="${escapeHtml(n.id)}">${escapeHtml(n.name)}</option>`)
    .join("");
  const changeList = scenarioChanges.length
    ? `<ul class="sim-changes">${scenarioChanges
        .map(
          (c, i) =>
            `<li><span>${escapeHtml(describeChange(c))}</span><button class="sim-x" data-rm="${i}" title="Remove" aria-label="Remove change">×</button></li>`,
        )
        .join("")}</ul>`
    : `<p class="hint">No changes yet — slip a task or edit a dependency below.</p>`;

  el.innerHTML = `
    <button class="detail-close" aria-label="Exit simulate" title="Exit simulate">×</button>
    <h2>Simulate — what if?</h2>
    <p class="reasoning">Compose hypothetical changes; the schedule recomputes deterministically and the timeline shows the shift against a ghost of the baseline.</p>
    <h3>Scenario</h3>
    ${changeList}
    <div class="sim-form">
      <label class="sim-label" for="sim-task">Slip a task</label>
      <div class="sim-row">
        <select id="sim-task">${taskOpts}</select>
        <input id="sim-days" type="number" min="1" step="1" value="5" aria-label="Working days" />
        <button id="sim-add-slip" class="toolbtn">Add</button>
      </div>
    </div>
    <div class="sim-form">
      <label class="sim-label" for="sim-pred">Dependency edge</label>
      <div class="sim-row">
        <select id="sim-pred">${nodeOpts}</select>
        <span>→</span>
        <select id="sim-succ">${nodeOpts}</select>
      </div>
      <div class="sim-row">
        <button id="sim-add-dep" class="toolbtn">Add edge</button>
        <button id="sim-remove-dep" class="toolbtn">Remove edge</button>
      </div>
    </div>
    <div id="sim-impact">${renderImpact()}</div>
  `;

  el.querySelector(".detail-close").addEventListener("click", exitSimMode);
  el.querySelector("#sim-add-slip").addEventListener("click", () => {
    const task_id = el.querySelector("#sim-task").value;
    const days = Number(el.querySelector("#sim-days").value);
    if (task_id && days > 0) {
      scenarioChanges.push({ kind: "delay_task", task_id, days });
      runSimulation();
    }
  });
  const addEdge = (kind) => {
    const predecessor_id = el.querySelector("#sim-pred").value;
    const successor_id = el.querySelector("#sim-succ").value;
    if (predecessor_id && successor_id) {
      scenarioChanges.push({ kind, predecessor_id, successor_id });
      runSimulation();
    }
  };
  el.querySelector("#sim-add-dep").addEventListener("click", () => addEdge("add_dependency"));
  el.querySelector("#sim-remove-dep").addEventListener("click", () => addEdge("remove_dependency"));
  for (const b of el.querySelectorAll("[data-rm]")) {
    b.addEventListener("click", () => {
      scenarioChanges.splice(Number(b.dataset.rm), 1);
      runSimulation();
    });
  }
}

// The detailed impact (the one-line headline lives in the banner).
function renderImpact() {
  if (!simResult) return "";
  const d = simResult.delta;
  const parts = [];

  if (d.notes.length) {
    parts.push(`<ul class="sim-notes">${d.notes.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul>`);
  }

  const missed = d.deadline_flips.filter((f) => f.met_before && !f.met_after);
  if (missed.length) {
    const rows = missed
      .map((f) => `<li><span class="conf conf-low">deadline missed</span> ${escapeHtml(f.constraint_id)} (${signed(f.slack_after)}d)</li>`)
      .join("");
    parts.push(`<h3>Deadlines breached (${missed.length})</h3><ul class="dec-list">${rows}</ul>`);
  }

  const moved = d.task_shifts.filter((s) => s.finish_shift_days !== 0);
  if (moved.length) {
    const rows = moved
      .slice()
      .sort((a, b) => b.finish_shift_days - a.finish_shift_days)
      .map(
        (s) =>
          `<li><span class="sim-moved-name">${escapeHtml(s.task_name)}</span><span class="sim-shift">${signed(s.finish_shift_days)}d</span></li>`,
      )
      .join("");
    parts.push(`<h3>Tasks moved (${moved.length})</h3><ul class="sim-moved">${rows}</ul>`);
  }

  if (simResult.warnings.length) {
    const rows = simResult.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("");
    parts.push(`<h3>Warnings</h3><ul class="dec-gaps">${rows}</ul>`);
  }

  const body = parts.length
    ? parts.join("")
    : `<p class="hint">No schedule change from this scenario.</p>`;
  return `<h3>Impact</h3>${body}`;
}

// Draw a faint "ghost" of each moved task's reference position (baseline plan, or
// the pre-what-if schedule) behind the current bar, with a connector showing the
// shift. Uses the same date->x calibration as the deadline/freeze overlays.
function drawGhostOverlay(referenceTasks, taskShifts) {
  try {
    const svg = document.querySelector("#gantt svg");
    if (!svg) return;
    const map = calibrateDateToX();
    if (!map) return;
    const ns = "http://www.w3.org/2000/svg";
    const baseById = new Map(referenceTasks.map((t) => [t.id, t]));
    for (const shift of taskShifts) {
      const base = baseById.get(shift.task_id);
      const barRect = document.querySelector(
        `.bar-wrapper[data-id="${cssEscape(shift.task_id)}"] .bar`,
      );
      if (!base || !barRect) continue;
      const y = Number(barRect.getAttribute("y"));
      const h = Number(barRect.getAttribute("height"));
      const x1 = map(base.start);
      const x2 = map(base.end);
      const gx = Math.min(x1, x2);
      const gw = Math.max(2, Math.abs(x2 - x1));

      const rect = document.createElementNS(ns, "rect");
      rect.setAttribute("x", gx);
      rect.setAttribute("y", y);
      rect.setAttribute("width", gw);
      rect.setAttribute("height", h);
      rect.setAttribute("rx", 3);
      rect.setAttribute("class", "ghost-bar");
      svg.appendChild(rect);

      const simX = Number(barRect.getAttribute("x"));
      if (Math.abs(simX - (gx + gw)) > 1) {
        const line = document.createElementNS(ns, "line");
        const cy = y + h / 2;
        line.setAttribute("x1", gx + gw);
        line.setAttribute("x2", simX);
        line.setAttribute("y1", cy);
        line.setAttribute("y2", cy);
        line.setAttribute("class", "ghost-connector");
        svg.appendChild(line);
      }
    }
  } catch (err) {
    console.warn("ghost overlay skipped:", err);
  }
}

// --- baseline / plan-vs-actual (RC1-192) -----------------------------------

function toggleBaselineMode() {
  if (baselineActive) exitBaselineMode();
  else enterBaselineMode();
}

async function enterBaselineMode() {
  if (simActive) exitSimMode();
  let data;
  try {
    const resp = await fetch(`${API_BASE}/api/baseline`);
    if (!resp.ok) throw new Error(`API ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    baselineActive = true;
    document.querySelector("#baseline-btn").classList.add("active");
    document.querySelector("#detail").innerHTML =
      `<button class="detail-close" title="Close">×</button><h2>Baseline</h2>` +
      `<p class="hint">Couldn't load the baseline: ${escapeHtml(err.message)}</p>`;
    document.querySelector("#detail .detail-close").addEventListener("click", exitBaselineMode);
    return;
  }

  baselineActive = true;
  document.querySelector("#baseline-btn").classList.add("active");

  if (!data.baseline) {
    // No baseline committed yet — prompt rather than error.
    baselineResult = null;
    const banner = document.querySelector("#sim-banner");
    banner.hidden = false;
    banner.className = "";
    document.querySelector("#sim-banner-text").textContent =
      "No baseline set yet — commit one to measure drift against.";
    renderBaselinePanel(null);
    return;
  }

  baselineResult = data;
  view = data.current.payload; // render current bars; ghost the baseline underneath
  index();
  renderGantt(currentViewMode);
  updateBaselineBanner();
  renderBaselinePanel(data);
}

function exitBaselineMode() {
  baselineActive = false;
  baselineResult = null;
  document.querySelector("#baseline-btn").classList.remove("active");
  document.querySelector("#sim-banner").hidden = true;
  view = payload;
  index();
  renderGantt(currentViewMode);
  clearDetail();
}

function updateBaselineBanner() {
  const banner = document.querySelector("#sim-banner");
  const text = document.querySelector("#sim-banner-text");
  const d = baselineResult.comparison.schedule_delta;
  const onTrack = baselineResult.comparison.is_on_track;
  const missed = d.deadline_flips.some((f) => f.met_before && !f.met_after);
  banner.hidden = false;
  banner.className = onTrack ? "ok" : missed ? "miss" : "slip";
  const v = baselineResult.baseline.version;
  text.innerHTML = `<strong>vs baseline v${v}</strong> — ${escapeHtml(d.headline)}`;
}

function renderBaselinePanel(data) {
  setPanelCollapsed(false);
  const el = document.querySelector("#detail");
  for (const sel of document.querySelectorAll(".selected")) sel.classList.remove("selected");

  if (!data) {
    el.innerHTML =
      `<button class="detail-close" aria-label="Close" title="Close">×</button>` +
      `<h2>Baseline</h2>` +
      `<p class="hint">No baseline has been committed yet. Set one from a committed plan with <code>plan baseline &lt;plan&gt; --by &lt;you&gt; --note "initial"</code>, then edits show up here as drift.</p>`;
    el.querySelector(".detail-close").addEventListener("click", exitBaselineMode);
    return;
  }

  const b = data.baseline;
  const d = data.comparison.schedule_delta;
  const structural = data.comparison.plan_diff;
  const parts = [];

  if (d.notes.length) {
    parts.push(
      `<ul class="sim-notes">${d.notes.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul>`,
    );
  }

  const moved = d.task_shifts.filter((s) => s.finish_shift_days !== 0);
  if (moved.length) {
    const rows = moved
      .slice()
      .sort((a, b2) => b2.finish_shift_days - a.finish_shift_days)
      .map(
        (s) =>
          `<li><span class="sim-moved-name">${escapeHtml(s.task_name)}</span><span class="sim-shift">${signed(s.finish_shift_days)}d</span></li>`,
      )
      .join("");
    parts.push(`<h3>Tasks drifted (${moved.length})</h3><ul class="sim-moved">${rows}</ul>`);
  }

  if (structural.length) {
    const symbol = { added: "+", removed: "−", modified: "~" };
    const rows = structural
      .map((e) => {
        const fields = e.fields
          .map((f) => `${escapeHtml(f.field)}: ${escapeHtml(String(f.before))} → ${escapeHtml(String(f.after))}`)
          .join("<br>");
        return `<li><div class="dec-head"><span class="dec-code">${symbol[e.change] ?? "~"} ${escapeHtml(e.kind)}</span> ${escapeHtml(byId.get(e.key)?.name ?? e.key)}</div>${fields ? `<p class="dec-reason">${fields}</p>` : ""}</li>`;
      })
      .join("");
    parts.push(`<h3>Structural changes (${structural.length})</h3><ul class="dec-list">${rows}</ul>`);
  }

  const body = parts.length
    ? parts.join("")
    : `<p class="hint">On track — no drift from the baseline.</p>`;

  el.innerHTML =
    `<button class="detail-close" aria-label="Close" title="Close">×</button>` +
    `<h2>Plan vs baseline</h2>` +
    `<p class="reasoning">Baseline <strong>v${b.version}</strong>${b.note ? ` — ${escapeHtml(b.note)}` : ""}${b.created_at ? ` · ${escapeHtml(b.created_at.slice(0, 10))}` : ""}. Current bars are shown over a ghost of the baseline.</p>` +
    body;
  el.querySelector(".detail-close").addEventListener("click", exitBaselineMode);
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
  // The decision-record panel: what the agents proposed vs. what Python did.
  const decBtn = document.querySelector("#decisions-btn");
  const n = decisionCount();
  if (n) decBtn.innerHTML = `Decisions <span class="count">${n}</span>`;
  decBtn.addEventListener("click", showDecisions);
  // The audit trail — "How this plan was made" (RC1-195).
  document.querySelector("#audit-btn").addEventListener("click", showAudit);
  // The RAID log (RC1-191).
  const raidBtn = document.querySelector("#raid-btn");
  const rn = raidCount();
  if (rn) raidBtn.innerHTML = `RAID <span class="count">${rn}</span>`;
  raidBtn.addEventListener("click", showRaid);
  // The slippage simulator (RC1-190).
  document.querySelector("#simulate-btn").addEventListener("click", toggleSimMode);
  // The baseline / plan-vs-actual view (RC1-192).
  document.querySelector("#baseline-btn").addEventListener("click", toggleBaselineMode);
  // The Jira generation preview (RC1-193).
  document.querySelector("#jira-btn").addEventListener("click", showJira);
  // The weekly status update (RC1-194).
  document.querySelector("#status-btn").addEventListener("click", showStatus);
  // The banner's Reset exits whichever overlay mode is active.
  document.querySelector("#sim-reset").addEventListener("click", exitOverlayMode);
  // Own the click handling via delegation rather than frappe's on_click (which
  // is unreliable across versions). #gantt persists across re-renders.
  document.querySelector("#gantt").addEventListener("click", (e) => {
    const wrapper = e.target.closest(".bar-wrapper");
    // In simulate mode the scenario panel owns the rail; don't hijack it.
    if (wrapper && !inOverlayMode()) showDetail(wrapper.getAttribute("data-id"));
  });
  // Keep the left task column in vertical lockstep with the chart.
  wrap.addEventListener("scroll", () => {
    document.querySelector("#task-column-inner").style.transform =
      `translateY(${-wrap.scrollTop}px)`;
  });
  // Escape exits an overlay mode, else closes the detail panel.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (inOverlayMode()) exitOverlayMode();
    else clearDetail();
  });
}

function exitOverlayMode() {
  if (baselineActive) exitBaselineMode();
  else exitSimMode();
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
