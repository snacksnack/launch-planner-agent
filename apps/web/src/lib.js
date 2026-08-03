// Pure helpers for the Gantt UI — no DOM, no module state, so they can be
// unit-tested headlessly (RC1-205). main.js imports these and passes its state
// (the byId/depById maps, the measured bar positions, etc.) in as arguments.

export function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}

export function signed(n) {
  return n > 0 ? `+${n}` : `${n}`;
}

// Days since the Unix epoch (UTC), so date arithmetic ignores timezones.
export function day(dateStr) {
  return Math.round(new Date(`${dateStr}T00:00:00Z`).getTime() / 86400000);
}

export function plural(n, word) {
  if (n === 1) return `${n} ${word}`;
  const p = /[^aeiou]y$/.test(word) ? `${word.slice(0, -1)}ies` : `${word}s`;
  return `${n} ${p}`;
}

// Risk severity (probability × impact, 1..25) → a colour band, or null if unscored.
export function severityBand(sev) {
  if (sev == null) return null;
  if (sev >= 15) return "high";
  if (sev >= 8) return "med";
  return "low";
}

// A calendar date as "Oct 23, 2026" (UTC, so it never drifts a day by timezone).
export function longDate(dateStr) {
  if (!dateStr) return "n/a";
  return new Date(`${dateStr}T00:00:00Z`).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

// Monte Carlo forecast geometry (RC1-201): fold the finish-date distribution and
// the percentile markers onto one shared date axis (fractions 0..1), so the
// histogram bars and the P10/P50/P80/P90/point markers line up. Pure — main.js
// renders the SVG from what this returns.
export function forecastBand(result) {
  const dist = result.distribution ?? [];
  if (!dist.length) return { bars: [], markers: [], first: null, last: null, span: 0 };

  const days = dist.map((d) => day(d.date));
  const first = Math.min(...days);
  const last = Math.max(...days);
  const span = last - first; // in days; 0 when every run finishes the same day
  const peak = Math.max(...dist.map((d) => d.count)) || 1;
  const xOf = (dateStr) => {
    if (!dateStr) return null;
    const frac = span === 0 ? 0.5 : (day(dateStr) - first) / span;
    return Math.min(1, Math.max(0, frac));
  };

  const bars = dist.map((d) => ({ x: xOf(d.date), h: d.count / peak, count: d.count }));
  const markers = [
    { key: "point", label: "Likely", date: result.deterministic_finish },
    { key: "p50", label: "P50", date: result.p50 },
    { key: "p80", label: "P80", date: result.p80 },
    { key: "p90", label: "P90", date: result.p90 },
  ]
    .filter((m) => m.date)
    .map((m) => ({ ...m, x: xOf(m.date) }));

  return {
    bars,
    markers,
    first: dist[0].date,
    last: dist[dist.length - 1].date,
    span,
  };
}

// Clamp the resizable detail-panel width (RC1-212) to a usable band: never
// narrower than `min`, never wider than the smaller of `maxPx` and `maxFrac` of
// the viewport (so the timeline always keeps room). Pure, so it's unit-testable.
export function clampPanelWidth(px, viewportW, { min = 260, maxPx = 680, maxFrac = 0.6 } = {}) {
  const max = Math.max(min, Math.min(maxPx, viewportW * maxFrac));
  return Math.round(Math.max(min, Math.min(px, max)));
}

// How a Jira preview issue's summary should behave when clicked (RC1-211):
// "open" a real ticket if it has been pushed (carries a jira_url), else "jump"
// to the underlying task if it has a bar on the timeline, else "none" (e.g. an
// epic that isn't on the chart and hasn't been pushed).
export function jiraLinkMode(op, canJump) {
  if (op && op.jira_url) return "open";
  if (canJump) return "jump";
  return "none";
}

// A saved scenario's launch impact as a short badge: "+24d", "-3d", or "no slip".
export function scenarioImpactLabel(impact) {
  if (!impact) return "";
  const d = impact.finish_shift_days;
  if (d > 0) return `+${d}d`;
  if (d < 0) return `${d}d`;
  return "no slip";
}

// A task's predecessor edges → Map(depId → {from, to}), so a flag on a dependency
// can be shown as its endpoint names instead of an opaque id.
export function buildDepIndex(tasks) {
  const deps = new Map();
  for (const t of tasks) {
    for (const p of t.predecessors ?? []) deps.set(p.id, { from: p.from, to: t.id });
  }
  return deps;
}

export function nameFor(id, byId) {
  return byId.get(id)?.name ?? id;
}

// A flag's entity id → a human, clickable subject: a task/milestone name, a
// dependency's "Predecessor → Successor" names, or the raw id as a fallback.
export function flagSubject(entityId, byId, depById) {
  if (!entityId) return "";
  if (byId.has(entityId)) {
    return `<a href="#" data-jump="${escapeHtml(entityId)}">${escapeHtml(nameFor(entityId, byId))}</a>`;
  }
  const edge = depById.get(entityId);
  if (edge) {
    return `<a href="#" data-jump="${escapeHtml(edge.to)}">${escapeHtml(nameFor(edge.from, byId))} → ${escapeHtml(nameFor(edge.to, byId))}</a>`;
  }
  return escapeHtml(entityId);
}

// A single what-if change → plain text. `nameOf` resolves an id to a display name.
export function describeChange(c, nameOf) {
  if (c.kind === "delay_task") return `${nameOf(c.task_id)} slips ${c.days}d`;
  const verb = c.kind === "add_dependency" ? "add" : "remove";
  return `${verb} ${nameOf(c.predecessor_id)} → ${nameOf(c.successor_id)}`;
}

export function describeScenario(changes, nameOf) {
  return changes.map((c) => describeChange(c, nameOf)).join("; ");
}

// Fit x = a·day + b from measured bars [{date, x}] and return a date→x function
// (null if fewer than two distinct dates are available to solve the line).
export function calibrate(bars) {
  const pts = bars.filter(Boolean);
  if (pts.length < 2) return null;
  pts.sort((p, q) => day(p.date) - day(q.date));
  const lo = pts[0];
  const hi = pts[pts.length - 1];
  const span = day(hi.date) - day(lo.date);
  if (span === 0) return null;
  const a = (hi.x - lo.x) / span;
  const b = lo.x - a * day(lo.date);
  return (dateStr) => a * day(dateStr) + b;
}

// Geometry for one ghost bar: given the date→x `map`, the baseline task's
// {start, end}, and the simulated bar's y/height/x, return the ghost rect and an
// optional connector line to the shifted bar (omitted when they barely differ).
export function ghostRect(map, base, y, h, simX) {
  const x1 = map(base.start);
  const x2 = map(base.end);
  const gx = Math.min(x1, x2);
  const gw = Math.max(2, Math.abs(x2 - x1));
  const rect = { x: gx, y, width: gw, height: h };
  const connector =
    Math.abs(simX - (gx + gw)) > 1 ? { x1: gx + gw, x2: simX, cy: y + h / 2 } : null;
  return { rect, connector };
}

// The gated CLI command to apply the selected Jira issues (partial → `--only`).
export function jiraCommand(gen, selectedIds, startDate) {
  const partial = selectedIds.length < gen.issues.length;
  const only = partial ? ` --only ${selectedIds.join(",")}` : "";
  return `plan jira <plan.json> --start-date ${startDate} --project ${gen.project_key}${only} --real --confirm`;
}
