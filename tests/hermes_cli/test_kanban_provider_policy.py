import importlib

import pytest


MODULE = "hermes_cli.kanban_provider_policy"


def load_module():
    return importlib.import_module(MODULE)


def policy():
    return {
        "roles": {
            "worker": {"tier": "cheap", "fallbacks": []},
            "reviewer": {"tier": "standard", "fallbacks": []},
        },
        "profiles": {
            "worker": {
                "role": "worker",
                "tier": "cheap",
                "capabilities": ["provider-llm", "repo-read"],
                "forbidden_credentials": ["github-token", "repo-admin"],
            },
            "reviewer": {
                "role": "reviewer",
                "tier": "standard",
                "capabilities": ["provider-llm", "repo-read", "reviewer"],
                "forbidden_credentials": ["repo-admin"],
            },
        },
        "providers": {
            "primary": {"capability_class": "provider-llm"},
            "backup": {"capability_class": "provider-llm"},
        },
    }


def test_explicit_primary_selection_is_auditable_and_deterministic():
    module = load_module()
    p = policy()
    first = module.select_provider(
        p,
        profile="worker",
        role="worker",
        requested_provider="primary",
        requested_model="cheap-model",
        available_models={"primary": {"cheap-model"}},
        credential_source_class="managed-provider",
    )
    second = module.select_provider(
        p,
        profile="worker",
        role="worker",
        requested_provider="primary",
        requested_model="cheap-model",
        available_models={"primary": {"cheap-model"}},
        credential_source_class="managed-provider",
    )

    assert first == second
    assert first.requested_provider == "primary"
    assert first.effective_model == "cheap-model"
    assert first.fallback_used is False
    assert first.role_tier == "cheap"
    assert first.credential_source_class == "managed-provider"


def test_worker_scope_rejects_github_and_repository_admin_credentials():
    module = load_module()
    invalid = policy()
    invalid["profiles"]["worker"]["forbidden_credentials"] = []
    invalid["profiles"]["worker"]["credentials"] = ["GH_TOKEN", "repository-admin"]

    with pytest.raises(module.ProviderPolicyError) as error:
        module.validate_policy(invalid)

    assert error.value.code == "AUTH_SCOPE_DENIED"


def test_fallback_must_be_explicit_same_tier_and_same_capability_class():
    module = load_module()
    p = policy()
    p["roles"]["worker"]["fallbacks"] = [
        {
            "provider": "backup",
            "model": "cheap-backup",
            "tier": "cheap",
            "capability_class": "provider-llm",
        }
    ]
    selection = module.select_provider(
        p,
        profile="worker",
        role="worker",
        requested_provider="primary",
        requested_model="cheap-model",
        available_models={"backup": {"cheap-backup"}},
        credential_source_class="managed-provider",
    )
    assert selection.effective_provider == "backup"
    assert selection.fallback_used is True

    p["roles"]["worker"]["fallbacks"][0]["tier"] = "standard"
    with pytest.raises(module.ProviderPolicyError) as error:
        module.select_provider(
            p,
            profile="worker",
            role="worker",
            requested_provider="primary",
            requested_model="cheap-model",
            available_models={"backup": {"cheap-backup"}},
            credential_source_class="managed-provider",
        )
    assert error.value.code == "POLICY_BLOCK"


def test_missing_capability_returns_sanitized_diagnostic():
    module = load_module()
    p = policy()
    with pytest.raises(module.ProviderPolicyError) as error:
        module.select_provider(
            p,
            profile="worker",
            role="worker",
            requested_provider="primary",
            requested_model="cheap-model",
            available_models={"primary": {"cheap-model"}},
            credential_source_class="managed-provider",
            required_capability="repo-maintainer",
        )

    assert error.value.to_diagnostic() == {
        "code": "AUTH_SCOPE_DENIED",
        "provider": "primary",
        "model": "cheap-model",
        "profile": "worker",
        "retryable": False,
        "recommended_action": "use a profile with the required capability",
        "secret_values_absent": True,
    }


def test_error_taxonomy_is_complete_and_secret_free():
    module = load_module()
    expected = {
        "AUTH_MISSING",
        "AUTH_INVALID",
        "AUTH_SCOPE_DENIED",
        "REGION_UNAVAILABLE",
        "MODEL_NOT_FOUND",
        "MODEL_CAPACITY",
        "RATE_LIMIT",
        "PROVIDER_TIMEOUT",
        "TRANSPORT_FAILURE",
        "MALFORMED_RESPONSE",
        "CONTEXT_LIMIT",
        "POLICY_BLOCK",
        "CONFIG_DIVERGENCE",
    }
    assert set(module.ERROR_TAXONOMY) == expected
