"""planner_core — the deterministic heart of the launch planner.

Task graph, dependency model, CPM/critical-path scheduling, validation, and
plan-store models live here. This package has **zero LLM dependencies** by
design: the scheduling math must be inspectable and testable without any model
in the loop. The `agents` package depends on this one, never the reverse — a
rule enforced in CI by import-linter.
"""

from planner_core.baseline import (
    BaselineComparison,
    compare_versions,
)
from planner_core.decision_record import (
    CycleBreakEntry,
    DecisionRecord,
    FlaggedIssue,
    RejectedEdge,
    build_decision_record,
)
from planner_core.dependencies import (
    CycleBreak,
    DependencyReport,
    EdgeRejection,
    build_dependency_report,
    check_gate_coverage,
    filter_dependencies,
    find_cycles,
    orphan_tasks,
    resolve_cycles,
)
from planner_core.diff import (
    EntityDiff,
    FieldChange,
    PlanDiff,
    diff_plans,
)
from planner_core.models import (
    Constraint,
    ConstraintType,
    Dependency,
    DependencyType,
    Epic,
    Milestone,
    Plan,
    Task,
    TeamMember,
    ThreePointEstimate,
    WorkBreakdown,
    plan_json_schema,
)
from planner_core.plan_store import (
    CommitRejected,
    InMemoryPlanRepository,
    PlanRepository,
    Snapshot,
    SnapshotKind,
    blocking_errors,
    commit_baseline,
    commit_plan,
    content_hash,
    record_proposal,
)
from planner_core.provenance import (
    Confidence,
    Provenance,
    ProvenancedModel,
)
from planner_core.raid import (
    Evidence,
    PrdEvidence,
    RaidItem,
    RaidProvenance,
    RaidType,
    ScheduleEvidence,
)
from planner_core.raid_analysis import (
    RaidReport,
    ScheduleFact,
    analyze_schedule_risks,
    build_raid_report,
    format_schedule_facts,
)
from planner_core.scheduling import (
    CPMResult,
    DeadlineCheck,
    MilestoneSchedule,
    NodeMetrics,
    Schedule,
    TaskSchedule,
    WorkingCalendar,
    compute_cpm,
    critical_paths,
    schedule_plan,
)
from planner_core.simulation import (
    AddDependency,
    DeadlineFlip,
    DelayTask,
    MilestoneShift,
    RemoveDependency,
    Scenario,
    ScenarioChange,
    ScheduleDelta,
    SetEstimate,
    SimulationResult,
    TaskShift,
    apply_scenario,
    diff_schedules,
    simulate,
)
from planner_core.validation import (
    BreakdownReport,
    Severity,
    ValidationIssue,
    build_report,
    coverage_gaps,
    markdown_sections,
    normalize_whitespace,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # provenance
    "Confidence",
    "Provenance",
    "ProvenancedModel",
    # RAID (risks / assumptions / issues / decisions)
    "Evidence",
    "PrdEvidence",
    "RaidItem",
    "RaidProvenance",
    "RaidType",
    "ScheduleEvidence",
    "RaidReport",
    "ScheduleFact",
    "analyze_schedule_risks",
    "build_raid_report",
    "format_schedule_facts",
    # domain models
    "Constraint",
    "ConstraintType",
    "Dependency",
    "DependencyType",
    "Epic",
    "Milestone",
    "Plan",
    "Task",
    "TeamMember",
    "ThreePointEstimate",
    "WorkBreakdown",
    "plan_json_schema",
    # validation
    "BreakdownReport",
    "Severity",
    "ValidationIssue",
    "build_report",
    "coverage_gaps",
    "markdown_sections",
    "normalize_whitespace",
    # dependency validation
    "CycleBreak",
    "DependencyReport",
    "EdgeRejection",
    "build_dependency_report",
    # decision record (durable build-time audit)
    "CycleBreakEntry",
    "DecisionRecord",
    "FlaggedIssue",
    "RejectedEdge",
    "build_decision_record",
    "check_gate_coverage",
    "filter_dependencies",
    "find_cycles",
    "orphan_tasks",
    "resolve_cycles",
    # scheduling (CPM)
    "CPMResult",
    "DeadlineCheck",
    "MilestoneSchedule",
    "NodeMetrics",
    "Schedule",
    "TaskSchedule",
    "WorkingCalendar",
    "compute_cpm",
    "critical_paths",
    "schedule_plan",
    # slippage simulator (what-if analysis)
    "AddDependency",
    "DelayTask",
    "DeadlineFlip",
    "MilestoneShift",
    "RemoveDependency",
    "Scenario",
    "ScenarioChange",
    "ScheduleDelta",
    "SetEstimate",
    "SimulationResult",
    "TaskShift",
    "apply_scenario",
    "diff_schedules",
    "simulate",
    # plan diff (human-vs-agent audit trail)
    "EntityDiff",
    "FieldChange",
    "PlanDiff",
    "diff_plans",
    # plan store (immutable commit / audit log)
    "CommitRejected",
    "InMemoryPlanRepository",
    "PlanRepository",
    "Snapshot",
    "SnapshotKind",
    "blocking_errors",
    "commit_plan",
    "commit_baseline",
    "content_hash",
    "record_proposal",
    # baselines & plan-vs-actual (RC1-192)
    "BaselineComparison",
    "compare_versions",
]

