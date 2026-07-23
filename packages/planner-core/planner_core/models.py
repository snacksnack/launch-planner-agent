"""The typed plan domain model shared by every agent and the scheduling engine.

This is the contract at the centre of the system. Agents schema-force their
output against these models (`plan_json_schema()` publishes the JSON Schema);
the deterministic engine reads them to compute CPM, float, and the critical
path; the review UI renders them. Every agent-produced entity
(`Epic`, `Task`, `Dependency`, `Milestone`, `Constraint`) carries mandatory
provenance via `ProvenancedModel`. `TeamMember` is human roster input and is
the one entity without a provenance block.

Entities reference each other by string id (task -> owner, dependency ->
tasks, constraint -> targets) rather than by object nesting, so the plan
serialises to a flat, diff-friendly JSON document and the graph can be rebuilt
deterministically by `planner-core` in a later ticket.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from planner_core.provenance import ProvenancedModel


class DependencyType(StrEnum):
    """Precedence relationship between two tasks (PDM / classic Gantt semantics)."""

    FINISH_TO_START = "finish_to_start"
    START_TO_START = "start_to_start"
    FINISH_TO_FINISH = "finish_to_finish"
    START_TO_FINISH = "start_to_finish"


class ConstraintType(StrEnum):
    """Kind of scheduling constraint imposed on the plan."""

    HARD_DATE = "hard_date"  # a task/milestone must land on/by a fixed calendar date
    GATE = "gate"  # a qualitative gate that must clear (e.g. "SRE review before prod")


class ThreePointEstimate(BaseModel):
    """PERT three-point estimate, in working days.

    Kept as a value object (not just three loose fields on `Task`) so the PERT
    expected duration and standard deviation live next to the numbers they
    derive from — the scheduling engine consumes `expected` directly.
    """

    model_config = ConfigDict(extra="forbid")

    optimistic: float = Field(..., ge=0, description="Best-case duration in working days.")
    likely: float = Field(..., ge=0, description="Most-likely duration in working days.")
    pessimistic: float = Field(..., ge=0, description="Worst-case duration in working days.")

    @model_validator(mode="after")
    def _ordered(self) -> ThreePointEstimate:
        if not (self.optimistic <= self.likely <= self.pessimistic):
            raise ValueError(
                "three-point estimate must satisfy optimistic <= likely <= pessimistic "
                f"(got {self.optimistic}, {self.likely}, {self.pessimistic})"
            )
        return self

    @property
    def expected(self) -> float:
        """PERT expected duration: (o + 4m + p) / 6."""
        return (self.optimistic + 4 * self.likely + self.pessimistic) / 6

    @property
    def std_dev(self) -> float:
        """PERT standard deviation: (p - o) / 6."""
        return (self.pessimistic - self.optimistic) / 6


class TeamMember(BaseModel):
    """A person who can own work. Human roster input — no provenance block."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="Stable identifier referenced by Task.owner.")
    name: str = Field(..., min_length=1)
    role: str | None = Field(None, description="Role/title, e.g. 'Backend Engineer'.")
    email: str | None = None


class Epic(ProvenancedModel):
    """A large body of work grouping related tasks. Agent-produced."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    description: str | None = None


class Task(ProvenancedModel):
    """A unit of schedulable work with an owner and a three-point estimate."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    description: str | None = None
    epic_id: str | None = Field(None, description="Id of the owning Epic, if any.")
    owner_id: str | None = Field(None, description="Id of the TeamMember who owns this task.")
    estimate: ThreePointEstimate


class Dependency(ProvenancedModel):
    """A precedence edge between two tasks. Agent-produced."""

    id: str = Field(..., min_length=1)
    predecessor_id: str = Field(..., min_length=1, description="Id of the upstream task.")
    successor_id: str = Field(..., min_length=1, description="Id of the downstream task.")
    type: DependencyType = DependencyType.FINISH_TO_START
    lag: float = Field(
        0.0,
        description="Lag in working days added to the relationship; may be negative (lead).",
    )

    @model_validator(mode="after")
    def _no_self_dependency(self) -> Dependency:
        if self.predecessor_id == self.successor_id:
            raise ValueError(f"task {self.predecessor_id!r} cannot depend on itself")
        return self


class Milestone(ProvenancedModel):
    """A zero-duration checkpoint, optionally pinned to a target date. Agent-produced."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    target_date: date | None = Field(None, description="Target calendar date, if fixed.")


class Constraint(ProvenancedModel):
    """A scheduling constraint: a hard date or a qualitative gate. Agent-produced."""

    id: str = Field(..., min_length=1)
    type: ConstraintType
    description: str = Field(..., min_length=1)
    hard_date: date | None = Field(
        None, description="Required for HARD_DATE constraints; the fixed calendar date."
    )
    gate: str | None = Field(
        None, description="Required for GATE constraints; what must clear (e.g. 'SRE review')."
    )
    applies_to: list[str] = Field(
        default_factory=list,
        description="Ids of tasks/milestones this constraint binds.",
    )

    @model_validator(mode="after")
    def _payload_matches_type(self) -> Constraint:
        if self.type is ConstraintType.HARD_DATE and self.hard_date is None:
            raise ValueError("a HARD_DATE constraint requires 'hard_date'")
        if self.type is ConstraintType.GATE and not self.gate:
            raise ValueError("a GATE constraint requires 'gate'")
        return self


class Plan(BaseModel):
    """The plan-of-record: the full typed document agents build and the engine schedules.

    Collections are flat lists of entities that reference one another by id.
    Because each agent-produced entity type requires provenance at construction
    time, a `Plan` simply cannot hold an agent-generated entity that lacks it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    description: str | None = None
    created_at: datetime | None = Field(
        None, description="When this plan document was assembled."
    )
    source_document: str | None = Field(
        None, description="Identifier of the source PRD/fixture this plan was derived from."
    )

    team: list[TeamMember] = Field(default_factory=list)
    epics: list[Epic] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    milestones: list[Milestone] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)


def plan_json_schema() -> dict[str, Any]:
    """Publish the `Plan` JSON Schema so agents can be schema-forced against it.

    This is the single source of truth agents target when producing structured
    output; keeping it a function (rather than a cached constant) means it always
    reflects the current model definitions.
    """
    return Plan.model_json_schema()
