import { describe, expect, it } from "vitest";

import {
  buildDepIndex,
  calibrate,
  describeChange,
  describeScenario,
  escapeHtml,
  flagSubject,
  forecastBand,
  ghostRect,
  jiraCommand,
  jiraLinkMode,
  longDate,
  nameFor,
  plural,
  scenarioImpactLabel,
  severityBand,
  signed,
} from "../src/lib.js";
import plan from "./fixtures/plan.json";
import sim from "./fixtures/simulate.json";

// byId / depById the way main.js builds them, but from the captured fixture.
const byId = new Map([
  ...plan.tasks.map((t) => [t.id, { ...t, kind: "task" }]),
  ...plan.milestones.map((m) => [m.id, { ...m, kind: "milestone" }]),
]);
const depById = buildDepIndex(plan.tasks);
const nameOf = (id) => nameFor(id, byId);

describe("primitive formatters", () => {
  it("escapes HTML metacharacters", () => {
    expect(escapeHtml('<a href="x">A & B</a>')).toBe(
      "&lt;a href=&quot;x&quot;&gt;A &amp; B&lt;/a&gt;",
    );
  });

  it("signs numbers", () => {
    expect(signed(4)).toBe("+4");
    expect(signed(-3)).toBe("-3");
    expect(signed(0)).toBe("0");
  });

  it("pluralizes, handling consonant-y", () => {
    expect(plural(1, "task")).toBe("1 task");
    expect(plural(3, "task")).toBe("3 tasks");
    expect(plural(32, "dependency")).toBe("32 dependencies");
    expect(plural(2, "day")).toBe("2 days"); // vowel-y stays +s
  });

  it("bands risk severity", () => {
    expect(severityBand(null)).toBe(null);
    expect(severityBand(4)).toBe("low");
    expect(severityBand(12)).toBe("med");
    expect(severityBand(20)).toBe("high");
  });
});

describe("dependency-flag naming", () => {
  it("indexes dependency edges by id from a plan's predecessors", () => {
    expect(depById.get("dep-ab")).toEqual({ from: "task-a", to: "task-b" });
    expect(depById.get("dep-bc")).toEqual({ from: "task-b", to: "task-c" });
  });

  it("renders a task flag as a jump link to its name", () => {
    expect(flagSubject("task-a", byId, depById)).toBe(
      '<a href="#" data-jump="task-a">Inventory projects</a>',
    );
  });

  it("renders a dependency flag as 'Predecessor → Successor', linking the successor", () => {
    expect(flagSubject("dep-ab", byId, depById)).toBe(
      '<a href="#" data-jump="task-b">Inventory projects → Migrate users</a>',
    );
  });

  it("falls back to the raw id when nothing resolves", () => {
    expect(flagSubject("mystery", byId, depById)).toBe("mystery");
    expect(flagSubject("", byId, depById)).toBe("");
  });
});

describe("scenario / impact formatting", () => {
  it("describes each change type in plain language", () => {
    expect(describeChange({ kind: "delay_task", task_id: "task-a", days: 4 }, nameOf)).toBe(
      "Inventory projects slips 4d",
    );
    expect(
      describeChange({ kind: "add_dependency", predecessor_id: "task-a", successor_id: "task-c" }, nameOf),
    ).toBe("add Inventory projects → Cutover");
    expect(
      describeChange({ kind: "remove_dependency", predecessor_id: "task-a", successor_id: "task-b" }, nameOf),
    ).toBe("remove Inventory projects → Migrate users");
  });

  it("joins a multi-change scenario", () => {
    const changes = [
      { kind: "delay_task", task_id: "task-a", days: 4 },
      { kind: "remove_dependency", predecessor_id: "task-b", successor_id: "task-c" },
    ];
    expect(describeScenario(changes, nameOf)).toBe(
      "Inventory projects slips 4d; remove Migrate users → Cutover",
    );
  });

  it("the captured simulate delta drives a headline shift of +4", () => {
    expect(sim.delta.finish_shift_days).toBe(4);
    // the slipped task appears in task_shifts with a positive finish shift
    const moved = sim.delta.task_shifts.find((s) => s.task_id === "task-a");
    expect(moved.finish_shift_days).toBe(4);
  });
});

describe("ghost-overlay geometry", () => {
  it("calibrates a date→x line from two measured bars", () => {
    const map = calibrate([
      { date: "2026-08-03", x: 100 },
      { date: "2026-08-13", x: 200 }, // +10 days → +100px = 10px/day
    ]);
    expect(map("2026-08-03")).toBeCloseTo(100);
    expect(map("2026-08-08")).toBeCloseTo(150); // 5 days in
    expect(map("2026-08-13")).toBeCloseTo(200);
  });

  it("returns null when it can't solve the line", () => {
    expect(calibrate([{ date: "2026-08-03", x: 100 }])).toBe(null); // one point
    expect(
      calibrate([
        { date: "2026-08-03", x: 100 },
        { date: "2026-08-03", x: 100 }, // zero span
      ]),
    ).toBe(null);
  });

  it("computes a ghost rect and a connector to the shifted bar", () => {
    const map = (d) => ({ "2026-08-03": 100, "2026-08-07": 140 })[d];
    const { rect, connector } = ghostRect(map, { start: "2026-08-03", end: "2026-08-07" }, 30, 18, 220);
    expect(rect).toEqual({ x: 100, y: 30, width: 40, height: 18 });
    expect(connector).toEqual({ x1: 140, x2: 220, cy: 39 }); // right edge → sim bar, mid-height
  });

  it("omits the connector when the bar barely moved", () => {
    const map = (d) => ({ "2026-08-03": 100, "2026-08-07": 140 })[d];
    const { connector } = ghostRect(map, { start: "2026-08-03", end: "2026-08-07" }, 0, 18, 140.5);
    expect(connector).toBe(null); // < 1px difference
  });

  it("enforces a minimum ghost width", () => {
    const map = () => 100; // zero-width span
    expect(ghostRect(map, { start: "x", end: "x" }, 0, 10, 100).rect.width).toBe(2);
  });
});

