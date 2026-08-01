from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_db as kb


def policy():
    return {
        "roles": {
            "worker": {
                "tier": "cheap",
                "capabilities": ["provider-llm", "repo-read", "runtime-executor"],
                "fallbacks": [{
                    "provider": "backup",
                    "model": "cheap-backup",
                    "tier": "cheap",
                    "capability_class": "provider-llm",
                    "order": 1,
                }],
            },
        },
        "profiles": {
            "worker": {
                "role": "worker",
                "tier": "cheap",
                "credential_source_class": "managed-provider",
                "capabilities": ["provider-llm", "repo-read", "runtime-executor"],
                "forbidden_credentials": ["GH_TOKEN", "repository-admin"],
            },
        },
        "providers": {
            "primary": {"capability_class": "provider-llm"},
            "backup": {"capability_class": "provider-llm"},
        },
    }


def task(task_id: str) -> kb.Task:
    return kb.Task(
        id=task_id,
        title="runtime policy",
        body=None,
        assignee="worker",
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        model_override="cheap-primary",
        provider_override="primary",
    )


def test_dispatcher_uses_effective_fallback_and_writes_sanitized_selection_audit(monkeypatch, tmp_path):
    policy_path = tmp_path / "provider-policy.json"
    policy_path.write_text(json.dumps(policy()))
    monkeypatch.setenv("HERMES_KANBAN_PROVIDER_POLICY", str(policy_path))
    monkeypatch.setenv("HERMES_KANBAN_AVAILABLE_MODELS_JSON", json.dumps({"primary": [], "backup": ["cheap-backup"]}))
    monkeypatch.setenv("HERMES_KANBAN_CREDENTIAL_SOURCE_CLASS", "managed-provider")
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr(kb.subprocess, "Popen", fake_popen)
    task_value = task("task-runtime-policy")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert kb._default_spawn(task_value, str(workspace)) == 4242
    cmd = captured["cmd"]
    assert cmd[cmd.index("-m") + 1] == "cheap-backup"
    assert cmd[cmd.index("--provider") + 1] == "backup"
    audit = json.loads(captured["env"]["HERMES_KANBAN_PROVIDER_SELECTION_JSON"])
    assert audit["fallback_used"] is True
    assert audit["effective_model"] == "cheap-backup"
    audit_path = Path(captured["env"]["HERMES_KANBAN_PROVIDER_SELECTION_AUDIT"])
    assert json.loads(audit_path.read_text()) == audit
    assert "secret" not in audit_path.read_text().lower()


def test_dispatcher_fails_closed_when_policy_preflight_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_PROVIDER_POLICY", str(tmp_path / "missing.json"))
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    task_value = task("task-runtime-policy-missing")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        kb._default_spawn(task_value, str(workspace))
    except RuntimeError as error:
        assert "provider policy" in str(error).lower()
    else:
        raise AssertionError("dispatcher must not spawn without the K8 policy artifact")
