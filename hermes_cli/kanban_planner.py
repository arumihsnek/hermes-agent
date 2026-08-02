"""Pure, offline K9 planner envelope validation and scheduler projection.

This module deliberately has no provider, filesystem, database, or lifecycle
side effects. Callers can validate and stage a complete planner envelope
before committing it to a durable store.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping


class PlannerValidationError(ValueError):
    """Raised when a planner envelope cannot be imported atomically."""


_REQUIRED_FIELDS = {
    "schema_version",
    "subtasks",
    "dependencies",
    "roles",
    "acceptance",
    "review_policy",
    "batch_groups",
    "evidence_expectations",
    "estimated_model_tier",
    "risk_classification",
    "closure_policy",
}
_ROLE_NAMES = {"worker", "reviewer", "closer", "kanban-coordinator"}
_MODEL_TIERS = {"cheap", "standard", "strong", "human"}
_RISK_CLASSES = {"low", "medium", "high", "critical"}


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlannerValidationError(f"{name} must be an object")
    return value


def _validate_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(envelope, Mapping):
        raise PlannerValidationError("planner envelope must be an object")
    missing = _REQUIRED_FIELDS - set(envelope)
    if missing:
        raise PlannerValidationError(f"missing planner fields: {', '.join(sorted(missing))}")
    if envelope["schema_version"] != "planner-dag/v1":
        raise PlannerValidationError("schema_version must be planner-dag/v1")

    subtasks = envelope["subtasks"]
    if not isinstance(subtasks, list) or not subtasks:
        raise PlannerValidationError("subtasks must be a non-empty array")
    task_ids: list[str] = []
    task_by_id: dict[str, Mapping[str, Any]] = {}
    for task in subtasks:
        task_map = _require_mapping(task, "subtask")
        task_id = task_map.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise PlannerValidationError("subtask id must be a non-empty string")
        if task_id in task_by_id:
            raise PlannerValidationError(f"duplicate subtask id: {task_id}")
        if not isinstance(task_map.get("title"), str) or not task_map["title"]:
            raise PlannerValidationError(f"subtask title missing: {task_id}")
        task_ids.append(task_id)
        task_by_id[task_id] = task_map

    roles = _require_mapping(envelope["roles"], "roles")
    for role_name, role_config in roles.items():
        if role_name not in _ROLE_NAMES:
            raise PlannerValidationError(f"unknown role: {role_name}")
        tier = _require_mapping(role_config, f"roles.{role_name}").get("tier")
        if tier not in _MODEL_TIERS:
            raise PlannerValidationError(f"invalid role tier: {role_name}")
    for task_id, task in task_by_id.items():
        role = task.get("role")
        if role not in roles:
            raise PlannerValidationError(f"unknown role: {role}")

    dependencies = envelope["dependencies"]
    if not isinstance(dependencies, list):
        raise PlannerValidationError("dependencies must be an array")
    graph = {task_id: [] for task_id in task_ids}
    for dependency in dependencies:
        dep = _require_mapping(dependency, "dependency")
        child = dep.get("subtask_id")
        parent = dep.get("depends_on")
        if child not in graph or parent not in graph:
            raise PlannerValidationError("unknown subtask in dependency")
        if child == parent or parent in graph[child]:
            raise PlannerValidationError("duplicate dependency")
        graph[child].append(parent)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise PlannerValidationError("dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for parent in graph[task_id]:
            visit(parent)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in task_ids:
        visit(task_id)

    for field_name in ("acceptance", "review_policy", "evidence_expectations", "estimated_model_tier", "risk_classification"):
        mapping = _require_mapping(envelope[field_name], field_name)
        missing_ids = set(task_ids) - set(mapping)
        if missing_ids:
            raise PlannerValidationError(f"{field_name} missing subtask: {sorted(missing_ids)[0]}")
    for task_id, tier in envelope["estimated_model_tier"].items():
        if tier not in _MODEL_TIERS:
            raise PlannerValidationError(f"invalid estimated model tier: {task_id}")
    for task_id, risk in envelope["risk_classification"].items():
        if risk not in _RISK_CLASSES:
            raise PlannerValidationError(f"invalid risk classification: {task_id}")

    closure_policy = _require_mapping(envelope["closure_policy"], "closure_policy")
    if not isinstance(closure_policy.get("allow_partial"), bool):
        raise PlannerValidationError("closure_policy allow_partial must be boolean")
    required_reviews = _require_mapping(
        closure_policy.get("required_review_roles"), "closure_policy.required_review_roles"
    )
    for task_id, review_roles in required_reviews.items():
        if task_id not in task_by_id:
            raise PlannerValidationError("unknown subtask in closure policy")
        if not isinstance(review_roles, list) or any(role not in roles for role in review_roles):
            raise PlannerValidationError("closure_policy contains unknown review role")

    if not isinstance(envelope["batch_groups"], list):
        raise PlannerValidationError("batch_groups must be an array")
    return deepcopy(dict(envelope))


def scheduler_dry_run(
    envelope: Mapping[str, Any],
    *,
    completed_subtasks: set[str] | None = None,
) -> dict[str, Any]:
    """Return a stable scheduler projection without mutation or external calls.

    ``completed_subtasks`` is an explicit caller-supplied runtime snapshot. It
    is never inferred from a database or provider and is validated against the
    envelope before it affects the runnable frontier.
    """

    canonical = _validate_envelope(envelope)
    tasks = canonical["subtasks"]
    task_by_id = {task["id"]: task for task in tasks}
    completed = set(completed_subtasks or ())
    unknown_completed = completed - set(task_by_id)
    if unknown_completed:
        raise PlannerValidationError(
            f"unknown completed subtask: {sorted(unknown_completed)[0]}"
        )
    dependencies = {task_id: [] for task_id in task_by_id}
    for dependency in canonical["dependencies"]:
        dependencies[dependency["subtask_id"]].append(dependency["depends_on"])
    ready = [
        task_id
        for task_id in task_by_id
        if task_id not in completed
        and all(parent in completed for parent in dependencies[task_id])
    ]
    not_yet_runnable = [
        task_id
        for task_id in task_by_id
        if task_id not in completed and task_id not in ready
    ]
    result = {
        "completed": [task_id for task_id in task_by_id if task_id in completed],
        "ready_workers": [task_id for task_id in ready if task_by_id[task_id]["role"] == "worker"],
        "ready_reviewers": [task_id for task_id in ready if task_by_id[task_id]["role"] == "reviewer"],
        "ready_closers": [task_id for task_id in ready if task_by_id[task_id]["role"] == "closer"],
        "blocked": [],
        "not_yet_runnable": not_yet_runnable,
        "batch_candidates": deepcopy(canonical["batch_groups"]),
        "policy_violations": [],
        "provider_calls": 0,
        "protected_value_reads": 0,
        "lifecycle_mutations": 0,
    }
    return result


class PlannerLedger:
    """Small in-memory transaction boundary used by the offline K9 slice."""

    def __init__(self) -> None:
        self._envelope: dict[str, Any] | None = None

    def import_envelope(self, envelope: Mapping[str, Any]) -> None:
        staged = _validate_envelope(envelope)
        self._envelope = staged

    def snapshot(self) -> dict[str, Any] | None:
        return deepcopy(self._envelope)


@dataclass
class PlannerMetrics:
    _calls: list[dict[str, Any]] = field(default_factory=list)

    def record_call(
        self,
        *,
        provider: str,
        model: str,
        tier: str,
        tokens: int | None,
        latency_ms: int | None,
        fallback: str | None,
        cache_hit: bool,
    ) -> None:
        self._calls.append(
            {
                "provider": provider,
                "model": model,
                "tier": tier,
                "tokens": tokens,
                "latency_ms": latency_ms,
                "fallback": fallback,
                "cache_hit": bool(cache_hit),
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {"calls": deepcopy(self._calls), "absolute_cost": None}
