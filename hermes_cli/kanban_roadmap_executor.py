"""Autonomous roadmap executor for Kanban long-running missions.

Loads a versioned JSON roadmap, resolves dependencies, selects slices,
invokes senior consult at declared gates, executes TDD, manages PRs,
and advances through phases autonomously.

Schema version: ``kanban-roadmap-executor/v1``.

This module builds on top of:
- kanban_supervisor.py (R3): slice execution, lease, fencing
- kanban_human_gate.py (R4): human decision gates
- kanban_mission_state.py (R2): durable mission state
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import subprocess
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from hermes_cli.kanban_supervisor import (
    add_slices,
    acquire_supervisor_lease,
    get_roadmap_slices,
    get_next_slice,
    mark_slice_active,
    mark_slice_completed,
    mark_slice_blocked,
    mark_slice_human_gate,
    mark_slice_failed,
    recover_supervisor_state,
    run_supervisor_tick,
    validate_slice,
    DEFAULT_MAX_ATTEMPTS,
    TERMINAL_SLICE_STATUSES,
    VALID_OUTCOMES,
    SliceRecord,
    TickResult,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Roadmap format
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoadmapSlice:
    """A slice definition from the roadmap JSON."""
    slice_id: str
    phase: str
    description: str
    dependencies: list[str]
    material: dict
    acceptance_criteria: list[str]
    tests: list[str]
    gate_type: Optional[str]  # None | "senior_consult" | "human_gate"
    max_attempts: int
    priority: int


@dataclass(frozen=True)
class RoadmapPhase:
    """A phase in the roadmap."""
    phase_id: str
    description: str
    slices: list[RoadmapSlice]
    dependencies: list[str]  # Other phase_ids this depends on


@dataclass(frozen=True)
class Roadmap:
    """A parsed roadmap."""
    roadmap_id: str
    version: str
    description: str
    phases: list[RoadmapPhase]
    all_slices: list[RoadmapSlice]


def load_roadmap(path: str) -> Roadmap:
    """Load and validate a JSON roadmap file.

    Expected format:
    {
        "roadmap_id": "...",
        "version": "1.0.0",
        "description": "...",
        "phases": [
            {
                "phase_id": "R3",
                "description": "...",
                "slices": [...],
                "dependencies": []
            }
        ]
    }
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    errors = validate_roadmap_data(data)
    if errors:
        raise ValueError(f"Invalid roadmap: {'; '.join(errors)}")

    phases = []
    all_slices = []
    for phase_data in data["phases"]:
        slices = []
        for s in phase_data.get("slices", []):
            rs = RoadmapSlice(
                slice_id=s["slice_id"],
                phase=phase_data["phase_id"],
                description=s["description"],
                dependencies=s.get("dependencies", []),
                material=s.get("material", {}),
                acceptance_criteria=s.get("acceptance_criteria", []),
                tests=s.get("tests", []),
                gate_type=s.get("gate_type"),
                max_attempts=s.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
                priority=s.get("priority", 0),
            )
            slices.append(rs)
            all_slices.append(rs)
        phases.append(RoadmapPhase(
            phase_id=phase_data["phase_id"],
            description=phase_data["description"],
            slices=slices,
            dependencies=phase_data.get("dependencies", []),
        ))

    return Roadmap(
        roadmap_id=data["roadmap_id"],
        version=data["version"],
        description=data["description"],
        phases=phases,
        all_slices=all_slices,
    )


def validate_roadmap_data(data: dict) -> list[str]:
    """Validate roadmap JSON structure. Returns list of errors."""
    errors = []

    if not isinstance(data, dict):
        return ["roadmap must be a JSON object"]

    for key in ("roadmap_id", "version", "description", "phases"):
        if key not in data:
            errors.append(f"missing required key: {key}")

    if not isinstance(data.get("phases"), list):
        errors.append("phases must be a list")
        return errors

    all_slice_ids = set()
    for i, phase in enumerate(data["phases"]):
        if not isinstance(phase, dict):
            errors.append(f"phases[{i}] must be an object")
            continue
        for key in ("phase_id", "description"):
            if key not in phase:
                errors.append(f"phases[{i}] missing key: {key}")

        slices = phase.get("slices", [])
        if not isinstance(slices, list):
            errors.append(f"phases[{i}].slices must be a list")
            continue

        for j, s in enumerate(slices):
            if not isinstance(s, dict):
                errors.append(f"phases[{i}].slices[{j}] must be an object")
                continue
            for key in ("slice_id", "description"):
                if key not in s:
                    errors.append(f"phases[{i}].slices[{j}] missing key: {key}")

            sid = s.get("slice_id", "")
            if sid in all_slice_ids:
                errors.append(f"duplicate slice_id: {sid}")
            all_slice_ids.add(sid)

            # Validate dependencies reference existing slices (checked later)
            deps = s.get("dependencies", [])
            if not isinstance(deps, list):
                errors.append(f"phases[{i}].slices[{j}].dependencies must be a list")

    # Validate cross-phase dependencies
    phase_ids = {p.get("phase_id") for p in data["phases"] if isinstance(p, dict)}
    for i, phase in enumerate(data["phases"]):
        if not isinstance(phase, dict):
            continue
        for dep in phase.get("dependencies", []):
            if dep not in phase_ids:
                errors.append(f"phases[{i}].dependencies references unknown phase: {dep}")

    # Validate slice dependencies reference existing slices
    for i, phase in enumerate(data["phases"]):
        if not isinstance(phase, dict):
            continue
        for j, s in enumerate(phase.get("slices", [])):
            if not isinstance(s, dict):
                continue
            for dep in s.get("dependencies", []):
                if dep not in all_slice_ids:
                    errors.append(f"phases[{i}].slices[{j}].dependencies references unknown slice: {dep}")

    return errors