describe("monte carlo forecast geometry", () => {
  it("formats a long date in UTC", () => {
    expect(longDate("2026-10-23")).toBe("Oct 23, 2026");
    expect(longDate(null)).toBe("n/a");
  });

  const result = {
    deterministic_finish: "2026-10-12",
    p10: "2026-10-09",
    p50: "2026-10-16",
    p80: "2026-10-23",
    p90: "2026-10-27",
    distribution: [
      { date: "2026-10-09", count: 5 },
      { date: "2026-10-16", count: 20 }, // the peak
      { date: "2026-10-27", count: 2 },
    ],
  };

  it("places bars on a shared date axis with height relative to the peak", () => {
    const { bars, span } = forecastBand(result);
    expect(span).toBe(18); // Oct 9 → Oct 27
    expect(bars[0]).toMatchObject({ x: 0, h: 0.25 }); // 5/20
    expect(bars[1]).toMatchObject({ h: 1 }); // the peak
    expect(bars[1].x).toBeCloseTo(7 / 18); // Oct 16 is 7 days in
    expect(bars[2]).toMatchObject({ x: 1, h: 0.1 });
  });

  it("places percentile + point markers on the same axis, skipping missing ones", () => {
    const { markers } = forecastBand(result);
    const byKey = Object.fromEntries(markers.map((m) => [m.key, m]));
    expect(byKey.point.x).toBeCloseTo(3 / 18); // likely finish, Oct 12
    expect(byKey.p80.x).toBeCloseTo(14 / 18); // Oct 23
    expect(byKey.p90.x).toBe(1);
    expect(markers.map((m) => m.key)).toEqual(["point", "p50", "p80", "p90"]);
  });

  it("degrades gracefully on an empty distribution", () => {
    expect(forecastBand({ distribution: [] })).toEqual({
      bars: [],
      markers: [],
      first: null,
      last: null,
      span: 0,
    });
  });

  it("centers a single-bucket distribution instead of dividing by zero", () => {
    const { bars, span } = forecastBand({
      deterministic_finish: "2026-10-12",
      distribution: [{ date: "2026-10-12", count: 9 }],
    });
    expect(span).toBe(0);
    expect(bars[0]).toMatchObject({ x: 0.5, h: 1 });
  });
});

describe("jira issue link mode", () => {
  it("opens the real ticket when the op carries a jira_url", () => {
    expect(jiraLinkMode({ jira_url: "https://x/browse/PMA-1" }, true)).toBe("open");
    expect(jiraLinkMode({ jira_url: "https://x/browse/PMA-1" }, false)).toBe("open");
  });

  it("jumps to the task when there's no ticket but it has a bar", () => {
    expect(jiraLinkMode({ jira_url: null, local_id: "task-a" }, true)).toBe("jump");
  });

  it("is inert when there's no ticket and no bar (e.g. an unpushed epic)", () => {
    expect(jiraLinkMode({ jira_url: null }, false)).toBe("none");
    expect(jiraLinkMode(null, false)).toBe("none");
  });
});

describe("saved scenario impact badge", () => {
  it("signs a slip, a pull-in, and no change", () => {
    expect(scenarioImpactLabel({ finish_shift_days: 24 })).toBe("+24d");
    expect(scenarioImpactLabel({ finish_shift_days: -3 })).toBe("-3d");
    expect(scenarioImpactLabel({ finish_shift_days: 0 })).toBe("no slip");
  });

  it("is empty when there's no impact payload", () => {
    expect(scenarioImpactLabel(null)).toBe("");
    expect(scenarioImpactLabel(undefined)).toBe("");
  });
});

describe("jira command builder", () => {
  const gen = { project_key: "PMA", issues: [{ local_id: "a" }, { local_id: "b" }, { local_id: "c" }] };

  it("omits --only when everything is selected", () => {
    expect(jiraCommand(gen, ["a", "b", "c"], "2026-08-03")).toBe(
      "plan jira <plan.json> --start-date 2026-08-03 --project PMA --real --confirm",
    );
  });

  it("adds --only for a partial selection", () => {
    expect(jiraCommand(gen, ["a", "c"], "2026-08-03")).toBe(
      "plan jira <plan.json> --start-date 2026-08-03 --project PMA --only a,c --real --confirm",
    );
  });
});
