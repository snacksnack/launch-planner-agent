import { describe, expect, it } from "vitest";

import {
  buildDepIndex,
  calibrate,
  describeChange,
  describeScenario,
  escapeHtml,
  flagSubject,
  ghostRect,
  jiraCommand,
  nameFor,
  plural,
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