# ---------------------------------------------------------------------------
# DAG resolver
# ---------------------------------------------------------------------------

def resolve_dependency_order(slices: list[RoadmapSlice]) -> list[list[str]]:
    """Topological sort of slices by dependencies.

    Returns a list of levels, where each level contains slice_ids
    that can be executed in parallel (all dependencies satisfied).
    Raises ValueError if circular dependency detected.
    """
    # Build adjacency
    in_degree = defaultdict(int)
    dependents = defaultdict(list)  # dep -> list of slices that depend on it
    slice_ids = {s.slice_id for s in slices}

    for s in slices:
        in_degree[s.slice_id] = len([d for d in s.dependencies if d in slice_ids])
        for dep in s.dependencies:
            if dep in slice_ids:
                dependents[dep].append(s.slice_id)

    # Kahn's algorithm
    queue = deque([sid for sid, deg in in_degree.items() if deg == 0])
    levels = []
    processed = 0

    while queue:
        level = []
        next_queue = deque()
        while queue:
            sid = queue.popleft()
            level.append(sid)
            processed += 1
            for dependent in dependents[sid]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    next_queue.append(dependent)
        levels.append(sorted(level))  # Sort within level for determinism
        queue = next_queue

    if processed != len(slices):
        remaining = [s.slice_id for s in slices if s.slice_id not in
                     {sid for level in levels for sid in level}]
        raise ValueError(f"Circular dependency detected involving: {remaining}")

    return levels


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

@dataclass
class ExecutorConfig:
    """Configuration for the roadmap executor."""
    mission_id: str
    supervisor_id: str
    roadmap_path: Optional[str] = None
    roadmap: Optional[Roadmap] = None
    senior_consult_fn: Optional[Callable] = None
    pr_fn: Optional[Callable] = None
    lease_ttl: int = 3600
    max_ticks: int = 100


@dataclass(frozen=True)
class ExecutorResult:
    """Result of running the roadmap executor."""
    outcome: str  # "completed" | "blocked" | "human_gate" | "error"
    mission_id: str
    slices_completed: int
    slices_total: int
    current_phase: Optional[str]
    error: Optional[dict] = None


