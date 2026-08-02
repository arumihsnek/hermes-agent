"""Offline contract tests for the K9 planner envelope and scheduler dry-run."""

import json

import pytest

from hermes_cli.kanban_planner import (
    PlannerLedger,
    PlannerMetrics,
    PlannerValidationError,
    scheduler_dry_run,
)


def _envelope():
    return {
        "schema_version": "planner-dag/v1",
        "subtasks": [
            {"id": "build", "title": "Build artifact", "role": "worker"},
            {"id": "review", "title": "Review artifact", "role": "reviewer"},
        ],
        "dependencies": [{"subtask_id": "review", "depends_on": "build"}],
        "roles": {"worker": {"tier": "cheap"}, "reviewer": {"tier": "standard"}},
        "acceptance": {"build": {"required": True}, "review": {"required": True}},
        "review_policy": {"build": {"reviewer_role": "reviewer"}, "review": {}},
        "batch_groups": [],
        "evidence_expectations": {"build": ["artifact"], "review": ["review_ref"]},
        "estimated_model_tier": {"build": "cheap", "review": "standard"},
        "risk_classification": {"build": "low", "review": "medium"},
        "closure_policy": {"allow_partial": False, "required_review_roles": {"build": ["reviewer"]}},
    }


def test_valid_envelope_dry_run_is_byte_stable_and_read_only():
    envelope = _envelope()

    first = scheduler_dry_run(envelope)
    second = scheduler_dry_run(envelope)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["ready_workers"] == ["build"]
    assert first["not_yet_runnable"] == ["review"]
    assert first["policy_violations"] == []
    assert first["provider_calls"] == 0
    assert first["protected_value_reads"] == 0
    assert first["lifecycle_mutations"] == 0


def test_invalid_envelope_is_rejected_before_any_import_commit():
    ledger = PlannerLedger()
    before = ledger.snapshot()
    invalid = _envelope()
    invalid["subtasks"].append({"id": "build", "title": "Duplicate", "role": "worker"})

    with pytest.raises(PlannerValidationError, match="duplicate subtask id"):
        ledger.import_envelope(invalid)

    assert ledger.snapshot() == before


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda e: e["subtasks"][0].update(role="unknown"), "unknown role"),
        (
            lambda e: e.__setitem__(
                "dependencies",
                [
                    {"subtask_id": "review", "depends_on": "build"},
                    {"subtask_id": "build", "depends_on": "review"},
                ],
            ),
            "dependency cycle",
        ),
        (lambda e: e.__setitem__("closure_policy", {}), "closure_policy"),
    ],
)
def test_structural_policy_violations_fail_closed(mutation, message):
    envelope = _envelope()
    mutation(envelope)

    with pytest.raises(PlannerValidationError, match=message):
        PlannerLedger().import_envelope(envelope)


def test_atomic_import_preserves_previous_snapshot_when_new_envelope_is_invalid():
    ledger = PlannerLedger()
    ledger.import_envelope(_envelope())
    before = ledger.snapshot()
    invalid = _envelope()
    invalid["dependencies"].append({"subtask_id": "missing", "depends_on": "build"})

    with pytest.raises(PlannerValidationError, match="unknown subtask"):
        ledger.import_envelope(invalid)

    assert ledger.snapshot() == before


def test_call_metrics_record_supplied_fields_without_inventing_cost():
    metrics = PlannerMetrics()
    metrics.record_call(
        provider="openai-codex",
        model="gpt-5.6-luna",
        tier="cheap",
        tokens=123,
        latency_ms=17,
        fallback=None,
        cache_hit=False,
    )

    assert metrics.as_dict() == {
        "calls": [
            {
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "tier": "cheap",
                "tokens": 123,
                "latency_ms": 17,
                "fallback": None,
                "cache_hit": False,
            }
        ],
        "absolute_cost": None,
    }
