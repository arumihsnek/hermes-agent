"""Offline contract tests for the K9 planner envelope and scheduler dry-run."""

import json
from argparse import ArgumentParser
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db
from hermes_cli.kanban_planner import (
    PlannerLedger,
    PlannerMetrics,
    PlannerValidationError,
    materialize_task_specs,
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


def test_dry_run_projects_next_role_frontier_after_completed_subtasks():
    projection = scheduler_dry_run(_envelope(), completed_subtasks={"build"})

    assert projection["completed"] == ["build"]
    assert projection["ready_workers"] == []
    assert projection["ready_reviewers"] == ["review"]
    assert projection["not_yet_runnable"] == []
    assert projection["provider_calls"] == 0


def test_dry_run_rejects_unknown_completed_subtask():
    with pytest.raises(PlannerValidationError, match="unknown completed subtask"):
        scheduler_dry_run(_envelope(), completed_subtasks={"missing"})


def test_materialize_task_specs_preserves_tasks_links_and_is_immutable():
    specs = materialize_task_specs(_envelope())

    assert [task.id for task in specs.tasks] == ["build", "review"]
    assert specs.tasks[0].role == "worker"
    assert [(link.parent_id, link.child_id) for link in specs.links] == [
        ("build", "review")
    ]
    with pytest.raises(AttributeError):
        specs.tasks[0].id = "changed"


def test_materialize_task_specs_is_byte_stable_for_repeated_conversion():
    first = materialize_task_specs(_envelope())
    second = materialize_task_specs(_envelope())

    assert first == second
    assert first.tasks[0].acceptance_json == '{"required":true}'
    assert first.tasks[1].evidence_expectations_json == '["review_ref"]'


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


def test_plan_cli_dry_run_reads_envelope_without_initializing_database(tmp_path, monkeypatch, capsys):
    envelope_path = tmp_path / "planner.json"
    envelope_path.write_text(json.dumps(_envelope()), encoding="utf-8")

    def unexpected_db_init():
        raise AssertionError("read-only planner dry-run must not initialize the database")

    monkeypatch.setattr(kanban_db, "init_db", unexpected_db_init)
    parser = ArgumentParser(add_help=False)
    sub = parser.add_subparsers(dest="command")
    kanban_cli.build_parser(sub)
    args = parser.parse_args(["kanban", "plan", str(envelope_path), "--dry-run", "--json"])

    assert kanban_cli.kanban_command(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready_workers"] == ["build"]
    assert result["provider_calls"] == 0
    assert result["lifecycle_mutations"] == 0


def test_plan_cli_rejects_malformed_envelope_without_db_access(tmp_path, monkeypatch, capsys):
    envelope_path = tmp_path / "malformed.json"
    envelope_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(kanban_db, "init_db", lambda: pytest.fail("database must not be touched"))
    parser = ArgumentParser(add_help=False)
    sub = parser.add_subparsers(dest="command")
    kanban_cli.build_parser(sub)
    args = parser.parse_args(["kanban", "plan", str(envelope_path), "--dry-run", "--json"])

    assert kanban_cli.kanban_command(args) == 2
    assert "planner" in capsys.readouterr().err.lower()