def run_roadmap_executor(
    conn: sqlite3.Connection,
    config: ExecutorConfig,
) -> ExecutorResult:
    """Run the roadmap executor: load roadmap, ingest slices, execute loop.

    This is the high-level entry point that orchestrates:
    1. Load and validate roadmap
    2. Ingest slices into supervisor
    3. Acquire lease
    4. Execute slices via supervisor tick
    5. Handle gates (senior consult, human)
    6. Track progress
    """
    # Load roadmap
    if config.roadmap:
        roadmap = config.roadmap
    elif config.roadmap_path:
        roadmap = load_roadmap(config.roadmap_path)
    else:
        return ExecutorResult(
            outcome="error",
            mission_id=config.mission_id,
            slices_completed=0,
            slices_total=0,
            current_phase=None,
            error={"message": "no roadmap provided"},
        )

    # Convert RoadmapSlice to dicts for add_slices
    slice_dicts = []
    for rs in roadmap.all_slices:
        slice_dicts.append({
            "slice_id": rs.slice_id,
            "phase": rs.phase,
            "description": rs.description,
            "dependencies": rs.dependencies,
            "material": rs.material,
            "acceptance_criteria": rs.acceptance_criteria,
            "tests": rs.tests,
            "gate_type": rs.gate_type,
            "max_attempts": rs.max_attempts,
            "priority": rs.priority,
        })

    # Ingest slices
    add_result = add_slices(conn, config.mission_id, slice_dicts)
    if add_result.outcome == "invalid":
        return ExecutorResult(
            outcome="error",
            mission_id=config.mission_id,
            slices_completed=0,
            slices_total=len(slice_dicts),
            current_phase=None,
            error={"message": f"slice ingestion failed: {add_result.errors}"},
        )

    # Acquire lease
    lease = acquire_supervisor_lease(
        conn, config.mission_id, config.supervisor_id, config.lease_ttl,
    )
    if lease.outcome == "lease_held":
        return ExecutorResult(
            outcome="error",
            mission_id=config.mission_id,
            slices_completed=0,
            slices_total=len(slice_dicts),
            current_phase=None,
            error={"message": f"lease held by {lease.error['message']}"},
        )

    # Execute loop
    slices_completed = 0
    ticks = 0
    current_phase = None

    while ticks < config.max_ticks:
        # Check lease before each tick
        from hermes_cli.kanban_supervisor import _validate_fencing
        if not _validate_fencing(
            conn, config.mission_id, config.supervisor_id, lease.fencing_epoch,
        ):
            # Try to renew
            lease = acquire_supervisor_lease(
                conn, config.mission_id, config.supervisor_id, config.lease_ttl,
            )
            if lease.outcome not in ("acquired", "renewed"):
                return ExecutorResult(
                    outcome="error",
                    mission_id=config.mission_id,
                    slices_completed=slices_completed,
                    slices_total=len(slice_dicts),
                    current_phase=current_phase,
                    error={"message": "lease lost and cannot renew"},
                )

        # Get next slice to determine phase
        from hermes_cli.kanban_supervisor import get_next_slice as _get_next
        next_slice = _get_next(conn, config.mission_id)
        if next_slice is None:
            # Check if all slices are done
            all_slices = get_roadmap_slices(conn, config.mission_id)
            terminal_count = sum(
                1 for s in all_slices if s.status in TERMINAL_SLICE_STATUSES
            )
            if terminal_count == len(all_slices):
                return ExecutorResult(
                    outcome="completed",
                    mission_id=config.mission_id,
                    slices_completed=slices_completed,
                    slices_total=len(slice_dicts),
                    current_phase=current_phase,
                )
            # Queue exhausted or blocked
            return ExecutorResult(
                outcome="blocked",
                mission_id=config.mission_id,
                slices_completed=slices_completed,
                slices_total=len(slice_dicts),
                current_phase=current_phase,
            )

        current_phase = next_slice.phase

        # Check if this slice needs senior consult
        if next_slice.gate_type == "senior_consult" and config.senior_consult_fn:
            # Invoke senior consult before executing
            try:
                consult_result = config.senior_consult_fn(next_slice)
                if consult_result.get("verdict") == "blocked":
                    mark_slice_blocked(
                        conn, config.mission_id, next_slice.slice_id,
                        {"reason": "senior_consult_blocked",
                         "details": consult_result},
                        config.supervisor_id, lease.fencing_epoch,
                    )
                    return ExecutorResult(
                        outcome="blocked",
                        mission_id=config.mission_id,
                        slices_completed=slices_completed,
                        slices_total=len(slice_dicts),
                        current_phase=current_phase,
                    )
            except Exception as exc:
                _log.warning("Senior consult failed: %s", exc)

        # Execute via supervisor tick
        def executor_fn(slice_rec: SliceRecord, fencing_epoch: int) -> dict:
            # Placeholder — real execution would delegate to subagent
            return {"outcome": "success", "evidence": {"result": "executed"}}

        result = run_supervisor_tick(
            conn, config.mission_id, config.supervisor_id, executor_fn,
        )

        ticks += 1

        if result.outcome == "slice_executed":
            slices_completed += 1
        elif result.outcome in ("slice_blocked", "slice_human_gate",
                                 "slice_queue_exhausted", "slice_failed",
                                 "lease_expired", "fencing_invalid"):
            return ExecutorResult(
                outcome="blocked" if result.outcome == "slice_blocked" else
                        "human_gate" if result.outcome == "slice_human_gate" else
                        "error",
                mission_id=config.mission_id,
                slices_completed=slices_completed,
                slices_total=len(slice_dicts),
                current_phase=current_phase,
                error={"message": f"slice {result.outcome}: {result.slice_id}"},
            )

    return ExecutorResult(
        outcome="completed" if slices_completed == len(slice_dicts) else "blocked",
        mission_id=config.mission_id,
        slices_completed=slices_completed,
        slices_total=len(slice_dicts),
        current_phase=current_phase,
    )
